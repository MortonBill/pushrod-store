"""Automatic beta-guide delivery (Bill 2026-10-08: no hand delivery).

Kit captures the signup and applies a beta tag. Two triggers converge on
this module's one delivery path:

* Kit's signed ``subscriber.tag_added`` webhook (primary, real time), and
* a watermarked poller that re-reads the four beta tags and posts any new
  or changed subscriber here (guaranteed fallback).

The endpoint resolves the promised file deterministically, mints the
store's existing per-subscriber signed download token, sends the delivery
email through the store's existing Brevo sender, and records the delivery
in the shared leads SQLite database. A paid product file is never exposed
on a public link: beta links are the same expiring, email-bound tokens as
paid fulfillment (see fulfillment/digital.py).

The EverReady Executor's checklist is the one deliberate exception: it is
a free lead magnet already served from a public page, so its delivery
email links that page directly instead of minting a token.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from fulfillment import digital as digital_mod
from fulfillment import storage as storage_mod
import leads as leads_mod

log = logging.getLogger("pushrod.beta_delivery")

bp = Blueprint("beta_delivery", __name__)

# Kit tag id -> internal brand key. These are the four beta tags whose
# capture was proven in the 2026-10-08 lead-magnet audit, plus the
# EverReady checklist tag (free magnet; see module docstring).
TAG_TO_BRAND = {
    23454693: "restorationessentials",
    23454694: "ironhead",
    23454696: "stitchfolk",
    23454697: "skillforge",
    24224974: "everready-checklist",
}

# Kit form id / hosted uid -> internal brand key (for subscribed_to_form
# events and direct calls that carry a form reference instead of a tag).
FORM_TO_BRAND = {
    "9922976": "restorationessentials",
    "612f5dffc5": "restorationessentials",
    "9923017": "ironhead",
    "3b635485d5": "ironhead",
    "9923038": "stitchfolk",
    "2f09d9fd9a": "stitchfolk",
    "9923047": "skillforge",
    "5b33261b97": "skillforge",
    "9992757": "everready-checklist",
    "57816ee3d5": "everready-checklist",
}

BRAND_ALIASES = {
    "restorationessentials": "restorationessentials",
    "restoreessentials": "restorationessentials",
    "re": "restorationessentials",
    "ironhead": "ironhead",
    "ih": "ironhead",
    "stitchfolk": "stitchfolk",
    "skillforge": "skillforge",
    "skillforgeai": "skillforge",
    "everready-checklist": "everready-checklist",
    "everready_checklist": "everready-checklist",
}

BRAND_NAMES = {
    "restorationessentials": "RestoreEssentials",
    "ironhead": "IronHead",
    "stitchfolk": "Stitchfolk",
    "skillforge": "SkillForge AI",
    "everready-checklist": "EverReady Family",
}

# Fixed promises (the exact files the beta offers name).
IRONHEAD_SKU = "IH-CB750-SOHC"          # Volume 1: Honda CB750
STITCHFOLK_SKU = "ST-PETAL-SHAWL-001"   # Petal Crescent Shawl pattern
SKILLFORGE_SKU = "SF-ELEC-001"          # Electricians AI Playbook

# RE fallback: the brand-wide Master Restoration Checklist ($19.95
# product, deliberately NOT in the sellable catalog — this SKU can only
# ever be minted by this endpoint, never bought or browsed to).
FALLBACK_SKU = "RE-GD-MASTER-CHECKLIST"
FALLBACK_FILE = "RestorationEssentials_Master-Restoration-Checklist.pdf"
FALLBACK_TITLE = "Automotive Restoration Master Checklist"

# EverReady free checklist (public lead magnet; no token by design).
CHECKLIST_SKU = "ER-FIRST30-FREE"
CHECKLIST_TITLE = "Executor's First 30 Days Checklist"
CHECKLIST_FILE = "executors-first-30-days-checklist.pdf"
CHECKLIST_URL = ("https://everready-family.com/static/free/everready/"
                 "executors-first-30-days-checklist.pdf")

# SHA-256 of the poller/webhook path token. The token itself lives only
# in the Kit webhook endpoint's target URL (the poller reads it back
# through the Kit API each run); only its hash is committed. A
# BETA_DELIVERY_TOKEN / BETA_DELIVERY_TOKEN_SHA256 service env var, when
# set, overrides this fallback.
_FALLBACK_TOKEN_SHA256 = (
    "414531ae314d79cd003a34afc8d76023c27cae06a2bcc89f609a93915e0403d5")

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

# Wiring supplied by init().
_PRODUCTS = {}
_ROOT_DIR = ""
_DIGITAL_FILES_DIR = None
_PUBLIC_BASE_URL = None
_SENDER_FACTORY = None
_SIGNER_FACTORY = None
_STORAGE_FACTORY = None


def fallback_product():
    """The RE no-exact-match deliverable, as a product-shaped dict."""
    return {
        "sku": FALLBACK_SKU,
        "title": FALLBACK_TITLE,
        "owner": "restorationessentials",
        "fulfillment_type": "digital",
        "digital_file": FALLBACK_FILE,
    }


def product_for_sku(sku):
    """Catalog product for a SKU, including the uncatalogued RE fallback."""
    if sku == FALLBACK_SKU:
        return fallback_product()
    return _PRODUCTS.get(sku)


# ---------- trigger authentication ----------

def _token_ok(candidate):
    if not candidate:
        return False
    env_token = os.environ.get("BETA_DELIVERY_TOKEN", "")
    if env_token:
        return hmac.compare_digest(candidate, env_token)
    expected = (os.environ.get("BETA_DELIVERY_TOKEN_SHA256", "")
                or _FALLBACK_TOKEN_SHA256).strip().lower()
    digest = hashlib.sha256(candidate.encode()).hexdigest()
    return hmac.compare_digest(digest, expected)


def _signature_ok():
    """Verify a Kit-signed delivery when KIT_WEBHOOK_SECRET is set."""
    secret = os.environ.get("KIT_WEBHOOK_SECRET", "")
    header = request.headers.get("X-Kit-Signature", "")
    if not secret or not header:
        return False
    parts = [p.strip() for p in header.split(",")]
    timestamp = next((p[2:] for p in parts if p.startswith("t=")), None)
    if timestamp is None:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    raw = request.get_data(cache=True) or b""
    signed = f"{timestamp}.".encode() + raw
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(p[3:], expected)
               for p in parts if p.startswith("v1="))


def _authorized(path_token=None):
    if path_token and _token_ok(path_token):
        return True
    if _token_ok(request.headers.get("X-Beta-Delivery-Token", "")):
        return True
    if request.headers.get("X-Kit-Signature"):
        return _signature_ok()
    return False


# ---------- delivery record (shared leads.db) ----------

def _db_path():
    return leads_mod.DB_PATH


def _conn():
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS beta_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            brand TEXT NOT NULL,
            sku TEXT NOT NULL,
            file_name TEXT NOT NULL,
            product_title TEXT,
            token TEXT,
            status TEXT NOT NULL,
            message_id TEXT,
            error TEXT,
            source TEXT,
            signup_fields TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(email, brand, sku)
        )
        """
    )
    conn.commit()
    conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _claim(email, brand, product, token, source, fields):
    """Insert or reclaim the delivery row. Returns (row_dict, state) where
    state is 'claimed', 'already_delivered', or 'in_progress'."""
    now = _now()
    file_name = os.path.basename(product.get("digital_file") or "")
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM beta_deliveries WHERE email=? AND brand=? AND sku=?",
            (email, brand, product["sku"])).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO beta_deliveries (email, brand, sku, file_name,"
                " product_title, token, status, source, signup_fields,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?, 'sending',"
                " ?, ?, ?, ?)",
                (email, brand, product["sku"], file_name,
                 product.get("title"), token, source,
                 json.dumps(fields or {}), now, now))
            conn.commit()
            return ({"sku": product["sku"]}, "claimed")
        row = dict(row)
        if row["status"] == "sent":
            conn.commit()
            return (row, "already_delivered")
        if row["status"] == "sending":
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(row["updated_at"])).total_seconds()
            except ValueError:
                age = 9999
            if age < 900:
                conn.commit()
                return (row, "in_progress")
        conn.execute(
            "UPDATE beta_deliveries SET token=?, status='sending', error=NULL,"
            " source=?, signup_fields=?, updated_at=? WHERE id=?",
            (token, source, json.dumps(fields or {}), now, row["id"]))
        conn.commit()
        return (row, "claimed")
    finally:
        conn.close()


