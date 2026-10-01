"""
PushRod wholesale partner program — accounts, applications, approval,
partner pricing, and minimums enforcement.

Persistence: SQLite at WHOLESALE_DB (defaults to /var/data/wholesale.db when
the Render disk is mounted, else <root>/data/wholesale.db for local dev).
Resale-certificate uploads go to WHOLESALE_CERT_DIR (same disk rule).

Agreement acceptance is a REQUIRED CHECKBOX on the application form —
recorded with timestamp, client IP, and agreement version on the
application row. Per Bill 2026-09-30 the agreement is FINAL as written
on his own authority (attorney review waived); there is no e-signature
integration — checkbox acceptance is the agreement method.
"""
import hashlib
import hmac
import logging
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone

import stripe
from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

log = logging.getLogger("pushrod.wholesale")

# ---------- program constants (from the program guide 2026-09-30) ----------
WHOLESALE_TYPES = {"tee", "hat", "sweatshirt"}  # the 3 launch blanks
BLANK_NAMES = {
    "tee": "Comfort Colors 1717",
    "hat": "Yupoong 6606",
    "sweatshirt": "Gildan 18000",
}
WHOLESALE_FACTOR = 0.80            # 20% off retail, flat — no tiers
BULK_MIN_PER_BLANK = 25           # 25+ units per base product, non-negotiable
OPENING_MIN_UNITS = 48            # opening order: 48+ units ...
OPENING_MIN_CENTS = 50000         # ... or $500+ merchandise
BULK_SHIP_CENTS = {               # bulk-shipping estimate per unit (guide §3)
    "tee": 116, "hat": 100, "sweatshirt": 200,
}
AGREEMENT_VERSION = "2026-09-30"  # final per Bill 2026-09-30 (attorney review waived)

CERT_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
MAX_CERT_BYTES = 10 * 1024 * 1024

# Wired by init(): product lookup, stripe context, publishable key.
_get_product = None
_stripe_ready = False
_stripe_acct = lambda: {}  # noqa: E731
_publishable_key = ""
ADMIN_TOKEN = ""
CERT_DIR = ""
DB_PATH = ""

bp = Blueprint("wholesale", __name__)


# ---------- pricing / minimums (pure logic — unit-tested) ----------
def wholesale_unit_cents(retail_cents):
    """20% off retail, rounded to the cent. 2600 -> 2080, 2400 -> 1920."""
    return int(round(retail_cents * WHOLESALE_FACTOR))


def blank_for_type(ptype):
    return BLANK_NAMES.get((ptype or "").strip().lower())


def is_wholesale_eligible(product):
    return bool(product) and product.get("type") in WHOLESALE_TYPES \
        and bool(product.get("purchasable"))


def bulk_shipping_cents(lines):
    """lines: [{type, qty}] -> total bulk-shipping estimate in cents."""
    total = 0
    for l in lines:
        total += BULK_SHIP_CENTS.get(l["type"], 0) * l["qty"]
    return total


def check_wholesale_minimums(lines, is_first_order):
    """lines: [{type, qty, unit_cents}]. Returns a list of error strings."""
    errors = []
    per_type = Counter()
    for l in lines:
        per_type[l["type"]] += l["qty"]
    for t, q in sorted(per_type.items()):
        if q < BULK_MIN_PER_BLANK:
            errors.append(
                f"{blank_for_type(t) or t}: {q} units — "
                f"wholesale orders need {BULK_MIN_PER_BLANK}+ units per base product "
                f"(designs and colors may mix within the blank)")
    if is_first_order:
        total_units = sum(per_type.values())
        merch_cents = sum(l["unit_cents"] * l["qty"] for l in lines)
        if total_units < OPENING_MIN_UNITS and merch_cents < OPENING_MIN_CENTS:
            errors.append(
                f"Opening order must total {OPENING_MIN_UNITS}+ units or "
                f"${OPENING_MIN_CENTS // 100}+ in merchandise "
                f"(yours: {total_units} units, ${merch_cents / 100:.2f})")
    return errors


