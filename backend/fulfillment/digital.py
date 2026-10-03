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

    ttl_days = int(os.environ.get("DIGITAL_LINK_TTL_DAYS", DEFAULT_TTL_DAYS))
    subject = (f"Your download from {store_name}" if store_name
               else "Your download links")
    text_lines = ["Thanks for your order — your download(s) are ready:",
                  ""]
    html_items = []
    for title, url in items:
        text_lines.append(f"{title}\n{url}\n")
        html_items.append(
            f'<li><a href="{html.escape(url)}">{html.escape(title)}</a></li>')
    tail = (f"Links are tied to this email address and expire in "
            f"{ttl_days} days. Questions? Just reply to this email.")
    text_lines.append(tail)
    html_content = ("<p>Thanks for your order — your download(s) are "
                    f"ready:</p><ul>{''.join(html_items)}</ul>"
                    f"<p>{html.escape(tail)}</p>")

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
