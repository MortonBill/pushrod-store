"""
Digital fulfillment: paid checkout -> emailed signed download links.

The print path (fulfill.py -> Printful) ships physical goods. Digital SKUs
(SkillForge playbooks, RE/IronHead guides, Stitchfolk patterns — anything
whose catalog row says fulfillment_type=digital) have nothing to print:
on checkout.session.completed the buyer gets ONE email with a signed,
expiring download link per digital item, served by the app's /download
endpoint from a configured digital-files directory.

Delivery chain (each step independently configured; unconfigured steps
degrade loudly but never silently swallow a paid order):

  1. Signed link  — HMAC-SHA256 token over {sku, email, exp}. Secret from
     DIGITAL_DOWNLOAD_SECRET. Missing secret is a hard DigitalConfigError
     (same fail-loud discipline as PrintfulConfigError): the webhook 500s,
     Stripe retries, and setting the env var unblocks redelivery.
  2. Email        — Brevo API, sender bill@aitoolsfortoday.com. Key from
     BREVO_API_KEY. Missing key => DRY-RUN: the email is logged in full and
     recorded as dry-run in the ledger, never sent, never raises.
  3. Kit tag      — Kit v4 API when KIT_API_KEY + KIT_BUYER_TAG_ID are set;
     otherwise a queued/logged no-op (recorded "queued" in the ledger).
     Tagging NEVER blocks delivery.

Idempotency: every fulfilled session id is persisted in a JSON ledger
(DIGITAL_FULFILLMENTS_PATH, default digital_fulfillments.json next to this
file). A duplicate checkout.session.completed for the same session returns
{"already_fulfilled": True} and sends NO second email.

Refunds: issued by a human in the Stripe dashboard (see README.md in this
directory). charge.refunded only flags the ledger; a delivered download
cannot be recalled — links simply expire per DIGITAL_LINK_TTL_DAYS.
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("pushrod.digital")

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
KIT_API_BASE = "https://api.kit.com/v4"
DEFAULT_SENDER_EMAIL = "bill@aitoolsfortoday.com"
DEFAULT_SENDER_NAME = "AI Tools for Today"
DEFAULT_TTL_DAYS = 7

# Brand id (catalog OWNERSHIP value) -> customer-facing display name, kept
# in sync with brands/<id>.yaml `brand.name`. The shared store service
# boots under ONE brand, so a delivery email branded from the boot brand
# alone lands in every buyer's inbox as that one brand ("Your download
# from SkillForge AI" on a RestorationEssentials purchase). The delivery
# brand therefore resolves from the purchased products' owners below;
# the caller-supplied store name is only the fallback for carts whose
# owners are unknown or mixed.
BRAND_DISPLAY_NAMES = {
    "restorationessentials": "Restoration Essentials",
    "ironhead": "IronHead",
    "skillforge": "SkillForge AI",
    "stitchfolk": "Stitchfolk",
    "everready": "EverReady Family",
    "sportroots": "SportRoots",
    "pushrod": "PUSHROD\u2122",
    "gateway": "PUSHROD\u2122",
}


def delivery_brand_name(digital_lines, products_by_sku, fallback=""):
    """Display brand for a delivery email: the single owning brand of the
    digital lines being delivered. Mixed-brand carts and unknown owners
    fall back to `fallback` (the caller's store name, today's behavior).
    """
    owners = []
    for line in digital_lines:
        owner = (products_by_sku.get(line["sku"]) or {}).get("owner")
        if owner and owner not in owners:
            owners.append(owner)
    if len(owners) == 1 and owners[0] in BRAND_DISPLAY_NAMES:
        return BRAND_DISPLAY_NAMES[owners[0]]
    return fallback


class DigitalConfigError(RuntimeError):
    """Digital fulfillment is misconfigured (e.g. no signing secret)."""


class DigitalSendError(RuntimeError):
    """The delivery email itself failed (Brevo rejected/errored)."""


# ---------- signed download tokens ----------

def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class DownloadTokenSigner:
    """Mint and verify expiring download tokens.

    Token = base64url(payload-json) + "." + base64url(HMAC-SHA256(secret,
    payload-part)). The payload binds the link to ONE sku and the buyer's
    email, so a forwarded link is useless past its expiry and a tampered
    payload fails the signature check.
    """

    def __init__(self, secret=None):
        secret = secret if secret is not None else os.environ.get(
            "DIGITAL_DOWNLOAD_SECRET", "")
        if not secret:
            raise DigitalConfigError(
                "DIGITAL_DOWNLOAD_SECRET is not set. Download links are "
                "HMAC-signed with this secret; generate any long random "
                "string and set it in the service environment (Render -> "
                "service -> Environment). Rotating it invalidates every "
                "outstanding buyer link, so set it once and keep it.")
        self._secret = secret.encode() if isinstance(secret, str) else secret

    def _sign(self, payload_part):
        return hmac.new(self._secret, payload_part.encode(),
                        hashlib.sha256).digest()

    def mint(self, sku, email, ttl_seconds=None):
        if ttl_seconds is None:
            ttl_seconds = int(os.environ.get("DIGITAL_LINK_TTL_DAYS",
                                             DEFAULT_TTL_DAYS)) * 86400
        payload = {"sku": sku, "email": (email or "").strip().lower(),
                   "exp": int(time.time()) + int(ttl_seconds)}
        part = _b64e(json.dumps(payload, separators=(",", ":"),
                                sort_keys=True).encode())
        return f"{part}.{_b64e(self._sign(part))}"

    def verify(self, token, now=None):
        """Returns the payload dict. Raises ValueError('malformed' |
        'bad_signature' | 'expired') — the /download endpoint maps these
        to 403/403/410."""
        try:
            part, sig = token.split(".", 1)
            payload = json.loads(_b64d(part))
            given = _b64d(sig)
        except Exception:
            raise ValueError("malformed")
        if not hmac.compare_digest(given, self._sign(part)):
            raise ValueError("bad_signature")
        if int(payload.get("exp", 0)) < int(now if now is not None
                                            else time.time()):
            raise ValueError("expired")
        return payload


# ---------- Brevo delivery email ----------

class BrevoSender:
    """Minimal Brevo transactional sender (urllib, same as printful_client).

    No BREVO_API_KEY => dry-run: log the email, return {"dry_run": True}.
    This keeps test-mode end-to-end runs honest without sending real mail.
    """

    def __init__(self, api_key=None, sender_email=None, sender_name=None):
        self.api_key = api_key if api_key is not None else os.environ.get(
            "BREVO_API_KEY", "")
        self.sender_email = sender_email or os.environ.get(
            "BREVO_SENDER_EMAIL", DEFAULT_SENDER_EMAIL)
        self.sender_name = sender_name or os.environ.get(
            "BREVO_SENDER_NAME", DEFAULT_SENDER_NAME)
        self.dry_run = not self.api_key
        if self.dry_run:
            log.warning("BREVO DRY-RUN: BREVO_API_KEY not set — delivery "
                        "emails will be logged, not sent.")

    def send(self, to_email, subject, html_content, text_content):
        payload = {
            "sender": {"name": self.sender_name, "email": self.sender_email},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_content,
            "textContent": text_content,
            "tags": ["digital-delivery"],
        }
        if self.dry_run:
            log.warning("BREVO DRY-RUN email to %s: %s\n%s", to_email,
                        subject, text_content)
            return {"dry_run": True}
        req = urllib.request.Request(
            BREVO_API_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"api-key": self.api_key,
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise DigitalSendError(
                f"Brevo API {e.code}: {e.read().decode(errors='replace')[:300]}")
        except Exception as e:  # noqa: BLE001 — surface as send failure
            raise DigitalSendError(f"Brevo send failed: {e}")


# ---------- branded delivery email ----------

# Email identity per brand. The storefronts carry each brand's name,
# colors, and tagline; the delivery email uses the same identity so a
# buyer's inbox matches the store they just bought from (Bill
# 2026-10-09: the bare black-and-white delivery email was the defect).
# Inline styles only, no images — nothing for a mail client to block.
BRAND_EMAIL_STYLES = {
    "restorationessentials": {
        "display": "RestoreEssentials",
        "tagline": "American iron, restored right",
        "accent": "#e8a020", "on_accent": "#1a1206",
        "dark": "#101418", "on_dark": "#f0e9d8",
        "domain": "restoreessentials.com",
        "logo_url": "https://restoreessentials.com/static/img/"
                    "restoration-essentials-logo.png",
        "logo_w": 110,
    },
    "ironhead": {
        "display": "IronHead",
        "tagline": "Vintage iron, kept running",
        "accent": "#c8402a", "on_accent": "#ffffff",
        "dark": "#14100d", "on_dark": "#f0e9d8",
        "domain": "ironheadguides.com",
        "logo_url": "https://ironheadguides.com/static/img/ironhead-logo.png",
        "logo_w": 110,
    },
    "skillforgeai": {
        "display": "SkillForge AI",
        "tagline": "AI playbooks for people who work with their hands",
        "accent": "#2dd4bf", "on_accent": "#05302a",
        "dark": "#0e1319", "on_dark": "#edf2f4",
        "domain": "skillforgeaihub.com",
        "logo_url": "https://skillforgeaihub.com/static/img/"
                    "skillforge-ai-logo.png",
        "logo_w": 110,
    },
    "stitchfolk": {
        "display": "Stitchfolk",
        "tagline": "Handwork patterns, made to be made",
        "accent": "#7a3b54", "on_accent": "#ffffff",
        "dark": "#221418", "on_dark": "#f3e8e2",
        "domain": "",
        "logo_url": "https://pushrodshop.com/static/img/stitchfolk-logo.png",
        "logo_w": 110,
    },
    "everreadyfamily": {
        "display": "EverReady Family",
        "tagline": "Everything your family needs, ready before it's needed",
        "accent": "#c9a24b", "on_accent": "#211a05",
        "dark": "#141210", "on_dark": "#f3ede2",
        "domain": "everready-family.com",
        "logo_url": "https://everready-family.com/static/img/"
                    "everready-family-logo.png",
        "logo_w": 110,
    },
    "pushrod": {
        "display": "PushRod",
        "tagline": "Garage-built gear for the air-cooled faithful",
        "accent": "#f59e0b", "on_accent": "#231600",
        "dark": "#14100c", "on_dark": "#f3ead8",
        "domain": "pushrodshop.com",
        "logo_url": "https://pushrodshop.com/static/img/pushrod-logo.png",
        "logo_w": 200,
    },
}

# SKU prefix -> style key, so a mixed-brand service process still
# brands the email by what was actually bought.
_SKU_STYLE_KEYS = (
    ("ER-", "everreadyfamily"),
    ("SF-", "skillforgeai"),
    ("ST-", "stitchfolk"),
    ("IH-", "ironhead"),
    ("RE-", "restorationessentials"),
    ("PR-", "pushrod"),
)

_DEFAULT_EMAIL_STYLE = {
    "display": "AI Tools for Today",
    "tagline": "Your order is ready",
    "accent": "#9a7b2d", "on_accent": "#ffffff",
    "dark": "#1c1c1e", "on_dark": "#f5f0e6",
    "domain": "",
}


def _style_key(name):
    return "".join((name or "").replace("™", "").replace("®", "")
                   .lower().split())


def email_style_for(store_name="", skus=()):
    """Resolve the brand identity for a delivery email.

    Prefers the store name the caller passed; falls back to the first
    recognized SKU prefix; defaults to the house identity. Never
    raises — a delivery email must go out even if branding misses.
    """
    style = BRAND_EMAIL_STYLES.get(_style_key(store_name))
    if style:
        return style
    for sku in skus or ():
        upper = (sku or "").upper()
        for prefix, key in _SKU_STYLE_KEYS:
            if upper.startswith(prefix):
                return BRAND_EMAIL_STYLES[key]
    if store_name:
        style = dict(_DEFAULT_EMAIL_STYLE)
        style["display"] = store_name
        return style
    return _DEFAULT_EMAIL_STYLE


def render_delivery_email(style, items, tail, intro):
    """Branded, email-client-safe HTML for a delivery email.

    items: list of (title, url, note). A None url renders a note row
    (drive.py's set-up-by-hand SKUs). Titles and URLs are escaped.
    """
    esc = html.escape
    accent, on_accent = style["accent"], style["on_accent"]
    dark, on_dark = style["dark"], style["on_dark"]
    rows = []
    for title, url, note in items:
        t = esc(title or "")
        if url:
            u = esc(url, quote=True)
            rows.append(
                '<tr><td style="padding:14px 0;border-bottom:1px solid '
                '#e7e0d2;">'
                f'<div style="font-size:15px;font-weight:bold;'
                f'color:#1d1a14;">{t}</div>'
                f'<a href="{u}" style="display:inline-block;margin-top:'
                f'8px;background:{accent};color:{on_accent};font-size:'
                f'14px;font-weight:bold;text-decoration:none;padding:'
                f'10px 20px;border-radius:6px;">Download</a>'
                f'<div style="margin-top:8px;font-size:11px;color:'
                f'#8a8172;word-break:break-all;">{u}</div>'
                '</td></tr>')
        else:
            note_html = (f'<div style="font-size:13px;color:#5c5546;'
                         f'margin-top:4px;">{esc(note)}</div>'
                         if note else "")
            rows.append(
                '<tr><td style="padding:14px 0;border-bottom:1px solid '
                '#e7e0d2;">'
                f'<div style="font-size:15px;font-weight:bold;'
                f'color:#1d1a14;">{t}</div>{note_html}</td></tr>')
    footer_brand = esc(style["display"])
    if style.get("domain"):
        footer_brand += f' &middot; {esc(style["domain"])}'
    logo_row = ""
    if style.get("logo_url"):
        logo_row = (
            '<tr><td align="center" style="background:#ffffff;'
            'padding:20px 28px 4px;">'
            f'<img src="{esc(style["logo_url"], quote=True)}" '
            f'alt="{esc(style["display"])}" '
            f'width="{int(style.get("logo_w", 110))}" '
            f'style="display:block;margin:0 auto;width:'
            f'{int(style.get("logo_w", 110))}px;height:auto;'
            'border:0;"></td></tr>')
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{esc(style["display"])}</title></head>'
        '<body style="margin:0;padding:0;">'
        '<table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" style="background:#efe9df;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" '
        'cellspacing="0" style="max-width:600px;width:100%;background:'
        '#ffffff;border-radius:10px;overflow:hidden;font-family:'
        'Arial,Helvetica,sans-serif;">'
        f'{logo_row}'
        f'<tr><td style="background:{dark};padding:22px 28px;">'
        f'<div style="font-size:24px;font-weight:bold;color:{on_dark};'
        f'letter-spacing:.4px;">{esc(style["display"])}</div>'
        f'<div style="font-size:13px;color:{accent};margin-top:4px;">'
        f'{esc(style["tagline"])}</div>'
        '</td></tr>'
        f'<tr><td style="background:{accent};height:4px;font-size:0;">'
        '&nbsp;</td></tr>'
        '<tr><td style="padding:24px 28px;color:#241f16;font-size:15px;'
        'line-height:1.55;">'
        f'<p style="margin:0;">{esc(intro)}</p>'
        '<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0">{"".join(rows)}</table>'
        f'<p style="margin:18px 0 0;font-size:13px;color:#5c5546;">'
        f'{esc(tail)}</p>'
        '</td></tr>'
        f'<tr><td style="background:{dark};padding:16px 28px;'
        f'font-size:12px;color:{on_dark};">{footer_brand}<br>'
        'Questions? Just reply to this email.</td></tr>'
        '</table></td></tr></table></body></html>')


# ---------- Kit buyer tag ----------

class KitTagger:
    """Tags the buyer in Kit after a digital purchase.

    Configured only when BOTH KIT_API_KEY and KIT_BUYER_TAG_ID are set.
    Otherwise (or on any API error) returns "queued": the tag is recorded
    in the ledger as owed and can be applied later — a marketing tag must
    never hold a paid download hostage.
    """

    def __init__(self, api_key=None, tag_id=None):
        self.api_key = api_key if api_key is not None else os.environ.get(
            "KIT_API_KEY", "")
        self.tag_id = tag_id if tag_id is not None else os.environ.get(
            "KIT_BUYER_TAG_ID", "")
        self.configured = bool(self.api_key and self.tag_id)
        if not self.configured:
            log.warning("KIT tag not configured (KIT_API_KEY / "
                        "KIT_BUYER_TAG_ID) — buyer tags will be queued.")

    def tag_buyer(self, email):
        if not self.configured:
            log.info("KIT tag QUEUED for %s (not configured)", email)
            return "queued"
        url = f"{KIT_API_BASE}/tags/{self.tag_id}/subscribers"
        req = urllib.request.Request(
            url, data=json.dumps({"email_address": email}).encode(),
            method="POST",
            headers={"X-Kit-Api-Key": self.api_key,
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return "tagged"
        except Exception as e:  # noqa: BLE001 — never block delivery
            log.warning("KIT tag failed for %s (%s) — queued instead",
                        email, e)
            return "queued"


# ---------- idempotency ledger ----------

def default_ledger_path():
    return os.environ.get(
        "DIGITAL_FULFILLMENTS_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "digital_fulfillments.json"))


class DigitalLedger:
    """JSON record of fulfilled Stripe sessions (the idempotency wall).

    Shape: {"sessions": {session_id: {"email", "skus", "emailed",
    "email_dry_run", "kit", "refunded", "ts"}}}. Written atomically
    (tmp file + os.replace) so a crash mid-write can't corrupt it.
    """

    def __init__(self, path=None):
        self.path = path or default_ledger_path()
        self._data = {"sessions": {}}
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except (ValueError, OSError):
                log.warning("digital ledger unreadable at %s — starting "
                            "fresh (duplicate-webhook guard degraded)",
                            self.path)
                self._data = {"sessions": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=1)
        os.replace(tmp, self.path)

    def get(self, session_id):
        return self._data["sessions"].get(session_id)

    def is_fulfilled(self, session_id):
        rec = self.get(session_id)
        return bool(rec and rec.get("emailed"))

    def record(self, session_id, email, skus, emailed, email_dry_run, kit):
        self._data["sessions"][session_id] = {
            "email": email, "skus": list(skus), "emailed": bool(emailed),
            "email_dry_run": bool(email_dry_run), "kit": kit,
            "refunded": False, "ts": int(time.time()),
        }
        self._save()

    def mark_refunded(self, session_id):
        rec = self.get(session_id)
        if rec is None:
            return False
        rec["refunded"] = True
        self._save()
        return True


# ---------- the fulfillment entry point ----------

def fulfill_digital_lines(stripe_session_id, customer_email, digital_lines,
                          products_by_sku, download_base_url,
                          store_name="", signer=None, sender=None,
                          kit_tagger=None, ledger=None, ttl_seconds=None):
    """Deliver digital cart lines for a paid session. Returns a summary
    dict; sends at most ONE email per session, ever (ledger-guarded).

    digital_lines: [{sku, qty}] already filtered to fulfillment_type=digital.
    download_base_url: public base of this service, e.g. https://shop... —
    links are <base>/download/<token>.
    """
    ledger = ledger if ledger is not None else DigitalLedger()
    if ledger.is_fulfilled(stripe_session_id):
        log.info("digital: session %s already fulfilled — no duplicate "
                 "email", stripe_session_id)
        return {"already_fulfilled": True, "session_id": stripe_session_id}

    signer = signer if signer is not None else DownloadTokenSigner()
    sender = sender if sender is not None else BrevoSender()
    kit_tagger = kit_tagger if kit_tagger is not None else KitTagger()

    base = download_base_url.rstrip("/")
    items = []  # (title, url)
    for line in digital_lines:
        product = products_by_sku.get(line["sku"]) or {}
        # A bundle SKU carries no file of its own: it delivers ONE signed
        # link per component SKU (catalog `bundle_skus`), each resolving
        # to that component's deliverable through /download exactly like
        # a direct purchase of the component.
        component_skus = product.get("bundle_skus") or [line["sku"]]
        for comp_sku in component_skus:
            comp = products_by_sku.get(comp_sku) or {}
            title = comp.get("title") or product.get("title") or comp_sku
            token = signer.mint(comp_sku, customer_email,
                                ttl_seconds=ttl_seconds)
            items.append((title, f"{base}/download/{token}"))

    # The delivery brand follows the purchased products, not the service's
    # boot brand (see BRAND_DISPLAY_NAMES): a RestorationEssentials buyer's
    # email says "Restoration Essentials" even though the shared service
    # boots as another brand. store_name stays the fallback.
    store_name = delivery_brand_name(digital_lines, products_by_sku,
                                     store_name)
    ttl_days = int(os.environ.get("DIGITAL_LINK_TTL_DAYS", DEFAULT_TTL_DAYS))
    subject = (f"Your download from {store_name}" if store_name
               else "Your download links")
    style = email_style_for(store_name,
                            [line["sku"] for line in digital_lines])
    if store_name:
        # The banner carries the same resolved brand name as the
        # subject; the style contributes colors, tagline, and domain.
        style = {**style, "display": store_name}
    text_lines = [f"{style['display']} — {style['tagline']}", "",
                  "Thanks for your order — your download(s) are ready:",
                  ""]
    for title, url in items:
        text_lines.append(f"{title}\n{url}\n")
    tail = (f"Links are tied to this email address and expire in "
            f"{ttl_days} days. Questions? Just reply to this email.")
    text_lines.append(tail)
    html_content = render_delivery_email(
        style, [(title, url, None) for title, url in items], tail,
        "Thanks for your order — your download(s) are ready:")

    result = sender.send(customer_email, subject, html_content,
                         "\n".join(text_lines))
    kit_status = kit_tagger.tag_buyer(customer_email)
    ledger.record(stripe_session_id, customer_email,
                  [l["sku"] for l in digital_lines], emailed=True,
                  email_dry_run=bool(result.get("dry_run")), kit=kit_status)
    log.info("digital: session %s delivered %d item(s) to %s "
             "(dry_run=%s, kit=%s)", stripe_session_id, len(items),
             customer_email, bool(result.get("dry_run")), kit_status)
    return {"already_fulfilled": False, "session_id": stripe_session_id,
            "items": [t for t, _ in items], "links": [u for _, u in items],
            "email_dry_run": bool(result.get("dry_run")), "kit": kit_status}