# ---------- persistence ----------
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(CERT_DIR, exist_ok=True)
    with _db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            legal_name TEXT NOT NULL, dba TEXT DEFAULT '',
            business_type TEXT DEFAULT '', ein TEXT NOT NULL,
            address TEXT NOT NULL, phone TEXT DEFAULT '',
            contact_name TEXT NOT NULL, contact_email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            resale_permits TEXT NOT NULL, cert_path TEXT NOT NULL,
            license_info TEXT DEFAULT '',
            business_kind TEXT DEFAULT '', website TEXT NOT NULL,
            storefront_proof TEXT DEFAULT '',
            channels TEXT DEFAULT '', categories TEXT DEFAULT '',
            volume_estimate TEXT DEFAULT '', trade_refs TEXT DEFAULT '',
            payment_method_id TEXT DEFAULT '',
            accept_terms INTEGER NOT NULL DEFAULT 0,
            accepted_at TEXT DEFAULT '', accepted_ip TEXT DEFAULT '',
            agreement_version TEXT DEFAULT '',
            decision_at TEXT DEFAULT '', decision_note TEXT DEFAULT ''
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT UNIQUE,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            legal_name TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            has_ordered INTEGER NOT NULL DEFAULT 0,
            payment_method_id TEXT DEFAULT ''
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS wholesale_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT NOT NULL,
            stripe_session_id TEXT NOT NULL,
            total_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )""")
    log.info("wholesale db ready: %s (certs: %s)", DB_PATH, CERT_DIR)


def current_partner():
    pid = session.get("wholesale_partner_id")
    if not pid:
        return None
    with _db() as c:
        row = c.execute("SELECT * FROM partners WHERE partner_id = ?",
                        (pid,)).fetchone()
    return dict(row) if row else None


def record_wholesale_order(partner_id, stripe_session_id, total_cents):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        c.execute(
            "INSERT INTO wholesale_orders (partner_id, stripe_session_id, "
            "total_cents, created_at) VALUES (?,?,?,?)",
            (partner_id, stripe_session_id, total_cents, now))
        c.execute("UPDATE partners SET has_ordered = 1 WHERE partner_id = ?",
                  (partner_id,))


# ---------- API ----------
@bp.get("/api/wholesale/me")
def api_me():
    p = current_partner()
    if not p:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "partner_id": p["partner_id"],
                    "email": p["email"]})


@bp.get("/api/wholesale/catalog")
def api_catalog():
    """Partner-priced catalog for the logged-in approved partner."""
    p = current_partner()
    if not p:
        return jsonify({"error": "partner login required"}), 401
    out = []
    for sku, prod in _all_products().items():
        if not is_wholesale_eligible(prod):
            continue
        retail_cents = int(round(prod["price"]["amount"] * 100))
        out.append({
            "sku": sku, "title": prod["title"], "type": prod["type"],
            "blank": blank_for_type(prod["type"]),
            "base_color": prod.get("base_color", ""),
            "image_url": prod.get("image_url", ""),
            "needs_size": bool(prod.get("needs_size")),
            "retail_cents": retail_cents,
            "wholesale_cents": wholesale_unit_cents(retail_cents),
            "bulk_ship_cents": BULK_SHIP_CENTS[prod["type"]],
        })
    return jsonify({"partner_id": p["partner_id"], "products": out})


def _all_products():
    return _get_product("__all__") or {}


@bp.post("/api/wholesale/setup-intent")
def api_setup_intent():
    """Stripe SetupIntent so the application form can tokenize the partner's
    card with Stripe.js — the server only ever sees a payment_method id,
    never raw card numbers."""
    if not _stripe_ready:
        return jsonify({"error": "Stripe test key not configured"}), 503
    try:
        si = stripe.SetupIntent.create(usage="off_session", **_stripe_acct())
    except stripe.error.StripeError as e:
        msg = getattr(e, "user_message", None) or str(e) or "setup failed"
        return jsonify({"error": f"Stripe error: {msg}"}), 502
    return jsonify({"client_secret": si.client_secret,
                    "publishable_key": _publishable_key})


REQUIRED_FIELDS = ["legal_name", "ein", "address", "contact_name",
                   "contact_email", "password", "resale_permits", "website"]


@bp.post("/api/wholesale/apply")
def api_apply():
    form = request.form
    missing = [f for f in REQUIRED_FIELDS if not (form.get(f) or "").strip()]
    if missing:
        return jsonify({"error": "missing required fields: " +
                                 ", ".join(missing)}), 400
    if not form.get("accept_terms"):
        return jsonify({"error": "you must accept the Terms, MAP policy, "
                                 "and brand guidelines to apply"}), 400
    email = form["contact_email"].strip().lower()
    if "@" not in email:
        return jsonify({"error": "contact email looks invalid"}), 400
    cert = request.files.get("resale_cert")
    if not cert or not cert.filename:
        return jsonify({"error": "a signed resale certificate upload is "
                                 "required — no cert, no wholesale account"}), 400
    ext = cert.filename.rsplit(".", 1)[-1].lower()
    if ext not in CERT_EXTENSIONS:
        return jsonify({"error": "resale certificate must be PDF, JPG, or PNG"}), 400
    cert_bytes = cert.read(MAX_CERT_BYTES + 1)
    if len(cert_bytes) > MAX_CERT_BYTES:
        return jsonify({"error": "resale certificate exceeds 10 MB"}), 400

    now = datetime.now(timezone.utc).isoformat()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ip = ip.split(",")[0].strip()
    with _db() as c:
        cur = c.execute(
            """INSERT INTO applications
               (created_at, legal_name, dba, business_type, ein, address, phone,
                contact_name, contact_email, password_hash, resale_permits,
                cert_path, license_info, business_kind, website, storefront_proof,
                channels, categories, volume_estimate, trade_refs,
                payment_method_id, accept_terms, accepted_at, accepted_ip,
                agreement_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, form.get("legal_name", "").strip(), form.get("dba", "").strip(),
             form.get("business_type", "").strip(), form.get("ein", "").strip(),
             form.get("address", "").strip(), form.get("phone", "").strip(),
             form.get("contact_name", "").strip(), email,
             generate_password_hash(form["password"]),
             form.get("resale_permits", "").strip(),
             "",  # cert_path filled below
             form.get("license_info", "").strip(),
             form.get("business_kind", "").strip(),
             form.get("website", "").strip(),
             form.get("storefront_proof", "").strip(),
             form.get("channels", "").strip(),
             form.get("categories", "").strip(),
             form.get("volume_estimate", "").strip(),
             form.get("trade_refs", "").strip(),
             form.get("payment_method_id", "").strip(),
             1, now, ip, AGREEMENT_VERSION))
        app_id = cur.lastrowid
        cert_name = f"app-{app_id}_{secure_filename(cert.filename)}"
        cert_path = os.path.join(CERT_DIR, cert_name)
        with open(cert_path, "wb") as f:
            f.write(cert_bytes)
        c.execute("UPDATE applications SET cert_path = ? WHERE id = ?",
                  (cert_path, app_id))
    log.info("wholesale application #%d from %s (%s)", app_id, email, ip)
    return jsonify({"ok": True, "application_id": app_id,
                    "status": "pending",
                    "message": "Application received — decision within 2 business days."})