def _finish(email, brand, sku, status, message_id=None, error=None):
    conn = _conn()
    conn.execute(
        "UPDATE beta_deliveries SET status=?, message_id=?, error=?,"
        " updated_at=? WHERE email=? AND brand=? AND sku=?",
        (status, message_id, (error or "")[:500] or None, _now(),
         email, brand, sku))
    conn.commit()
    conn.close()


# ---------- deterministic file resolution ----------

_GENERIC_TOKENS = {
    "restoration", "guide", "the", "a", "an", "and", "for", "my", "i",
    "drive", "car", "truck", "american", "muscle", "factory", "correct",
    "free", "beta", "edition", "pdf", "download", "with", "series",
}

_MAKE_GROUPS = [
    {"chevrolet", "chevy"},
    {"ford"},
    {"dodge"},
    {"plymouth"},
    {"pontiac"},
    {"buick"},
    {"oldsmobile", "olds"},
    {"studebaker"},
    {"mercury"},
    {"chrysler"},
    {"amc"},
    {"gmc"},
    {"jeep"},
    {"cadillac"},
    {"lincoln"},
    {"rambler"},
    {"hudson"},
    {"packard"},
    {"international"},
]


def _tokens(text):
    text = (text or "").lower().replace("&", " and ")
    toks = re.findall(r"[a-z0-9]+", text)
    out = []
    for tok in toks:
        if tok == "chevy":
            tok = "chevrolet"
        out.append(tok)
    return out


