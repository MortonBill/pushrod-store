"""
Auction engine for RestorationEssentials (RE) + IronHead (IH) — Slice 1.

Spec: ~/workspace/your_files/business/auction-engine-spec-claude-2026-10-02.md
§1 (data model) + render-migration-plan-2026-10-02.md §4. NOTE: the saved
spec file ends mid-§1.12 — §§2–9 (proxy-bid algorithm, closer, pay pages,
test-gate, slice ordering) were never written to disk (Claude output cap).
Slice 1 is therefore built exactly as scoped by the migration plan §4 and
this module's charter; later slices extend it.

Slice 1 scope (this module):
  * Data layer — full §1 schema. Production source of truth is Postgres
    (auctions_schema.sql). Runtime follows the repo convention set by
    wholesale.py: SQLite on the Render disk locally, Postgres when
    AUCTIONS_DATABASE_URL / DATABASE_URL is set. One code path; dialect
    differences are contained in _adapt_params/_adapt_row.
  * Lot lifecycle state machine (spec §1.3 transition table). Contradictory
    states are unrepresentable: there is no reserve_met flag — the CLOSED
    fork derives the outcome and atomically writes winner + status, and the
    chk_winner_consistency CHECK backstops it at the database layer.
  * Seller submission -> moderation queue -> scheduled -> live, where LIVE
    is reachable ONLY through attempt_go_live(), which runs the public-
    render smoke check first. "Scheduled" can never read as "live" (the
    exact Polsia failure this engine replaces).
  * Minimal JSON API + session auth for the flow above.

NOT in Slice 1 (later slices): bid placement / proxy engine / soft close,
the closer cron, pay-page tokens + Stripe Checkout, second-chance ladder,
settlement recording UI, notifications delivery, email verification flow
(accounts start unverified; bids will require email_verified_at).

Persistence: AUCTIONS_DB (defaults to /var/data/auctions.db when the Render
disk is mounted, else <root>/data/auctions.db) — same disk rule as wholesale.
"""
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("pushrod.auctions")

bp = Blueprint("auctions", __name__)

BRANDS = ("RE", "IH")
SESSION_KEY = "auctions_account_id"
EXTENSION_CAP_MINUTES = 120  # spec §1.6 chk_extension_cap

# ---------------------------------------------------------------------------
# Lifecycle state machine (spec §1.3 — ALLOWED TRANSITIONS)
# ---------------------------------------------------------------------------
TRANSITIONS = {
    "DRAFT": {"IN_MODERATION", "CANCELLED"},
    "IN_MODERATION": {"SCHEDULED", "REJECTED", "DRAFT", "CANCELLED"},
    "REJECTED": {"DRAFT"},
    "SCHEDULED": {"RENDER_CHECK", "CANCELLED"},
    "RENDER_CHECK": {"LIVE", "RENDER_FAILED"},
    "RENDER_FAILED": {"SCHEDULED", "CANCELLED"},
    "LIVE": {"CLOSED"},
    "CLOSED": {"NO_SALE", "INVOICED"},
    "NO_SALE": {"RELISTED"},
    "INVOICED": {"PAID", "RELISTED"},
    "PAID": {"SETTLED"},
    "SETTLED": set(),
    "RELISTED": {"DRAFT"},
    "CANCELLED": set(),
}
STATUSES = tuple(TRANSITIONS)

POST_WIN_STATUSES = {"INVOICED", "PAID", "SETTLED"}

# Moderation audit action per (from_status, to_status); moves absent here are
# engine events (close/invoice/pay/settle), not moderation actions.
_AUDIT_BY_TRANSITION = {
    ("DRAFT", "IN_MODERATION"): "SUBMITTED",
    ("IN_MODERATION", "SCHEDULED"): "APPROVED",
    ("RENDER_FAILED", "SCHEDULED"): "EDITED",       # reschedule after fix
    ("IN_MODERATION", "REJECTED"): "REJECTED",
    ("IN_MODERATION", "DRAFT"): "SENT_BACK",
    ("REJECTED", "DRAFT"): "EDITED",               # seller re-submission prep
    ("RELISTED", "DRAFT"): "EDITED",               # relist restarts lifecycle
    ("RENDER_CHECK", "LIVE"): "RENDER_PASS",
    ("RENDER_CHECK", "RENDER_FAILED"): "RENDER_FAIL",
}
# Any transition into CANCELLED audits as CANCELLED (handled in transition()).


class AuctionError(Exception):
    """Domain error; surfaced to API callers as a 400 with this message."""


class PermissionDenied(AuctionError):
    """Surfaced as 403."""


# ---------------------------------------------------------------------------
# Dialect-contained DB layer
# ---------------------------------------------------------------------------
_DIALECT = "sqlite"          # "sqlite" | "postgres"
_DB_PATH = ""                # sqlite file path
_DB_URL = ""                 # postgres URL
_BRAND_CODE = ""             # "RE" / "IH" when the service is single-brand
_ADMIN_TOKEN = ""
_render_check_fn = None      # callable(lot_row, public_dict) -> (ok, detail)