def _require_admin():
    """Returns None when the request is authorized, else an error response."""
    if not ADMIN_TOKEN:
        return jsonify({"error": "wholesale admin not configured"}), 503
    if not hmac.compare_digest(request.headers.get("X-Admin-Token", ""),
                               ADMIN_TOKEN):
        return jsonify({"error": "unauthorized"}), 401
    return None


# Login brute-force throttle: (ip -> [attempt_count, window_start]).
# Low-traffic B2B endpoint; in-memory is fine (resets on restart).
_login_attempts = {}


def _login_throttled():
    ip = request.remote_addr or "?"
    now = time.time()
    count, start = _login_attempts.get(ip, (0, now))
    if now - start > 300:
        count, start = 0, now
    if count >= 10:
        return True
    _login_attempts[ip] = (count + 1, start)
    return False


@bp.get("/api/admin/wholesale/applications")
def admin_list():
    denied = _require_admin()
    if denied:
        return denied
    status = request.args.get("status", "")
    q = "SELECT id, created_at, status, legal_name, dba, contact_name, " \
        "contact_email, website, channels, volume_estimate, agreement_version " \
        "FROM applications"
    args = ()
    if status in ("pending", "approved", "rejected"):
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY id DESC"
    with _db() as c:
        rows = c.execute(q, args).fetchall()
    return jsonify({"applications": [dict(r) for r in rows]})