def _extract_year(text):
    m = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", text or "")
    if m:
        return m.group(1)
    m = re.search(r"['’]?([5-7][0-9])(?![0-9])", text or "")
    if m:
        return "19" + m.group(1)
    return None


def _field_text(fields, preferred_keys):
    """Best vehicle/bike description from Kit custom fields."""
    if not isinstance(fields, dict):
        return ""
    lowered = {str(k).lower(): v for k, v in fields.items()}
    for key in preferred_keys:
        val = lowered.get(key)
        if val:
            return str(val)
    parts = [str(v) for v in fields.values() if v]
    return " ".join(parts)


def _match_catalog(brand_owner, text):
    """Score catalog titles against the subscriber's vehicle text.

    Deterministic: exact year (when given) is a hard gate, then make
    overlap, then model-token overlap; ties break on fewer title tokens,
    then SKU. Returns the winning product or None."""
    if not text:
        return None
    year = _extract_year(text)
    in_tokens = [t for t in _tokens(text) if t not in _GENERIC_TOKENS]
    in_set = set(in_tokens)
    if not in_set:
        return None
    in_makes = []
    for tok in in_set:
        for grp in _MAKE_GROUPS:
            if tok in grp and grp not in in_makes:
                in_makes.append(grp)
    scored = []
    for product in _PRODUCTS.values():
        if product.get("owner") != brand_owner:
            continue
        if (product.get("fulfillment_type") or "").strip().lower() != "digital":
            continue
        if not product.get("digital_file"):
            continue
        title = product.get("title") or ""
        title_tokens = [t for t in _tokens(title) if t not in _GENERIC_TOKENS]
        title_set = set(title_tokens)
        title_year = _extract_year(title)
        if year and title_year and title_year != year:
            continue
        if year and not title_year:
            continue
        make_hit = bool(in_makes) and any(
            any(tok in grp for tok in title_set) for grp in in_makes)
        overlap = {t for t in in_set & title_set
                   if t != year and not any(t in grp for grp in _MAKE_GROUPS)}
        if not overlap:
            continue
        score = (100 if year else 0) + (25 if make_hit else 0) \
            + 10 * len(overlap)
        scored.append((-score, len(title_tokens), product["sku"], product))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return scored[0][3]