_UUID_RE_LEN = 36


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def _parse_ts(value):
    """Normalize a DB timestamp back to an aware datetime (or None)."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _looks_uuid(value):
    return (isinstance(value, str) and len(value) == _UUID_RE_LEN
            and value.count("-") == 4)


def _adapt_params(params):
    """Python values -> driver values for the active dialect."""
    out = []
    for p in params:
        if _DIALECT == "postgres":
            if isinstance(p, bool):
                out.append(p)
            elif _looks_uuid(p):
                out.append(uuid.UUID(p))
            elif isinstance(p, (list, dict)):
                out.append(json.dumps(p))
            else:
                out.append(p)  # datetime passes natively; text/ints fine
        else:  # sqlite
            if isinstance(p, bool):
                out.append(1 if p else 0)
            elif isinstance(p, datetime):
                out.append(p.isoformat())
            elif isinstance(p, (list, dict)):
                out.append(json.dumps(p))
            else:
                out.append(p)
    return tuple(out)


def _adapt_row(row):
    if row is None:
        return None
    out = dict(row)
    for key, value in out.items():
        if isinstance(value, uuid.UUID):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _sql(sqlite_sql):
    """Translate ? placeholders for Postgres; sqlite passes through."""
    return sqlite_sql.replace("?", "%s") if _DIALECT == "postgres" else sqlite_sql


class _Conn:
    """Thin connection wrapper: one API over sqlite3 / psycopg.

    Use as a context manager; commits on clean exit, rolls back on error,
    closes always. execute() returns a cursor-like object whose fetchone /
    fetchall yield plain dicts.
    """

    def __init__(self):
        self._raw = None
        self._cur = None

    def __enter__(self):
        if _DIALECT == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            self._raw = psycopg.connect(_DB_URL, row_factory=dict_row)
            self._cur = self._raw.cursor()
        else:
            self._raw = sqlite3.connect(_DB_PATH)
            self._raw.row_factory = sqlite3.Row
            self._cur = self._raw.cursor()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False

    def execute(self, sqlite_sql, params=()):
        self._cur.execute(_sql(sqlite_sql), _adapt_params(params))
        return self

    def fetchone(self):
        return _adapt_row(self._cur.fetchone())

    def fetchall(self):
        return [_adapt_row(r) for r in self._cur.fetchall()]

    @property
    def lastrowid(self):
        return self._cur.lastrowid


def _connect():
    return _Conn()


# ---------------------------------------------------------------------------
# Schema — SQLite mirror of auctions_schema.sql (same tables/invariants;
# enums become TEXT + CHECK, uuid becomes TEXT, timestamptz becomes ISO text,
# generated columns become app-computed where SQLite can't express them).
# ---------------------------------------------------------------------------
_SQLITE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        brand TEXT NOT NULL CHECK (brand IN ('RE','IH')),
        email TEXT NOT NULL,
        email_verified_at TEXT,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        is_seller INTEGER NOT NULL DEFAULT 0,
        is_suspended INTEGER NOT NULL DEFAULT 0,
        stripe_customer_id TEXT,
        stripe_connect_account_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CONSTRAINT uq_accounts_brand_email UNIQUE (brand, email)
    )""",
    """
    CREATE TABLE IF NOT EXISTS lots (
        id TEXT PRIMARY KEY,
        brand TEXT NOT NULL CHECK (brand IN ('RE','IH')),
        seller_account_id TEXT NOT NULL REFERENCES accounts(id),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 3 AND 200),
        description TEXT NOT NULL,
        category TEXT NOT NULL,
        condition_notes TEXT,
        image_keys TEXT NOT NULL DEFAULT '[]',
        starting_price_cents INTEGER NOT NULL CHECK (starting_price_cents >= 0),
        reserve_price_cents INTEGER,
        current_price_cents INTEGER NOT NULL DEFAULT 0,
        leading_bidder_id TEXT REFERENCES accounts(id),
        scheduled_start_at TEXT,
        scheduled_close_at TEXT,
        current_close_at TEXT,
        extension_minutes_used INTEGER NOT NULL DEFAULT 0
            CHECK (extension_minutes_used >= 0),
        status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
            'DRAFT','IN_MODERATION','REJECTED','SCHEDULED','RENDER_CHECK',
            'RENDER_FAILED','LIVE','CLOSED','NO_SALE','INVOICED','PAID',
            'SETTLED','RELISTED','CANCELLED')),
        winner_account_id TEXT REFERENCES accounts(id),
        winning_price_cents INTEGER,
        moderated_by_id TEXT REFERENCES accounts(id),
        moderation_note TEXT,
        last_render_check_at TEXT,
        last_render_check_ok INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CONSTRAINT chk_winner_consistency CHECK (
            (status IN ('INVOICED','PAID','SETTLED')
                AND winner_account_id IS NOT NULL
                AND winning_price_cents IS NOT NULL
                AND winning_price_cents > 0)
            OR
            (status NOT IN ('INVOICED','PAID','SETTLED')
                AND winner_account_id IS NULL
                AND winning_price_cents IS NULL)
        ),
        CONSTRAINT chk_no_sale_no_winner CHECK (
            NOT (status = 'NO_SALE' AND winner_account_id IS NOT NULL)
        ),
        CONSTRAINT chk_reserve_gte_start CHECK (
            reserve_price_cents IS NULL
            OR reserve_price_cents >= starting_price_cents
        ),
        CONSTRAINT chk_extension_cap CHECK (extension_minutes_used <= 120),
        CONSTRAINT chk_seller_not_leader CHECK (
            leading_bidder_id IS NULL
            OR leading_bidder_id <> seller_account_id
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS bids (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        bidder_account_id TEXT NOT NULL REFERENCES accounts(id),
        max_bid_cents INTEGER NOT NULL CHECK (max_bid_cents > 0),
        effective_price_cents INTEGER NOT NULL CHECK (effective_price_cents > 0),
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE','OUTBID','VOIDED')),
        proxy_rank INTEGER,
        placed_at TEXT NOT NULL,
        voided_at TEXT,
        voided_by_id TEXT REFERENCES accounts(id),
        void_reason TEXT,
        triggered_extension INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        CONSTRAINT chk_void_fields CHECK (
            (status = 'VOIDED') = (voided_at IS NOT NULL)
        ),
        CONSTRAINT chk_effective_lte_max CHECK (
            effective_price_cents <= max_bid_cents
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS increment_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brand TEXT NOT NULL CHECK (brand IN ('RE','IH')),
        range_low_cents INTEGER NOT NULL CHECK (range_low_cents >= 0),
        range_high_cents INTEGER,
        increment_cents INTEGER NOT NULL CHECK (increment_cents > 0),
        effective_from TEXT NOT NULL,
        effective_to TEXT,
        CONSTRAINT uq_increment_brand_range
            UNIQUE (brand, range_low_cents, effective_from),
        CONSTRAINT chk_range_order CHECK (
            range_high_cents IS NULL OR range_high_cents > range_low_cents
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS invoices (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        winner_account_id TEXT NOT NULL REFERENCES accounts(id),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        status TEXT NOT NULL DEFAULT 'OPEN'
            CHECK (status IN ('OPEN','PAID','VOID')),
        stripe_payment_intent_id TEXT UNIQUE,
        stripe_checkout_session_id TEXT UNIQUE,
        stripe_customer_id TEXT,
        issued_at TEXT NOT NULL,
        paid_at TEXT,
        voided_at TEXT,
        void_reason TEXT,
        reminder_24h_sent_at TEXT,
        reminder_48h_sent_at TEXT,
        payment_deadline_at TEXT NOT NULL,
        CONSTRAINT uq_invoice_lot UNIQUE (lot_id),
        CONSTRAINT chk_paid_fields CHECK (
            (status = 'PAID') =
            (paid_at IS NOT NULL
                AND stripe_payment_intent_id IS NOT NULL
                AND stripe_checkout_session_id IS NOT NULL)
        ),
        CONSTRAINT chk_void_fields CHECK (
            (status = 'VOID') = (voided_at IS NOT NULL)
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS pay_page_tokens (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        invoice_id TEXT NOT NULL REFERENCES invoices(id),
        winner_account_id TEXT NOT NULL REFERENCES accounts(id),
        token_hash TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        revoked_at TEXT,
        revoke_reason TEXT,
        CONSTRAINT uq_pay_page_lot UNIQUE (lot_id),
        CONSTRAINT chk_revoke_fields CHECK (
            (revoked_at IS NULL) = (revoke_reason IS NULL)
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS settlements (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        invoice_id TEXT NOT NULL REFERENCES invoices(id),
        seller_account_id TEXT NOT NULL REFERENCES accounts(id),
        gross_amount_cents INTEGER NOT NULL CHECK (gross_amount_cents > 0),
        platform_fee_cents INTEGER NOT NULL CHECK (platform_fee_cents >= 0),
        seller_payout_cents INTEGER NOT NULL
            GENERATED ALWAYS AS (gross_amount_cents - platform_fee_cents) STORED,
        payout_method TEXT NOT NULL,
        payout_reference TEXT,
        payment_cleared_at TEXT NOT NULL,
        delivery_confirmed_at TEXT,
        buffer_release_at TEXT,
        released_at TEXT,
        stripe_transfer_id TEXT,
        notes TEXT,
        recorded_by_id TEXT NOT NULL REFERENCES accounts(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CONSTRAINT uq_settlement_lot UNIQUE (lot_id),
        CONSTRAINT chk_release_timeline CHECK (
            released_at IS NULL
            OR (delivery_confirmed_at IS NOT NULL
                AND released_at >= buffer_release_at)
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS second_chance_offers (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        original_invoice_id TEXT NOT NULL REFERENCES invoices(id),
        offeree_account_id TEXT NOT NULL REFERENCES accounts(id),
        offered_price_cents INTEGER NOT NULL CHECK (offered_price_cents > 0),
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
            'PENDING','ACCEPTED','DECLINED','EXPIRED','CANCELLED')),
        offered_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        responded_at TEXT,
        created_at TEXT NOT NULL,
        CONSTRAINT chk_offer_response CHECK (
            (status IN ('ACCEPTED','DECLINED')) = (responded_at IS NOT NULL)
        )
    )""",
    """
    CREATE TABLE IF NOT EXISTS moderation_actions (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        actor_account_id TEXT REFERENCES accounts(id),
        action TEXT NOT NULL CHECK (action IN (
            'SUBMITTED','CLAIMED','APPROVED','REJECTED','SENT_BACK','EDITED',
            'RENDER_PASS','RENDER_FAIL','CANCELLED')),
        note TEXT,
        created_at TEXT NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        lot_id TEXT REFERENCES lots(id),
        event TEXT NOT NULL CHECK (event IN (
            'OUTBID','WINNING','AUCTION_WON','INVOICE_ISSUED',
            'PAYMENT_REMINDER','PAYMENT_CONFIRMED','SECOND_CHANCE_OFFER',
            'SECOND_CHANCE_EXPIRED','LOT_RELISTED','LOT_CANCELLED',
            'RESERVE_NOT_MET')),
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        read_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lots_brand_status ON lots (brand, status)",
    "CREATE INDEX IF NOT EXISTS idx_lots_seller ON lots (seller_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_bids_lot_status ON bids (lot_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_mod_actions_lot ON moderation_actions (lot_id, created_at)",
]

# Spec §1.8 seed data.
_INCREMENT_SEED = [
    # RestorationEssentials (cars)
    ("RE", 0, 2500, 100), ("RE", 2500, 5000, 250), ("RE", 5000, 25000, 500),
    ("RE", 25000, 100000, 1000), ("RE", 100000, 500000, 2500),
    ("RE", 500000, 1000000, 5000), ("RE", 1000000, 2500000, 10000),
    ("RE", 2500000, None, 25000),
    # IronHead (motorcycle scale)
    ("IH", 0, 1000, 50), ("IH", 1000, 2500, 100), ("IH", 2500, 10000, 250),
    ("IH", 10000, 25000, 500), ("IH", 25000, 100000, 1000),
    ("IH", 100000, 250000, 2500), ("IH", 250000, None, 5000),
]


def _init_schema():
    if _DIALECT == "postgres":
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "auctions_schema.sql")
        with open(schema_path) as f:
            script = f.read()
        with _connect() as c:
            c.execute(script)
        log.info("auctions schema ready (postgres)")
        return
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    with _connect() as c:
        for stmt in _SQLITE_DDL:
            c.execute(stmt)
        today = _now().date().isoformat()
        for brand, low, high, inc in _INCREMENT_SEED:
            c.execute(
                "INSERT OR IGNORE INTO increment_rules "
                "(brand, range_low_cents, range_high_cents, increment_cents,"
                " effective_from) VALUES (?,?,?,?,?)",
                (brand, low, high, inc, today))
    log.info("auctions schema ready (sqlite): %s", _DB_PATH)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def create_account(brand, email, display_name, password,
                   is_seller=False, is_admin=False):
    brand = (brand or "").strip().upper()
    if brand not in BRANDS:
        raise AuctionError(f"brand must be one of {BRANDS}")
    email = (email or "").strip().lower()
    if "@" not in email:
        raise AuctionError("a valid email is required")
    if len(password or "") < 8:
        raise AuctionError("password must be at least 8 characters")
    now = _iso(_now())
    account_id = str(uuid.uuid4())
    with _connect() as c:
        existing = c.execute(
            "SELECT id FROM accounts WHERE brand = ? AND email = ?",
            (brand, email)).fetchone()
        if existing:
            raise AuctionError("an account with this email already exists")
        c.execute(
            "INSERT INTO accounts (id, brand, email, email_verified_at,"
            " display_name, password_hash, is_admin, is_seller, is_suspended,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, brand, email, None, display_name.strip(),
             generate_password_hash(password), bool(is_admin),
             bool(is_seller), False, now, now))
    return get_account(account_id)


def get_account(account_id):
    if not account_id:
        return None
    with _connect() as c:
        return c.execute("SELECT * FROM accounts WHERE id = ?",
                         (account_id,)).fetchone()


def authenticate(brand, email, password):
    with _connect() as c:
        row = c.execute(
            "SELECT * FROM accounts WHERE brand = ? AND email = ?",
            ((brand or "").strip().upper(), (email or "").strip().lower())
        ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password or ""):
        return None
    return row


def mark_email_verified(account_id):
    """Slice-1 helper (admin/test path). The buyer-facing verification email
    flow is a later slice; bids require email_verified_at regardless."""
    with _connect() as c:
        c.execute(
            "UPDATE accounts SET email_verified_at = ?, updated_at = ?"
            " WHERE id = ?", (_iso(_now()), _iso(_now()), account_id))


def public_account_dict(account):
    if not account:
        return None
    return {
        "id": account["id"],
        "brand": account["brand"],
        "email": account["email"],
        "display_name": account["display_name"],
        "email_verified": bool(account["email_verified_at"]),
        "is_seller": bool(account["is_seller"]),
        "is_admin": bool(account["is_admin"]),
        "is_suspended": bool(account["is_suspended"]),
    }


# ---------------------------------------------------------------------------
# Lots + lifecycle
# ---------------------------------------------------------------------------
def get_lot(lot_id):
    with _connect() as c:
        return c.execute("SELECT * FROM lots WHERE id = ?",
                         (lot_id,)).fetchone()


def _require_lot(c, lot_id):
    lot = c.execute("SELECT * FROM lots WHERE id = ?",
                    (lot_id,)).fetchone()
    if not lot:
        raise AuctionError("lot not found")
    return lot


def _audit(c, lot_id, actor_account_id, action, note=None):
    c.execute(
        "INSERT INTO moderation_actions (id, lot_id, actor_account_id,"
        " action, note, created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), lot_id, actor_account_id, action, note,
         _iso(_now())))


def moderation_history(lot_id):
    with _connect() as c:
        return c.execute(
            "SELECT * FROM moderation_actions WHERE lot_id = ?"
            " ORDER BY created_at ASC", (lot_id,)).fetchall()


def transition(lot_id, to_status, actor_account_id=None, note=None, **fields):
    """Move a lot along the spec §1.3 transition table, atomically with its
    moderation audit row. Raises AuctionError on any illegal move.

    Extra lot columns (schedule, winner, render-check result...) pass as
    **fields and are written in the same update. The winner invariant is
    enforced here AND by the chk_winner_consistency CHECK in the schema."""
    if to_status not in TRANSITIONS:
        raise AuctionError(f"unknown lot status: {to_status}")
    now = _iso(_now())
    with _connect() as c:
        lot = _require_lot(c, lot_id)
        current = lot["status"]
        if to_status not in TRANSITIONS[current]:
            raise AuctionError(
                f"illegal lot transition: {current} -> {to_status}")
        if to_status in POST_WIN_STATUSES:
            winner = fields.get("winner_account_id",
                                lot["winner_account_id"])
            price = fields.get("winning_price_cents",
                               lot["winning_price_cents"])
            if to_status == "INVOICED" and (not winner or not price
                                            or int(price) <= 0):
                raise AuctionError(
                    "INVOICED requires winner_account_id and a positive "
                    "winning_price_cents, written atomically with the status")
        updates = dict(fields)
        updates["status"] = to_status
        updates["updated_at"] = now
        cols = ", ".join(f"{k} = ?" for k in updates)
        c.execute(f"UPDATE lots SET {cols} WHERE id = ?",
                  (*updates.values(), lot_id))
        action = _AUDIT_BY_TRANSITION.get((current, to_status))
        if to_status == "CANCELLED":
            action = "CANCELLED"
        if action:
            _audit(c, lot_id, actor_account_id, action, note)
    return get_lot(lot_id)


def create_lot(seller_id, brand, title, description, category,
               starting_price_cents, reserve_price_cents=None,
               condition_notes=None, image_keys=None):
    """Seller creates a DRAFT lot. DRAFT = submitted by seller, not yet in
    the moderation queue (spec §1.3); submit_lot() puts it in the queue."""
    brand = (brand or "").strip().upper()
    if brand not in BRANDS:
        raise AuctionError(f"brand must be one of {BRANDS}")
    seller = get_account(seller_id)
    if not seller:
        raise AuctionError("seller account not found")
    if not seller["is_seller"]:
        raise PermissionDenied("only seller accounts can create lots")
    if seller["is_suspended"]:
        raise PermissionDenied("suspended accounts cannot create lots")
    if seller["brand"] != brand:
        raise AuctionError("lot brand must match the seller's brand")
    title = (title or "").strip()
    if not 3 <= len(title) <= 200:
        raise AuctionError("title must be 3-200 characters")
    if not (description or "").strip():
        raise AuctionError("description is required")
    if not (category or "").strip():
        raise AuctionError("category is required")
    starting = int(starting_price_cents)
    if starting < 0:
        raise AuctionError("starting price cannot be negative")
    reserve = None
    if reserve_price_cents is not None:
        reserve = int(reserve_price_cents)
        if reserve < starting:
            raise AuctionError(
                "reserve price must be >= the starting price")
    now = _iso(_now())
    lot_id = str(uuid.uuid4())
    with _connect() as c:
        c.execute(
            "INSERT INTO lots (id, brand, seller_account_id, title,"
            " description, category, condition_notes, image_keys,"
            " starting_price_cents, reserve_price_cents, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lot_id, brand, seller_id, title, description.strip(),
             category.strip(), condition_notes, list(image_keys or []),
             starting, reserve, now, now))
    return get_lot(lot_id)


def submit_lot(lot_id, actor_account_id):
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    if lot["seller_account_id"] != actor_account_id:
        raise PermissionDenied("only the seller can submit this lot")
    return transition(lot_id, "IN_MODERATION",
                      actor_account_id=actor_account_id)


def moderation_queue(brand=None):
    """Lots awaiting admin review (status IN_MODERATION), oldest first."""
    sql = "SELECT * FROM lots WHERE status = 'IN_MODERATION'"
    params = ()
    if brand:
        sql += " AND brand = ?"
        params = (brand.strip().upper(),)
    sql += " ORDER BY created_at ASC"
    with _connect() as c:
        return c.execute(sql, params).fetchall()


def claim_lot(lot_id, admin_account_id):
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    if lot["status"] != "IN_MODERATION":
        raise AuctionError("only lots in moderation can be claimed")
    with _connect() as c:
        c.execute(
            "UPDATE lots SET moderated_by_id = ?, updated_at = ? WHERE id = ?",
            (admin_account_id, _iso(_now()), lot_id))
        _audit(c, lot_id, admin_account_id, "CLAIMED")
    return get_lot(lot_id)


def approve_lot(lot_id, admin_account_id, scheduled_start_at,
                scheduled_close_at, note=None):
    start = _parse_ts(scheduled_start_at)
    close = _parse_ts(scheduled_close_at)
    if not start or not close:
        raise AuctionError(
            "approval requires scheduled_start_at and scheduled_close_at")
    if close <= start:
        raise AuctionError("scheduled close must be after scheduled start")
    return transition(
        lot_id, "SCHEDULED", actor_account_id=admin_account_id, note=note,
        moderated_by_id=admin_account_id, moderation_note=note,
        scheduled_start_at=_iso(start), scheduled_close_at=_iso(close),
        current_close_at=_iso(close))


def reject_lot(lot_id, admin_account_id, note=None):
    return transition(lot_id, "REJECTED", actor_account_id=admin_account_id,
                      note=note, moderated_by_id=admin_account_id,
                      moderation_note=note)


def send_back_lot(lot_id, admin_account_id, note=None):
    return transition(lot_id, "DRAFT", actor_account_id=admin_account_id,
                      note=note, moderated_by_id=admin_account_id,
                      moderation_note=note)


def cancel_lot(lot_id, admin_account_id, note=None):
    return transition(lot_id, "CANCELLED", actor_account_id=admin_account_id,
                      note=note)


def reschedule_lot(lot_id, admin_account_id, scheduled_start_at,
                   scheduled_close_at, note=None):
    start = _parse_ts(scheduled_start_at)
    close = _parse_ts(scheduled_close_at)
    if not start or not close or close <= start:
        raise AuctionError("reschedule requires a valid start and close")
    return transition(
        lot_id, "SCHEDULED", actor_account_id=admin_account_id, note=note,
        scheduled_start_at=_iso(start), scheduled_close_at=_iso(close),
        current_close_at=_iso(close))


# ---------------------------------------------------------------------------
# Public representation + render smoke check
# ---------------------------------------------------------------------------
def public_lot_dict(lot):
    """What bidders may see. The reserve AMOUNT is never exposed — only its
    presence, and after close whether it was met (derived, never stored)."""
    if not lot:
        return None
    image_keys = lot["image_keys"]
    if isinstance(image_keys, str):
        try:
            image_keys = json.loads(image_keys)
        except (TypeError, ValueError):
            image_keys = []
    status = lot["status"]
    reserve_met = None
    if status in ("NO_SALE", "INVOICED", "PAID", "SETTLED"):
        reserve_met = status != "NO_SALE"
    return {
        "id": lot["id"],
        "brand": lot["brand"],
        "title": lot["title"],
        "description": lot["description"],
        "category": lot["category"],
        "condition_notes": lot["condition_notes"],
        "image_keys": image_keys,
        "starting_price_cents": lot["starting_price_cents"],
        "current_price_cents": lot["current_price_cents"],
        "status": status,
        "is_live": status == "LIVE",
        "reserve_present": lot["reserve_price_cents"] is not None,
        "reserve_met": reserve_met,
        "scheduled_start_at": lot["scheduled_start_at"],
        "scheduled_close_at": lot["scheduled_close_at"],
        "current_close_at": lot["current_close_at"],
    }


def _default_render_check(lot, public):
    """Public-render smoke check. Structural floor: the lot must serialize
    to a complete public representation with no reserve-amount leak. When
    AUCTIONS_RENDER_CHECK_URL_TEMPLATE is configured (e.g.
    "https://restorationessentials.com/auctions/{lot_id}"), the live public
    page is also fetched and must return 200 carrying the lot title."""
    problems = []
    for key in ("title", "description", "category", "status"):
        if not public.get(key):
            problems.append(f"public representation missing {key}")
    if "reserve_price_cents" in json.dumps(public, default=str):
        problems.append("reserve amount leaked into public representation")
    template = os.environ.get("AUCTIONS_RENDER_CHECK_URL_TEMPLATE", "")
    if template:
        url = template.format(lot_id=lot["id"], brand=lot["brand"])
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                problems.append(f"public page returned HTTP {resp.status_code}")
            elif lot["title"] not in resp.text:
                problems.append("public page does not carry the lot title")
        except requests.RequestException as exc:
            problems.append(f"public page fetch failed: {exc}")
    return (not problems,
            "; ".join(problems) if problems else "render check passed")


def attempt_go_live(lot_id, actor_account_id=None, render_check=None):
    """The ONLY path into LIVE: SCHEDULED -> RENDER_CHECK -> (smoke check)
    -> LIVE or RENDER_FAILED. Returns (lot, ok, detail)."""
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    if lot["status"] != "SCHEDULED":
        raise AuctionError(
            f"only SCHEDULED lots can go live (lot is {lot['status']})")
    start = _parse_ts(lot["scheduled_start_at"])
    if start and _now() < start:
        raise AuctionError(
            "scheduled start has not been reached; a scheduled lot is not live")
    check = render_check or _render_check_fn or _default_render_check
    transition(lot_id, "RENDER_CHECK", actor_account_id=actor_account_id)
    lot = get_lot(lot_id)
    try:
        ok, detail = check(lot, public_lot_dict(lot))
    except Exception as exc:  # a crashing check is a FAILED check
        ok, detail = False, f"render check raised: {exc}"
    now = _iso(_now())
    lot = transition(
        lot_id, "LIVE" if ok else "RENDER_FAILED",
        actor_account_id=actor_account_id,
        note=detail, last_render_check_at=now,
        last_render_check_ok=bool(ok))
    return lot, bool(ok), detail


def list_public_lots(brand=None, statuses=("LIVE", "SCHEDULED")):
    clauses, params = [], []
    if brand:
        clauses.append("brand = ?")
        params.append(brand.strip().upper())
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    sql = "SELECT * FROM lots"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY scheduled_start_at ASC, created_at ASC"
    with _connect() as c:
        return c.execute(sql, tuple(params)).fetchall()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def _current_account():
    return get_account(session.get(SESSION_KEY))


def _require_account():
    account = _current_account()
    if not account:
        raise PermissionDenied("sign in required")
    if account["is_suspended"]:
        raise PermissionDenied("account suspended")
    return account


def _is_admin_request():
    account = _current_account()
    if account and account["is_admin"] and not account["is_suspended"]:
        return True
    token = request.headers.get("X-Auctions-Admin-Token", "")
    return bool(_ADMIN_TOKEN) and hmac.compare_digest(token, _ADMIN_TOKEN)


def _require_admin():
    if not _is_admin_request():
        raise PermissionDenied("admin access required")
    account = _current_account()
    return account["id"] if account else None


def _admin_actor_id():
    account = _current_account()
    return account["id"] if account else None


def _err(exc):
    status = 403 if isinstance(exc, PermissionDenied) else 400
    return jsonify({"error": str(exc)}), status


@bp.post("/api/auctions/accounts")
def api_create_account():
    data = request.get_json(silent=True) or {}
    try:
        account = create_account(
            data.get("brand"), data.get("email"), data.get("display_name"),
            data.get("password"), is_seller=bool(data.get("is_seller")))
    except AuctionError as exc:
        return _err(exc)
    return jsonify(public_account_dict(account)), 201


@bp.post("/api/auctions/login")
def api_login():
    data = request.get_json(silent=True) or {}
    account = authenticate(data.get("brand"), data.get("email"),
                           data.get("password"))
    if not account:
        return jsonify({"error": "invalid credentials"}), 401
    if account["is_suspended"]:
        return jsonify({"error": "account suspended"}), 403
    session[SESSION_KEY] = account["id"]
    return jsonify(public_account_dict(account))


@bp.post("/api/auctions/logout")
def api_logout():
    session.pop(SESSION_KEY, None)
    return jsonify({"ok": True})


@bp.get("/api/auctions/me")
def api_me():
    account = _current_account()
    if not account:
        return jsonify({"error": "not signed in"}), 401
    return jsonify(public_account_dict(account))


@bp.post("/api/auctions/lots")
def api_create_lot():
    try:
        seller = _require_account()
        data = request.get_json(silent=True) or {}
        brand = data.get("brand") or _BRAND_CODE or seller["brand"]
        lot = create_lot(
            seller["id"], brand, data.get("title"), data.get("description"),
            data.get("category"), data.get("starting_price_cents"),
            reserve_price_cents=data.get("reserve_price_cents"),
            condition_notes=data.get("condition_notes"),
            image_keys=data.get("image_keys"))
    except AuctionError as exc:
        return _err(exc)
    return jsonify(public_lot_dict(lot)), 201


@bp.post("/api/auctions/lots/<lot_id>/submit")
def api_submit_lot(lot_id):
    try:
        account = _require_account()
        lot = submit_lot(lot_id, account["id"])
    except AuctionError as exc:
        return _err(exc)
    return jsonify(public_lot_dict(lot))


@bp.get("/api/auctions/moderation/queue")
def api_moderation_queue():
    try:
        _require_admin()
    except AuctionError as exc:
        return _err(exc)
    brand = request.args.get("brand") or _BRAND_CODE or None
    lots = moderation_queue(brand)
    return jsonify([public_lot_dict(lot) for lot in lots])


@bp.post("/api/auctions/lots/<lot_id>/moderate")
def api_moderate_lot(lot_id):
    try:
        _require_admin()
        admin_id = _admin_actor_id()
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip().lower()
        note = data.get("note")
        if action == "claim":
            lot = claim_lot(lot_id, admin_id)
        elif action == "approve":
            lot = approve_lot(lot_id, admin_id,
                              data.get("scheduled_start_at"),
                              data.get("scheduled_close_at"), note=note)
        elif action == "reject":
            lot = reject_lot(lot_id, admin_id, note=note)
        elif action == "send_back":
            lot = send_back_lot(lot_id, admin_id, note=note)
        elif action == "cancel":
            lot = cancel_lot(lot_id, admin_id, note=note)
        elif action == "reschedule":
            lot = reschedule_lot(lot_id, admin_id,
                                 data.get("scheduled_start_at"),
                                 data.get("scheduled_close_at"), note=note)
        else:
            raise AuctionError(f"unknown moderation action: {action}")
    except AuctionError as exc:
        return _err(exc)
    return jsonify(public_lot_dict(lot))


@bp.post("/api/auctions/lots/<lot_id>/go-live")
def api_go_live(lot_id):
    try:
        _require_admin()
        lot, ok, detail = attempt_go_live(lot_id,
                                          actor_account_id=_admin_actor_id())
    except AuctionError as exc:
        return _err(exc)
    body = public_lot_dict(lot)
    body["render_check"] = {"ok": ok, "detail": detail}
    return jsonify(body), (200 if ok else 422)


@bp.get("/api/auctions/lots/<lot_id>")
def api_get_lot(lot_id):
    lot = get_lot(lot_id)
    if not lot:
        return jsonify({"error": "lot not found"}), 404
    account = _current_account()
    is_owner = account and account["id"] == lot["seller_account_id"]
    if lot["status"] in ("DRAFT", "IN_MODERATION", "REJECTED") \
            and not is_owner and not _is_admin_request():
        return jsonify({"error": "lot not found"}), 404
    return jsonify(public_lot_dict(lot))


@bp.get("/api/auctions/lots")
def api_list_lots():
    brand = request.args.get("brand") or _BRAND_CODE or None
    status_param = request.args.get("status")
    statuses = (tuple(s.strip().upper() for s in status_param.split(","))
                if status_param else ("LIVE", "SCHEDULED"))
    lots = list_public_lots(brand, statuses)
    return jsonify([public_lot_dict(lot) for lot in lots])


# ---------------------------------------------------------------------------
# Wiring (called once from app.py; disabled unless the brand opts in)
# ---------------------------------------------------------------------------
def init(app, brand_cfg=None, root_dir=".", render_check=None):
    """Wire the auctions blueprint into the Flask app.

    Enabled when the brand yaml carries `auctions: {enabled: true}` or the
    AUCTIONS_ENABLED env var is truthy — existing brands/services are
    untouched until they opt in. Returns True when enabled."""
    global _DIALECT, _DB_PATH, _DB_URL, _BRAND_CODE, _ADMIN_TOKEN
    global _render_check_fn
    cfg = (brand_cfg or {}).get("auctions", {}) or {}
    enabled_env = os.environ.get("AUCTIONS_ENABLED", "").strip().lower()
    if enabled_env not in ("1", "true", "yes") and not cfg.get("enabled"):
        log.info("auctions disabled for this service "
                 "(brand yaml auctions.enabled / AUCTIONS_ENABLED)")
        return False
    _BRAND_CODE = (os.environ.get("AUCTIONS_BRAND_CODE")
                   or cfg.get("brand_code") or "").strip().upper()
    _ADMIN_TOKEN = os.environ.get("AUCTIONS_ADMIN_TOKEN", "")
    _render_check_fn = render_check
    _DB_URL = (os.environ.get("AUCTIONS_DATABASE_URL")
               or os.environ.get("DATABASE_URL") or "").strip()
    if _DB_URL:
        _DIALECT = "postgres"
    else:
        _DIALECT = "sqlite"
        data_root = "/var/data" if os.path.isdir("/var/data") else \
            os.path.join(root_dir, "data")
        _DB_PATH = os.environ.get(
            "AUCTIONS_DB", os.path.join(data_root, "auctions.db"))
    _init_schema()
    app.register_blueprint(bp)
    log.info("auctions wired: dialect=%s brand_code=%s admin_token=%s",
             _DIALECT, _BRAND_CODE or "(any)",
             "configured" if _ADMIN_TOKEN else "NOT CONFIGURED")
    return True