@bp.get("/api/admin/wholesale/applications/<int:app_id>")
def admin_detail(app_id):
    denied = _require_admin()
    if denied:
        return denied
    with _db() as c:
        row = c.execute("SELECT * FROM applications WHERE id = ?",
                        (app_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    d = dict(row)
    d.pop("password_hash", None)  # never expose the hash
    return jsonify(d)


@bp.post("/api/admin/wholesale/applications/<int:app_id>/approve")
def admin_approve(app_id):
    denied = _require_admin()
    if denied:
        return denied
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        app_row = c.execute("SELECT * FROM applications WHERE id = ?",
                            (app_id,)).fetchone()
        if not app_row:
            return jsonify({"error": "not found"}), 404
        if app_row["status"] != "pending":
            return jsonify({"error": f"already {app_row['status']}"}), 400
        # The status='pending' guard on the UPDATE closes the double-approval
        # race: a concurrent approval of the same app gets rowcount 0.
        updated = c.execute(
            "UPDATE applications SET status='approved', decision_at=? "
            "WHERE id = ? AND status='pending'", (now, app_id)).rowcount
        if not updated:
            return jsonify({"error": "already decided by another admin"}), 409
        try:
            cur = c.execute(
                """INSERT INTO partners
                   (email, password_hash, legal_name, approved_at, payment_method_id)
                   VALUES (?,?,?,?,?)""",
                (app_row["contact_email"], app_row["password_hash"],
                 app_row["legal_name"], now, app_row["payment_method_id"]))
        except sqlite3.IntegrityError:
            return jsonify({"error": "a partner with this email already "
                                     "exists"}), 409
        partner_id = f"PTNR-{cur.lastrowid:06d}"
        c.execute("UPDATE partners SET partner_id = ? WHERE id = ?",
                  (partner_id, cur.lastrowid))
    # No welcome email is sent — Bill handles partner outreach. The admin view
    # shows the partner ID; the partner logs in with the email + password
    # they chose on the application.
    log.info("wholesale application #%d approved -> %s", app_id, partner_id)
    return jsonify({"ok": True, "partner_id": partner_id,
                    "email": app_row["contact_email"]})


@bp.post("/api/admin/wholesale/applications/<int:app_id>/reject")
def admin_reject(app_id):
    denied = _require_admin()
    if denied:
        return denied
    note = (request.get_json(silent=True) or {}).get("note", "")
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        row = c.execute("SELECT status FROM applications WHERE id = ?",
                        (app_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        if row["status"] != "pending":
            return jsonify({"error": f"already {row['status']}"}), 400
        c.execute("UPDATE applications SET status='rejected', decision_at=?, "
                  "decision_note=? WHERE id = ?", (now, note[:500], app_id))
    log.info("wholesale application #%d rejected", app_id)
    return jsonify({"ok": True})


@bp.post("/api/wholesale/login")
def api_login():
    if _login_throttled():
        return jsonify({"error": "too many attempts, try again later"}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    with _db() as c:
        row = c.execute("SELECT * FROM partners WHERE email = ?",
                        (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        # Same response for unknown email vs wrong password: no enumeration.
        return jsonify({"error": "invalid email or password"}), 401
    session.clear()  # rotate the session on login (fixation defense)
    session["wholesale_partner_id"] = row["partner_id"]
    return jsonify({"ok": True, "partner_id": row["partner_id"]})


@bp.post("/api/wholesale/logout")
def api_logout():
    session.pop("wholesale_partner_id", None)
    return jsonify({"ok": True})


def init(app, get_product, stripe_ready, stripe_acct, publishable_key,
         root_dir):
    """Wire the wholesale blueprint into the Flask app. Called once from app.py."""
    global _get_product, _stripe_ready, _stripe_acct, _publishable_key
    global ADMIN_TOKEN, CERT_DIR, DB_PATH
    _get_product = get_product
    _stripe_ready = stripe_ready
    _stripe_acct = stripe_acct
    _publishable_key = publishable_key
    ADMIN_TOKEN = os.environ.get("WHOLESALE_ADMIN_TOKEN", "")
    data_root = "/var/data" if os.path.isdir("/var/data") else \
        os.path.join(root_dir, "data")
    DB_PATH = os.environ.get("WHOLESALE_DB",
                             os.path.join(data_root, "wholesale.db"))
    CERT_DIR = os.environ.get("WHOLESALE_CERT_DIR",
                              os.path.join(data_root, "certs"))
    secret = os.environ.get("WHOLESALE_SESSION_SECRET")
    if not secret:
        log.warning("WHOLESALE_SESSION_SECRET not set — using an ephemeral "
                    "secret; partner sessions will not survive restarts. "
                    "Set it in the Render dashboard.")
        secret = hashlib.sha256(os.urandom(32)).hexdigest()
    app.secret_key = secret
    init_db()
    app.register_blueprint(bp)
    # Cheap sha of the token for ops logs without ever printing it.
    log.info("wholesale wired: admin %s, agreement %s",
             "configured" if ADMIN_TOKEN else "NOT CONFIGURED (set WHOLESALE_ADMIN_TOKEN)",
             AGREEMENT_VERSION)