def resolve_product(brand, fields):
    """Returns (product, fallback_note). fallback_note is '' on an exact
    promise-kept match, or the honest explanation to put in the email."""
    if brand == "restorationessentials":
        car = _field_text(fields, ("car", "vehicle", "what_do_you_drive",
                                   "year_make_model", "make_model"))
        match = _match_catalog("restorationessentials", car)
        if match:
            return match, ""
        if car:
            note = (f"I couldn't match \u201c{car}\u201d to a catalogued "
                    "guide for that exact year, make, and model from the "
                    "details that came through, so I'm sending the Master "
                    "Restoration Checklist instead — it walks the whole job "
                    "in order for any car. Reply with your exact year, "
                    "make, and model and I'll send the guide built for "
                    "your car.")
        else:
            note = ("Your signup didn't include a car, so I'm sending "
                    "the Master Restoration Checklist — it walks the "
                    "whole restoration in order for any car. Reply with "
                    "your exact year, make, and model and I'll send the "
                    "guide built for your car.")
        return fallback_product(), note
    if brand == "ironhead":
        bike = _field_text(fields, ("bike", "motorcycle", "ride",
                                    "vehicle", "car"))
        note = ""
        if bike and "cb750" not in bike.lower():
            note = (f"Volume 1 covers the Honda CB750. You mentioned "
                    f"\u201c{bike}\u201d — your beta spot covers every "
                    "volume free as they follow (Harley, Triumph, "
                    "Kawasaki, and Yamaha), and Volume 1 is yours now.")
        return _PRODUCTS.get(IRONHEAD_SKU), note
    if brand == "stitchfolk":
        return _PRODUCTS.get(STITCHFOLK_SKU), ""
    if brand == "skillforge":
        return _PRODUCTS.get(SKILLFORGE_SKU), ""
    if brand == "everready-checklist":
        return ({"sku": CHECKLIST_SKU, "title": CHECKLIST_TITLE,
                 "owner": "everready", "fulfillment_type": "digital",
                 "digital_file": CHECKLIST_FILE,
                 "download_url": CHECKLIST_URL}, "")
    return None, ""


# ---------- file availability (never email a dead link) ----------

def _file_available(product):
    filename = os.path.basename(product.get("digital_file") or "")
    if not filename:
        return False
    backend = _STORAGE_FACTORY()
    if backend.exists(filename):
        return True
    # Repo-bundled copy (small files ship in data/digital with the deploy).
    if getattr(backend, "is_local", False):
        bundled = os.path.join(_ROOT_DIR, "data", "digital", filename)
        return os.path.isfile(bundled)
    return False


# ---------- the delivery itself ----------

_SUBJECTS = {
    "restorationessentials": "Your free RestoreEssentials beta guide",
    "ironhead": "Your free IronHead beta guide — Volume 1",
    "stitchfolk": "Your free Stitchfolk beta pattern",
    "skillforge": "Your free SkillForge Electricians AI Playbook",
    "everready-checklist": "Your Executor's First 30 Days Checklist",
}


def _email_bodies(brand, first_name, product, download_url, fallback_note):
    name = (first_name or "").strip() or "there"
    brand_name = BRAND_NAMES[brand]
    title = product.get("title") or product["sku"]
    text = [f"Hi {name},", ""]
    if brand == "everready-checklist":
        text += [
            "Here's the checklist you asked for — Executor's First 30 "
            "Days, the plain-English walk through the first month after "
            "losing someone:",
            "",
            download_url,
            "",
            "It's a free PDF — download it, print it, work it in order. "
            "If it helps, EverReady Family's full Executor's Kit lives "
            "at everready-family.com when you want the complete system.",
            "",
            "Take care,",
            "Bill Morton",
            "EverReady Family",
        ]
    else:
        text += [
            f"Thanks for joining the {brand_name} beta crew. Here's the "
            f"file promised on the signup page:",
            "",
            f"{title}",
            download_url,
            "",
        ]
        if fallback_note:
            text += [fallback_note, ""]
        if brand == "ironhead":
            text += [
                "Beta readers get every volume free: Volume 1 (Honda "
                "CB750) is yours at the link above, and you're on the "
                "beta list for the Harley, Triumph, Kawasaki, and Yamaha "
                "volumes as they follow.",
                "",
            ]
        if brand == "stitchfolk":
            ask = ("Work it like a tester: note anything unclear, any "
                   "stitch count that doesn't add up, any step you had "
                   "to guess at — then reply to this email with your "
                   "honest notes. That's the exchange, and it makes the "
                   "pattern better for everyone after you.")
        elif brand == "skillforge":
            ask = ("Run it on real jobs for two weeks, then reply to "
                   "this email and tell me straight whether it saves "
                   "time — estimating, invoicing, callbacks, the lot. "
                   "Honest notes are the whole exchange.")
        else:
            ask = ("Read it like an owner: mark anything unclear, "
                   "wrong, or missing, then reply to this email with "
                   "your honest feedback. That's the exchange — your "
                   "notes make the next edition better.")
        text += [
            ask,
            "",
            "Thanks,",
            "Bill Morton",
            brand_name,
            "",
            "This private link is tied to your email address and "
            "expires in 7 days. If it ever stops working, just reply "
            "and I'll get you sorted.",
        ]
    text_body = "\n".join(text)
    html_parts = [f"<p>{html.escape(line)}</p>" if line else ""
                  for line in text]
    # Make the bare URL clickable in the HTML copy.
    html_body = "\n".join(html_parts).replace(
        html.escape(download_url),
        f'<a href="{html.escape(download_url)}">'
        f"{html.escape(download_url)}</a>")
    return _SUBJECTS[brand], html_body, text_body


def deliver(email, brand, fields=None, first_name="", source="trigger"):
    """Resolve, record, and email one beta delivery. Idempotent per
    (email, brand, sku): a repeat trigger never sends a second copy.
    Returns (http_status, payload_dict)."""
    email = (email or "").strip().lower()
    fields = fields if isinstance(fields, dict) else {}
    if not _EMAIL_RE.match(email):
        return 400, {"ok": False, "error": "a valid email is required"}
    product, fallback_note = resolve_product(brand, fields)
    if product is None:
        return 503, {"ok": False, "error":
                     f"promised file for brand {brand} is not catalogued",
                     "brand": brand}
    sku = product["sku"]
    brand_name = BRAND_NAMES[brand]

    is_checklist = brand == "everready-checklist"
    if is_checklist:
        token = None
        download_url = product["download_url"]
    else:
        if not product.get("digital_file"):
            return 503, {"ok": False, "error":
                         f"promised file for {sku} has no deliverable",
                         "brand": brand, "sku": sku}
        try:
            signer = _SIGNER_FACTORY()
        except digital_mod.DigitalConfigError as e:
            return 503, {"ok": False, "error": str(e), "brand": brand}
        token = signer.mint(sku, email)
        download_url = f"{_PUBLIC_BASE_URL()}/download/{token}"

    row, state = _claim(email, brand, product, token, source, fields)
    if state == "already_delivered":
        return 200, {"ok": True, "status": "already_delivered",
                     "brand": brand, "sku": sku}
    if state == "in_progress":
        return 202, {"ok": True, "status": "in_progress",
                     "brand": brand, "sku": sku}

    if not is_checklist:
        try:
            available = _file_available(product)
        except storage_mod.StorageError as e:
            _finish(email, brand, sku, "failed", error=str(e))
            return 503, {"ok": False, "error": f"storage check failed: {e}",
                         "brand": brand, "sku": sku}
        if not available:
            _finish(email, brand, sku, "failed",
                    error="deliverable file not available in storage")
            return 503, {"ok": False, "error": "deliverable file not "
                         "available in storage", "brand": brand, "sku": sku}

    subject, html_body, text_body = _email_bodies(
        brand, first_name, product, download_url, fallback_note)
    sender = _SENDER_FACTORY(sender_name=brand_name)
    if getattr(sender, "dry_run", False):
        _finish(email, brand, sku, "dry_run",
                error="BREVO_API_KEY not set — email not sent")
        return 503, {"ok": False, "error": "delivery email is not "
                     "configured (BREVO_API_KEY missing); email not sent",
                     "brand": brand, "sku": sku}
    try:
        result = sender.send(email, subject, html_body, text_body) or {}
    except digital_mod.DigitalSendError as e:
        _finish(email, brand, sku, "failed", error=str(e))
        return 502, {"ok": False, "error": str(e), "brand": brand, "sku": sku}
    message_id = (result.get("messageId") or result.get("message_id")
                  or result.get("id"))
    _finish(email, brand, sku, "sent", message_id=message_id)
    log.info("beta delivery sent: brand=%s sku=%s to=%s", brand, sku, email)
    return 200, {"ok": True, "status": "sent", "brand": brand, "sku": sku,
                 "file": os.path.basename(product.get("digital_file") or ""),
                 "message_id": message_id}


# ---------- trigger payload handling ----------

def _brand_from_event(data):
    tag = data.get("tag") or {}
    tag_id = tag.get("id") if isinstance(tag, dict) else None
    if tag_id in TAG_TO_BRAND:
        return TAG_TO_BRAND[tag_id]
    form = data.get("form") or {}
    if isinstance(form, dict):
        for key in ("id", "uid"):
            if str(form.get(key)) in FORM_TO_BRAND:
                return FORM_TO_BRAND[str(form.get(key))]
    return None


def _handle_event(event):
    """One normalized event -> one delivery attempt (or an honest skip)."""
    if not isinstance(event, dict):
        return {"ok": False, "error": "event is not an object"}
    etype = event.get("type") or event.get("name") or ""
    data = event.get("data") if isinstance(event.get("data"), dict) else event
    if etype and etype not in ("subscriber.tag_added",
                               "subscriber.subscribed_to_form",
                               "subscriber.form_subscribe",
                               "beta_deliver"):
        return {"ok": True, "status": "ignored", "type": etype}
    subscriber = data.get("subscriber") or {}
    if not isinstance(subscriber, dict):
        subscriber = {}
    email = (subscriber.get("email_address") or subscriber.get("email")
             or data.get("email_address") or data.get("email") or "")
    brand = _brand_from_event(data)
    if brand is None:
        raw_brand = data.get("brand") or event.get("brand") or ""
        brand = BRAND_ALIASES.get(str(raw_brand).strip().lower())
    if not brand:
        return {"ok": True, "status": "ignored",
                "reason": "tag/form not a beta funnel"}
    fields = subscriber.get("fields") or data.get("fields") or {}
    first_name = subscriber.get("first_name") or data.get("first_name") or ""
    status, payload = deliver(email, brand, fields=fields,
                              first_name=first_name,
                              source=f"kit:{etype or 'direct'}")
    payload["http_status"] = status
    return payload


@bp.post("/api/beta-deliver")
def beta_deliver():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    status, payload = deliver(
        data.get("email") or "", _brand(data),
        fields=data.get("fields") or {},
        first_name=data.get("first_name") or "",
        source=data.get("source") or "poller")
    return jsonify(payload), status


def _brand(data):
    raw = str(data.get("brand") or "").strip().lower()
    return BRAND_ALIASES.get(raw, raw)


@bp.post("/api/kit-beta-webhook")
@bp.post("/api/kit-beta-webhook/<path_token>")
def kit_beta_webhook(path_token=None):
    if not _authorized(path_token):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    events = data.get("events")
    if not isinstance(events, list):
        events = [data]
    results = [_handle_event(event) for event in events]
    worst = 200
    for result in results:
        code = result.get("http_status", 200)
        if code >= 400:
            worst = code
    return jsonify({"ok": worst < 400, "results": results}), worst


# ---------- wiring ----------

def init(app, root_dir, products_by_sku, public_base_url, digital_files_dir,
         sender_factory=None, signer_factory=None, storage_factory=None):
    """Wire the beta-delivery blueprint into the Flask app. Called once
    from app.py after the catalog and leads DB exist."""
    global _PRODUCTS, _ROOT_DIR, _DIGITAL_FILES_DIR, _PUBLIC_BASE_URL
    global _SENDER_FACTORY, _SIGNER_FACTORY, _STORAGE_FACTORY
    _PRODUCTS = products_by_sku
    _ROOT_DIR = root_dir
    _DIGITAL_FILES_DIR = digital_files_dir
    _PUBLIC_BASE_URL = public_base_url
    _SENDER_FACTORY = sender_factory or (
        lambda sender_name=None: digital_mod.BrevoSender(
            sender_name=sender_name))
    _SIGNER_FACTORY = signer_factory or digital_mod.DownloadTokenSigner
    _STORAGE_FACTORY = storage_factory or (
        lambda: storage_mod.get_storage(_DIGITAL_FILES_DIR()))
    init_db()
    app.register_blueprint(bp)
