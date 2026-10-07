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

Slice 2 (this module, spec §§2–3): proxy max-bid placement — price =
second-highest max + one increment from the seeded increment_rules,
earliest-bid tiebreak, leader self-raise; soft close (+5 min per bid in
the final 5 minutes, +120-minute cap); the guarded closer (token-gated
endpoint for the Render Cron) computing winners idempotently through the
CLOSED -> {NO_SALE | INVOICED} fork, creating the winner's invoice and
the notifications outbox rows.

Slice 3 (this module, spec §§4–5, §7): winner pay pages — pay-page
tokens minted at invoice time (SHA-256 hash stored only; one ACTIVE
token per lot via a partial unique index, revoked predecessors kept for
audit), GET /pay/<token> minting a FRESH Stripe Checkout session on
every click (manual settlement: nothing auto-charges; sessions are
created on the brand service's own Stripe account — the auctions code
never passes a Connect account), the checkout.session.completed handler
marking invoice + lot PAID idempotently (invoice status re-read +
UNIQUE Stripe ids are the processed-events wall), the +24h/+48h unpaid
reminder pass, and the 72h second-chance ladder (runner-up offered at
their own max; accept swaps the winner atomically and issues a new
invoice + token; decline/expiry advances; exhaustion relists to DRAFT).
A reserve-not-met close with bids also offers the lot to the high
bidder at their max (best-practices gap-check C4); declining or letting
that offer expire relists the lot. Bid placement records the client IP
+ user agent on the bid row and writes a bid_attempts audit row for
every attempt, accepted or rejected (§8.2; gap-check C3).

Build slice numbering supersedes spec §9: this Slice 3 already carries
the pay pages AND the second-chance ladder that §9 splits across two
slices; public lot pages/admin UI and hardening follow as Slices 4–5.

Slice 4 (this module, spec §§6–7 as amended by auction-best-
practices-2026-10-02 §3.B): the public face.

  * Fee model (Bill, 2026-10-02): 4% BUYER premium added on top of the
    hammer; sellers list free and are paid the hammer in full. The
    premium is computed once, at invoice creation, and frozen on the
    invoice row (hammer_cents / buyer_premium_cents / amount_cents =
    hammer + premium). Settlements therefore record platform_fee_cents
    = 0 and seller_payout_cents = the hammer, in full.
  * Listing standard enforced at moderation (best-practices #2/#3):
    approve_lot() refuses until moderation_checklist() passes — photo
    minimums per brand, at least one video, a completed flaws section,
    VIN/title photos (RE) or frame-number photos (IH), condition
    notes, and the seller's no-AI-photos attestation.
  * Public lot pages (server-rendered, brand-aware): lot detail with
    photos/video, flaws, live close countdown (extended closes shown),
    reserve presence/met-only, "+ 4% buyer premium" displayed on lot
    and pay pages. The reserve AMOUNT and every bidder's max stay
    server-side, always.
  * Comments & Q&A on every lot (best-practices #1): email-verified
    accounts post; seller replies are flagged; admins hide junk (the
    hide is stamped on the comment row). COMMENT_QUESTION /
    SELLER_REPLIED outbox events reuse the §7 notifications table.
  * Public bidder profiles (read-only: member since, bid/win/sold
    counts, closed-lot bid history — effective prices only, never a
    max) and a per-account watchlist.
  * Pay-page polish: Referrer-Policy: no-referrer +
    Cache-Control: no-store on every pay-page response, and the
    hammer/premium/total breakdown shown to the winner.

NOT in Slices 1–4 (Slice 5): notifications delivery worker, Postgres
live-fire, launch gate, email verification flow (accounts start
unverified; bids and comments already require email_verified_at).

Slice 5 prep (this module): cross-brand category routing (Bill,
2026-10-02 — standing rule): motorcycle-related lots/categories on the
RestorationEssentials side link to IronHead, and muscle-car / truck /
modern-performance lots on the IronHead side link to
RestorationEssentials. The map is data-driven (CROSS_BRAND_ROUTES) and
surfaced on the auction index and every public lot page.

Persistence: AUCTIONS_DB (defaults to /var/data/auctions.db when the Render
disk is mounted, else <root>/data/auctions.db) — same disk rule as wholesale.
"""
import hashlib
import hmac
import html as _html
import json
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from flask import Blueprint, jsonify, make_response, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("pushrod.auctions")

bp = Blueprint("auctions", __name__)

BRANDS = ("RE", "IH")
BRAND_NAMES = {"RE": "Restoration Essentials", "IH": "IronHead"}
# Each brand's home site — the auctions page header links back here (a
# visitor who arrived from the brand site gets a working way back).
BRAND_HOME_URLS = {"RE": "https://restoreessentials.com/",
                   "IH": "https://ironheadguides.com/"}
# Canonical brand logos (frontend/static/img/) shown in the auctions
# page header, same mark each brand's face carries.
BRAND_LOGOS = {"RE": "/static/img/restoration-essentials-logo.png",
               "IH": "/static/img/ironhead-logo.png"}
BRAND_ACCENTS = {"RE": "#e8a020", "IH": "#c8402a"}
SESSION_KEY = "auctions_account_id"
EXTENSION_CAP_MINUTES = 120  # spec §1.6 chk_extension_cap

# Fee model (Bill, 2026-10-02): 4% buyer premium on top of the hammer,
# sellers list free. Frozen onto the invoice row at creation time.
BUYER_PREMIUM_BPS = 400


def buyer_premium_cents(hammer_cents):
    """4% of the hammer, rounded half-up to the cent."""
    hammer = int(hammer_cents or 0)
    if hammer <= 0:
        return 0
    return (hammer * BUYER_PREMIUM_BPS + 5000) // 10000


# Listing standard (best-practices #2/#3), enforced by approve_lot().
MIN_LOT_PHOTOS = {"RE": 20, "IH": 15}
MIN_ID_PHOTOS = {"RE": 2, "IH": 1}  # RE: VIN plate + title; IH: frame no.
COMMENT_MAX_CHARS = 2000
COMMENT_RULES = (
    "Be helpful and stay on the vehicle. No personal contact info, no "
    "asking about the reserve, no price predictions, no personal "
    "attacks, no self-promotion.")

# Cross-brand routing (Bill, 2026-10-02 — standing rule): RE directs
# motorcycles to IronHead; IH directs muscle cars, trucks, and modern
# performance to RestorationEssentials. Data-driven: category tokens
# matched against each lot's category string (case-insensitive substring
# match, first matching rule wins), and the auction index always offers
# the sibling brand's auctions. Categories outside the map carry no
# cross-brand implication and stay on their own brand's site.
CROSS_BRAND_ROUTES = [
    ("IH", ("muscle", "truck", "performance", "muscle-cars",
            "classic-trucks", "trucks"), "RE",
     ("muscle cars", "trucks", "modern performance")),
    ("RE", ("motorcycle", "motorcycles", "bike", "bikes"), "IH",
     ("motorcycles",)),
]


def cross_brand_target(brand, category):
    """The sibling-brand route for a (brand, category) pair, or None.

    Returns {"brand", "brand_name", "label"} when the category belongs
    on the other brand's auctions site under Bill's standing rule;
    None when the category stays with its own brand."""
    brand = (brand or "").strip().upper()
    cat = (category or "").strip().lower()
    for src, tokens, dst, labels in CROSS_BRAND_ROUTES:
        if src == brand and any(tok in cat for tok in tokens):
            return {"brand": dst, "brand_name": BRAND_NAMES[dst],
                    "label": ", ".join(labels)}
    return None


def sibling_brand(brand):
    """The cross-brand sibling ({RE -> IH, IH -> RE}) as a route dict."""
    brand = (brand or "").strip().upper()
    if brand == "RE":
        return {"brand": "IH", "brand_name": BRAND_NAMES["IH"],
                "label": "motorcycles"}
    if brand == "IH":
        return {"brand": "RE", "brand_name": BRAND_NAMES["RE"],
                "label": "muscle cars, trucks, modern performance"}
    return None

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


def _esc(value):
    return _html.escape("" if value is None else str(value), quote=True)


def _money(cents):
    cents = int(cents or 0)
    return f"${cents // 100:,d}.{cents % 100:02d}"


def _invoice_breakdown(invoice):
    """The frozen composition as one readable line: hammer + 4% buyer
    premium = amount due. Empty for legacy rows without a hammer."""
    hammer = invoice.get("hammer_cents")
    if hammer is None:
        return ""
    premium = int(invoice.get("buyer_premium_cents") or 0)
    return (f" Hammer {_money(hammer)} + buyer premium (4%) "
            f"{_money(premium)} = {_money(invoice['amount_cents'])}"
            " total.")


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

    def execute_script(self, script):
        """Execute a multi-statement DDL script verbatim (Postgres only).

        The schema script must NOT flow through execute(): psycopg scans
        every query that arrives WITH a params argument — even an empty
        one — for %s-style placeholders, and auctions_schema.sql carries
        literal '%' characters ("4% buyer premium" comments), which
        psycopg rejects with a ProgrammingError. Called with no params,
        the driver sends the script uninterpreted and Postgres runs
        every statement as written.
        """
        if _DIALECT != "postgres":
            raise RuntimeError(
                "execute_script is the Postgres DDL path; the SQLite"
                " branch applies its own per-statement DDL")
        self._cur.execute(script)

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
        flaws TEXT,
        image_keys TEXT NOT NULL DEFAULT '[]',
        video_keys TEXT NOT NULL DEFAULT '[]',
        id_photo_keys TEXT NOT NULL DEFAULT '[]',
        no_ai_photos_attested INTEGER NOT NULL DEFAULT 0,
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
        ip_address TEXT,
        user_agent TEXT,
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
    CREATE TABLE IF NOT EXISTS bid_attempts (
        id TEXT PRIMARY KEY,
        lot_id TEXT,
        bidder_account_id TEXT,
        max_bid_cents INTEGER,
        bid_id TEXT,
        ip_address TEXT,
        user_agent TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('ACCEPTED','REJECTED')),
        reason TEXT,
        created_at TEXT NOT NULL
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
        hammer_cents INTEGER,
        buyer_premium_cents INTEGER NOT NULL DEFAULT 0,
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
        -- Fee model (Bill 2026-10-02): the 4% premium is buyer-side,
        -- frozen on the invoice. Settlements record gross = the hammer
        -- and platform_fee_cents = 0, so the generated payout is the
        -- hammer in full. The fee column stays for the arithmetic.
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
        original_invoice_id TEXT REFERENCES invoices(id),
        offer_kind TEXT NOT NULL DEFAULT 'UNPAID_WINNER'
            CHECK (offer_kind IN ('UNPAID_WINNER','RESERVE_NOT_MET')),
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
            'RESERVE_NOT_MET','COMMENT_QUESTION','SELLER_REPLIED')),
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        read_at TEXT
    )""",
    """
    CREATE TABLE IF NOT EXISTS comments (
        id TEXT PRIMARY KEY,
        lot_id TEXT NOT NULL REFERENCES lots(id),
        account_id TEXT NOT NULL REFERENCES accounts(id),
        parent_id TEXT REFERENCES comments(id),
        body TEXT NOT NULL,
        is_seller INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'VISIBLE'
            CHECK (status IN ('VISIBLE','HIDDEN')),
        hidden_at TEXT,
        hidden_by_id TEXT REFERENCES accounts(id),
        hide_note TEXT,
        created_at TEXT NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        lot_id TEXT NOT NULL REFERENCES lots(id),
        created_at TEXT NOT NULL,
        CONSTRAINT uq_watchlist_account_lot UNIQUE (account_id, lot_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lots_brand_status ON lots (brand, status)",
    # Slice 3 (spec §4.2): one ACTIVE pay token per lot, revoked rows kept
    # for audit; one OPEN invoice per lot, voided invoices kept for audit.
    # (Postgres twins live in auctions_schema.sql.)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pay_page_active_token_per_lot"
    " ON pay_page_tokens (lot_id) WHERE revoked_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_open_per_lot"
    " ON invoices (lot_id) WHERE status = 'OPEN'",
    "CREATE INDEX IF NOT EXISTS idx_lots_seller ON lots (seller_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_bids_lot_status ON bids (lot_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_bid_attempts_lot"
    " ON bid_attempts (lot_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_mod_actions_lot ON moderation_actions (lot_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_comments_lot ON comments (lot_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_watchlist_account ON watchlist (account_id, created_at)",
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
            # NOT c.execute(script): execute() always binds a params
            # tuple, and psycopg's placeholder scan chokes on the
            # script's literal '%' (see _Conn.execute_script).
            c.execute_script(script)
        log.info("auctions schema ready (postgres)")
        return
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    with _connect() as c:
        for stmt in _SQLITE_DDL:
            c.execute(stmt)
        # Pre-Slice-3 SQLite files: CREATE TABLE IF NOT EXISTS does not
        # add new columns, so backfill the bid-IP / offer-kind columns.
        bid_cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(bids)").fetchall()}
        for col in ("ip_address", "user_agent"):
            if col not in bid_cols:
                c.execute(f"ALTER TABLE bids ADD COLUMN {col} TEXT")
        offer_cols = {r["name"] for r in c.execute(
            "PRAGMA table_info(second_chance_offers)").fetchall()}
        if "offer_kind" not in offer_cols:
            c.execute(
                "ALTER TABLE second_chance_offers ADD COLUMN offer_kind"
                " TEXT NOT NULL DEFAULT 'UNPAID_WINNER'")
        # Pre-Slice-4 SQLite files: add the listing-standard columns,
        # the frozen invoice composition, and backfill the hammer.
        lot_cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(lots)").fetchall()}
        for col, ddl in (
                ("flaws", "TEXT"),
                ("video_keys", "TEXT NOT NULL DEFAULT '[]'"),
                ("id_photo_keys", "TEXT NOT NULL DEFAULT '[]'"),
                ("no_ai_photos_attested", "INTEGER NOT NULL DEFAULT 0")):
            if col not in lot_cols:
                c.execute(f"ALTER TABLE lots ADD COLUMN {col} {ddl}")
        inv_cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(invoices)").fetchall()}
        if "hammer_cents" not in inv_cols:
            c.execute("ALTER TABLE invoices ADD COLUMN hammer_cents"
                      " INTEGER")
        if "buyer_premium_cents" not in inv_cols:
            c.execute("ALTER TABLE invoices ADD COLUMN"
                      " buyer_premium_cents INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE invoices SET hammer_cents = amount_cents"
                  " WHERE hammer_cents IS NULL")
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
    if _DIALECT == "postgres" and not _looks_uuid(account_id):
        # accounts.id is a uuid column on Postgres: a non-UUID id can
        # only be "not found" — asking the driver raises instead.
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
    if _DIALECT == "postgres" and not _looks_uuid(lot_id):
        # lots.id is a uuid column on Postgres: a non-UUID id can only
        # be "not found" — asking the driver raises instead of 404ing.
        return None
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
               condition_notes=None, image_keys=None, flaws=None,
               video_keys=None, id_photo_keys=None,
               no_ai_photos_attested=False):
    """Seller creates a DRAFT lot. DRAFT = submitted by seller, not yet in
    the moderation queue (spec §1.3); submit_lot() puts it in the queue.
    The Slice-4 listing fields (flaws, videos, VIN/title photos, the
    no-AI-photos attestation) are collected here and enforced later, at
    approval time, by moderation_checklist()."""
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
            " description, category, condition_notes, flaws, image_keys,"
            " video_keys, id_photo_keys, no_ai_photos_attested,"
            " starting_price_cents, reserve_price_cents, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lot_id, brand, seller_id, title, description.strip(),
             category.strip(), condition_notes,
             (flaws or "").strip() or None, list(image_keys or []),
             list(video_keys or []), list(id_photo_keys or []),
             bool(no_ai_photos_attested),
             starting, reserve, now, now))
    return get_lot(lot_id)


_LISTING_FIELDS = ("description", "condition_notes", "flaws",
                   "image_keys", "video_keys", "id_photo_keys",
                   "no_ai_photos_attested")


def update_lot_listing(lot_id, actor_account_id, **fields):
    """Seller edits the listing fields of their own DRAFT lot — the
    send-back loop: moderation names what's missing, the seller fixes
    it here, then resubmits. Nothing outside _LISTING_FIELDS moves."""
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    if lot["seller_account_id"] != actor_account_id:
        raise PermissionDenied("only the seller can edit this lot")
    if lot["status"] != "DRAFT":
        raise AuctionError("only DRAFT lots can be edited")
    updates = {}
    for key in _LISTING_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key in ("image_keys", "video_keys", "id_photo_keys"):
            if not isinstance(value, (list, tuple)):
                raise AuctionError(f"{key} must be a list")
            updates[key] = list(value)
        elif key == "no_ai_photos_attested":
            updates[key] = bool(value)
        elif key == "flaws":
            updates[key] = (value or "").strip() or None
        else:
            updates[key] = value
    if not updates:
        raise AuctionError("no listing fields supplied")
    updates["updated_at"] = _iso(_now())
    cols = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as c:
        c.execute(f"UPDATE lots SET {cols} WHERE id = ?",
                  (*updates.values(), lot_id))
    return get_lot(lot_id)


def _json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def moderation_checklist(lot):
    """The listing standard (best-practices #2/#3), evaluated against a
    lot row. approve_lot() hard-blocks until every item passes; the
    moderation queue shows the same list so the fix is obvious."""
    brand = lot["brand"]
    photos = _json_list(lot.get("image_keys"))
    videos = _json_list(lot.get("video_keys"))
    id_photos = _json_list(lot.get("id_photo_keys"))
    min_photos = MIN_LOT_PHOTOS[brand]
    min_id = MIN_ID_PHOTOS[brand]
    id_label = ("VIN plate + title photos" if brand == "RE"
                else "frame-number photos")
    items = [
        {"key": "photos", "label": f"at least {min_photos} photos",
         "ok": len(photos) >= min_photos,
         "detail": f"{len(photos)}/{min_photos}"},
        {"key": "videos",
         "label": "video present (walk-around + cold start)",
         "ok": len(videos) >= 1, "detail": f"{len(videos)} video(s)"},
        {"key": "flaws", "label": "flaws section completed",
         "ok": bool((lot.get("flaws") or "").strip()),
         "detail": "present" if (lot.get("flaws") or "").strip()
         else "missing"},
        {"key": "id_photos", "label": id_label,
         "ok": len(id_photos) >= min_id,
         "detail": f"{len(id_photos)}/{min_id}"},
        {"key": "no_ai_photos",
         "label": "no-AI-photos attestation confirmed",
         "ok": bool(lot.get("no_ai_photos_attested")),
         "detail": "attested" if lot.get("no_ai_photos_attested")
         else "not attested"},
        {"key": "condition_notes", "label": "condition notes present",
         "ok": bool((lot.get("condition_notes") or "").strip()),
         "detail": "present" if (lot.get("condition_notes") or "").strip()
         else "missing"},
    ]
    missing = [i["label"] for i in items if not i["ok"]]
    return {"brand": brand, "passed": not missing, "items": items,
            "missing": missing}


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
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    checklist = moderation_checklist(lot)
    if not checklist["passed"]:
        raise AuctionError(
            "listing standard not met — missing: "
            + "; ".join(checklist["missing"]))
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
def public_lot_dict(lot, account_id=None):
    """What bidders may see. The reserve AMOUNT is never exposed — only its
    presence, and after close whether it was met (derived, never stored).
    With account_id, adds that viewer's own bid state (§2.7)."""
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
    view = {
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
    if status in ("LIVE", "CLOSED", "NO_SALE", "INVOICED", "PAID",
                  "SETTLED"):
        state = public_bid_state(lot, account_id)
        view["bid_count"] = state["bid_count"]
        if status == "LIVE":
            with _connect() as c:
                view["minimum_next_bid_cents"] = (
                    int(lot["current_price_cents"]) + _increment_for(
                        c, lot["brand"], int(lot["current_price_cents"])))
        if account_id:
            view["your_max_bid_cents"] = state["your_max_bid_cents"]
            view["you_are_leading"] = state["you_are_leading"]
    # Slice 4 public-face fields. VIN/title photo keys stay OUT (title
    # photos carry owner PII — admin-only, see admin_lot_dict).
    view["flaws"] = lot.get("flaws")
    view["video_keys"] = _json_list(lot.get("video_keys"))
    view["buyer_premium_bps"] = BUYER_PREMIUM_BPS
    view["seller_account_id"] = lot["seller_account_id"]
    with _connect() as c:
        seller = c.execute(
            "SELECT display_name FROM accounts WHERE id = ?",
            (lot["seller_account_id"],)).fetchone()
    view["seller_display_name"] = seller["display_name"] if seller else None
    if status in POST_WIN_STATUSES:
        with _connect() as c:
            inv = c.execute(
                "SELECT * FROM invoices WHERE lot_id = ?"
                " ORDER BY issued_at DESC LIMIT 1", (lot["id"],)).fetchone()
        if inv:
            hammer = inv.get("hammer_cents")
            if hammer is None:
                hammer = int(inv["amount_cents"])
            view["hammer_cents"] = int(hammer)
            view["buyer_premium_cents"] = int(
                inv.get("buyer_premium_cents") or 0)
            view["amount_due_cents"] = int(inv["amount_cents"])
    if account_id:
        view["watching"] = is_watching(account_id, lot["id"])
    return view


def admin_lot_dict(lot):
    """The admin/moderation view: everything public PLUS the reserve
    amount, the VIN/title photo keys, the attestation flag, and the
    listing-standard checklist. Admin endpoints only — never serialize
    this for a bidder."""
    if not lot:
        return None
    view = public_lot_dict(lot)
    view["reserve_price_cents"] = lot["reserve_price_cents"]
    view["id_photo_keys"] = _json_list(lot.get("id_photo_keys"))
    view["no_ai_photos_attested"] = bool(lot.get("no_ai_photos_attested"))
    view["moderation_checklist"] = moderation_checklist(lot)
    view["moderated_by_id"] = lot.get("moderated_by_id")
    return view


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
# Bidding — proxy engine, soft close, closer (Slice 2, spec §§2–3)
# ---------------------------------------------------------------------------
SOFT_CLOSE_WINDOW = timedelta(minutes=5)   # bid inside final 5 min extends
SOFT_CLOSE_EXTENSION = timedelta(minutes=5)
PAYMENT_WINDOW = timedelta(hours=72)       # spec §1.10 payment_deadline_at


def _increment_for(c, brand, price_cents):
    """Spec §2.4: the increment whose range [low, high) contains the price.
    The price passed in is always the LOWER of the two competing maxes."""
    row = c.execute(
        "SELECT increment_cents FROM increment_rules WHERE brand = ?"
        " AND range_low_cents <= ?"
        " AND (range_high_cents IS NULL OR range_high_cents > ?)"
        " AND (effective_to IS NULL OR effective_to >= ?)"
        " ORDER BY range_low_cents DESC LIMIT 1",
        (brand, price_cents, price_cents, _now().date().isoformat())
    ).fetchone()
    if not row:
        raise AuctionError("no increment rule seeded for this price range")
    return int(row["increment_cents"])


def _notify(c, account_id, lot_id, event, payload=None):
    """Write one notifications outbox row (spec §7.1 — same transaction as
    the state change; delivery is a later slice)."""
    c.execute(
        "INSERT INTO notifications (id, account_id, lot_id, event, payload,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), account_id, lot_id, event,
         dict(payload or {}), _iso(_now())))


def _ranked_active_bids(c, lot_id):
    """ACTIVE bids in proxy order: highest max first, earliest placed wins
    ties (spec §§2.5, 3.3)."""
    return c.execute(
        "SELECT * FROM bids WHERE lot_id = ? AND status = 'ACTIVE'"
        " ORDER BY max_bid_cents DESC, placed_at ASC, id ASC",
        (lot_id,)).fetchall()


def _floor_price(lot, max_bid_cents):
    """Effective price of a lone leading bid: the starting price (spec
    §3.3 single-bid rule), never above the bidder's own max, never 0
    (bids.effective_price_cents CHECK requires > 0)."""
    return max(1, min(int(max_bid_cents), int(lot["starting_price_cents"])))


def _record_bid_attempt(lot_id, bidder_account_id, max_bid_cents,
                        bid_id, ip_address, user_agent, outcome, reason):
    """Write one bid_attempts audit row (§8.2). Runs on its own
    connection so a rejected bid's rollback can never take the audit
    row with it. Audit failure must never mask the bid outcome, so
    errors are logged, not raised."""
    try:
        with _connect() as c:
            c.execute(
                "INSERT INTO bid_attempts (id, lot_id, bidder_account_id,"
                " max_bid_cents, bid_id, ip_address, user_agent, outcome,"
                " reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), lot_id, bidder_account_id,
                 max_bid_cents, bid_id, ip_address, user_agent, outcome,
                 reason, _iso(_now())))
    except Exception:  # noqa: BLE001 — audit must not mask the bid result
        log.warning("bid attempt audit write failed (lot=%s)", lot_id,
                    exc_info=True)


def place_bid(bidder_account_id, lot_id, max_bid_cents, now=None,
              ip_address=None, user_agent=None):
    """Place a proxy max-bid (spec §2). Returns a result dict describing
    the bidder's standing; never exposes another bidder's max. Every
    attempt — accepted or rejected — leaves a bid_attempts audit row
    carrying the client IP (§8.2)."""
    try:
        parsed_max = int(max_bid_cents)
        if isinstance(max_bid_cents, bool):
            parsed_max = None
    except (TypeError, ValueError):
        parsed_max = None
    try:
        result = _place_bid_validated(bidder_account_id, lot_id,
                                      max_bid_cents, now=now,
                                      ip_address=ip_address,
                                      user_agent=user_agent)
    except Exception as exc:
        _record_bid_attempt(lot_id, bidder_account_id, parsed_max, None,
                            ip_address, user_agent, "REJECTED", str(exc))
        raise
    _record_bid_attempt(lot_id, bidder_account_id, parsed_max,
                        result["bid_id"], ip_address, user_agent,
                        "ACCEPTED", result["outcome"])
    return result


def _place_bid_validated(bidder_account_id, lot_id, max_bid_cents,
                         now=None, ip_address=None, user_agent=None):
    now = now or _now()
    try:
        max_bid = int(max_bid_cents)
    except (TypeError, ValueError):
        raise AuctionError("bid amount must be a whole number of cents")
    if isinstance(max_bid_cents, bool) or max_bid <= 0:
        raise AuctionError("bid amount must be a positive number of cents")
    bidder = get_account(bidder_account_id)
    if not bidder:
        raise AuctionError("bidder account not found")
    with _connect() as c:
        lot = _require_lot(c, lot_id)
        if lot["status"] != "LIVE":
            raise AuctionError("lot is not live for bidding")
        if bidder["brand"] != lot["brand"]:
            raise AuctionError("bidder account belongs to a different brand")
        if lot["seller_account_id"] == bidder["id"]:
            raise PermissionDenied("sellers cannot bid on their own lots")
        if bidder["is_suspended"]:
            raise PermissionDenied("suspended accounts cannot bid")
        if not bidder["email_verified_at"]:
            raise PermissionDenied(
                "email must be verified before bidding")
        ranked = _ranked_active_bids(c, lot_id)
        leader = ranked[0] if ranked else None
        current_price = int(lot["current_price_cents"])
        if leader is None:
            minimum = int(lot["starting_price_cents"])
            if max_bid < minimum:
                raise AuctionError(
                    f"bid is below the minimum of {minimum} cents")
        elif leader["bidder_account_id"] == bidder["id"]:
            if max_bid <= int(leader["max_bid_cents"]):
                raise AuctionError(
                    "new max must exceed your current max bid")
            minimum = int(leader["max_bid_cents"]) + 1
        else:
            minimum = current_price + _increment_for(
                c, lot["brand"], current_price)
            if max_bid < minimum:
                raise AuctionError(
                    f"bid is below the minimum of {minimum} cents")
        brand = lot["brand"]
        now_iso = _iso(now)
        bid_id = str(uuid.uuid4())
        outcome = None            # "leading" | "outbid" | "raised"
        new_price = current_price
        previous_leader_id = leader["bidder_account_id"] if leader else None
        if leader is None:
            new_price = _floor_price(lot, max_bid)
            c.execute(
                "INSERT INTO bids (id, lot_id, bidder_account_id,"
                " max_bid_cents, effective_price_cents, status, proxy_rank,"
                " placed_at, ip_address, user_agent, triggered_extension, created_at)"
                " VALUES (?,?,?,?,?,'ACTIVE',1,?,?,?,?,?)",
                (bid_id, lot_id, bidder["id"], max_bid, new_price,
                 now_iso, ip_address, user_agent, False, now_iso))
            outcome = "leading"
        elif leader["bidder_account_id"] == bidder["id"]:
            # Case C — leader raising their own max: price never moves.
            c.execute(
                "UPDATE bids SET status = 'OUTBID' WHERE id = ?",
                (leader["id"],))
            c.execute(
                "INSERT INTO bids (id, lot_id, bidder_account_id,"
                " max_bid_cents, effective_price_cents, status, proxy_rank,"
                " placed_at, ip_address, user_agent, triggered_extension, created_at)"
                " VALUES (?,?,?,?,?,'ACTIVE',?, ?,?,?,?,?)",
                (bid_id, lot_id, bidder["id"], max_bid,
                 int(leader["effective_price_cents"]),
                 (leader["proxy_rank"] or 1) + 1, now_iso, ip_address, user_agent, False, now_iso))
            outcome = "raised"
        elif max_bid > int(leader["max_bid_cents"]):
            # Case B-1 — challenger wins at second-highest max + increment.
            inc = _increment_for(c, brand, int(leader["max_bid_cents"]))
            new_price = min(max_bid,
                            int(leader["max_bid_cents"]) + inc)
            c.execute(
                "UPDATE bids SET status = 'OUTBID' WHERE id = ?",
                (leader["id"],))
            c.execute(
                "INSERT INTO bids (id, lot_id, bidder_account_id,"
                " max_bid_cents, effective_price_cents, status, proxy_rank,"
                " placed_at, ip_address, user_agent, triggered_extension, created_at)"
                " VALUES (?,?,?,?,?,'ACTIVE',?, ?,?,?,?,?)",
                (bid_id, lot_id, bidder["id"], max_bid, new_price,
                 (leader["proxy_rank"] or 1) + 1, now_iso, ip_address, user_agent, False, now_iso))
            outcome = "leading"
        elif max_bid == int(leader["max_bid_cents"]):
            # Tie (§2.5): earliest bid keeps the lead at its full max.
            new_price = int(leader["max_bid_cents"])
            c.execute(
                "UPDATE bids SET effective_price_cents = ? WHERE id = ?",
                (new_price, leader["id"]))
            c.execute(
                "INSERT INTO bids (id, lot_id, bidder_account_id,"
                " max_bid_cents, effective_price_cents, status, proxy_rank,"
                " placed_at, ip_address, user_agent, triggered_extension, created_at)"
                " VALUES (?,?,?,?,?,'OUTBID',?, ?,?,?,?,?)",
                (bid_id, lot_id, bidder["id"], max_bid, max_bid,
                 leader["proxy_rank"], now_iso, ip_address, user_agent, False, now_iso))
            outcome = "outbid"
        else:
            # Case B-2 — challenger loses; leader's price rises to the
            # challenger's max + increment (capped at the leader's max).
            inc = _increment_for(c, brand, max_bid)
            new_price = min(int(leader["max_bid_cents"]), max_bid + inc)
            if new_price > int(leader["effective_price_cents"]):
                c.execute(
                    "UPDATE bids SET effective_price_cents = ? WHERE id = ?",
                    (new_price, leader["id"]))
            c.execute(
                "INSERT INTO bids (id, lot_id, bidder_account_id,"
                " max_bid_cents, effective_price_cents, status, proxy_rank,"
                " placed_at, ip_address, user_agent, triggered_extension, created_at)"
                " VALUES (?,?,?,?,?,'OUTBID',?, ?,?,?,?,?)",
                (bid_id, lot_id, bidder["id"], max_bid, max_bid,
                 leader["proxy_rank"], now_iso, ip_address, user_agent, False, now_iso))
            outcome = "outbid"
        # Soft close (spec §3.1): a bid inside the final window extends
        # the close by 5 minutes, capped at +120 total.
        extended = False
        close_at = _parse_ts(lot["current_close_at"])
        used = int(lot["extension_minutes_used"] or 0)
        if close_at is not None and close_at - now < SOFT_CLOSE_WINDOW \
                and used < EXTENSION_CAP_MINUTES:
            step = min(5, EXTENSION_CAP_MINUTES - used)
            close_at = close_at + timedelta(minutes=step)
            used += step
            c.execute(
                "UPDATE bids SET triggered_extension = ? WHERE id = ?",
                (True, bid_id))
            c.execute(
                "UPDATE lots SET current_close_at = ?,"
                " extension_minutes_used = ? WHERE id = ?",
                (_iso(close_at), used, lot_id))
            extended = True
        if outcome in ("leading", "raised"):
            c.execute(
                "UPDATE lots SET current_price_cents = ?,"
                " leading_bidder_id = ?, updated_at = ? WHERE id = ?",
                (new_price, bidder["id"], now_iso, lot_id))
            if outcome == "leading" and previous_leader_id \
                    and previous_leader_id != bidder["id"]:
                _notify(c, previous_leader_id, lot_id, "OUTBID", {
                    "lot_id": lot_id, "current_price_cents": new_price,
                    "minimum_next_bid_cents": new_price + _increment_for(
                        c, brand, new_price)})
                _notify(c, bidder["id"], lot_id, "WINNING", {
                    "lot_id": lot_id, "current_price_cents": new_price})
        else:
            c.execute(
                "UPDATE lots SET current_price_cents = ?, updated_at = ?"
                " WHERE id = ?", (new_price, now_iso, lot_id))
        result_lot = _require_lot(c, lot_id)
        result = {
            "bid_id": bid_id,
            "lot_id": lot_id,
            "outcome": outcome,                    # leading | outbid | raised
            "is_leading": outcome in ("leading", "raised"),
            "your_max_bid_cents": max_bid,
            "current_price_cents": int(result_lot["current_price_cents"]),
            "minimum_next_bid_cents":
                int(result_lot["current_price_cents"]) + _increment_for(
                    c, brand, int(result_lot["current_price_cents"])),
            "current_close_at": result_lot["current_close_at"],
            "extension_triggered": extended,
            "bid_count": c.execute(
                "SELECT COUNT(*) AS n FROM bids WHERE lot_id = ?"
                " AND status != 'VOIDED'", (lot_id,)).fetchone()["n"],
        }
    return result


def public_bid_state(lot, account_id=None):
    """Per-viewer bid state for a lot page (spec §2.7): the viewer's own
    max/standing is visible to them alone; nobody else's max ever leaks."""
    view = {
        "bid_count": 0,
        "your_max_bid_cents": None,
        "you_are_leading": False,
    }
    with _connect() as c:
        view["bid_count"] = c.execute(
            "SELECT COUNT(*) AS n FROM bids WHERE lot_id = ?"
            " AND status != 'VOIDED'", (lot["id"],)).fetchone()["n"]
        if account_id:
            mine = c.execute(
                "SELECT * FROM bids WHERE lot_id = ?"
                " AND bidder_account_id = ? AND status != 'VOIDED'"
                " ORDER BY placed_at DESC LIMIT 1",
                (lot["id"], account_id)).fetchone()
            if mine:
                view["your_max_bid_cents"] = mine["max_bid_cents"]
                view["you_are_leading"] = (
                    lot["leading_bidder_id"] == account_id)
    return view


def compute_winner(c, lot):
    """Spec §3.3: returns (winner_bid, winning_price_cents) or
    (None, current_price) when there are no ACTIVE bids."""
    ranked = _ranked_active_bids(c, lot["id"])
    if not ranked:
        return None, int(lot["current_price_cents"])
    winner = ranked[0]
    if len(ranked) == 1:
        return winner, int(winner["effective_price_cents"])
    second_max = int(ranked[1]["max_bid_cents"])
    inc = _increment_for(c, lot["brand"], second_max)
    price = min(int(winner["max_bid_cents"]), second_max + inc)
    return winner, price


def close_lot(lot_id, now=None):
    """Compute the winner for one due lot through the guarded CLOSED fork
    (spec §§3.3–3.4). Idempotent: a lot not LIVE (or CLOSED with its fork
    already resolved) is reported as skipped, and re-running after a
    crash self-heals the invoice. Returns a result dict."""
    now = now or _now()
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    if lot["status"] == "INVOICED":
        invoice = _ensure_invoice(lot, now)
        return {"lot_id": lot_id, "outcome": "already_invoiced",
                "invoice_id": invoice["id"] if invoice else None}
    if lot["status"] not in ("LIVE", "CLOSED"):
        return {"lot_id": lot_id, "outcome": "skipped",
                "reason": f"lot is {lot['status']}"}
    if lot["status"] == "LIVE":
        close_at = _parse_ts(lot["current_close_at"])
        if close_at is not None and close_at > now:
            return {"lot_id": lot_id, "outcome": "not_due"}
        transition(lot_id, "CLOSED")
        lot = get_lot(lot_id)
    # CLOSED with no winner yet -> resolve the fork now (crash recovery
    # lands here too). The fork write itself is atomic via transition().
    with _connect() as c:
        winner, price = compute_winner(c, lot)
    reserve = lot["reserve_price_cents"]
    if winner is None or (reserve is not None and price < int(reserve)):
        closed = transition(lot_id, "NO_SALE",
                            current_price_cents=price)
        rnm_offer = None
        with _connect() as c:
            _notify(c, lot["seller_account_id"], lot_id, "RESERVE_NOT_MET", {
                "lot_id": lot_id, "current_price_cents": price,
                "had_bids": winner is not None})
            for bid in _ranked_active_bids(c, lot_id):
                _notify(c, bid["bidder_account_id"], lot_id, "OUTBID", {
                    "lot_id": lot_id, "current_price_cents": price,
                    "final": True})
            if winner is not None:
                # Gap-check C4: reserve missed but bids exist — offer
                # the lot to the high bidder at their max instead of
                # stranding it at NO_SALE.
                rnm_offer = _offer_reserve_not_met(
                    c, _require_lot(c, lot_id), winner, now)
        result = {"lot_id": lot_id, "outcome": "no_sale",
                  "current_price_cents": price}
        if rnm_offer:
            result["second_chance_offer_id"] = rnm_offer["offer_id"]
            result["second_chance_offeree_account_id"] = \
                rnm_offer["offeree_account_id"]
            result["second_chance_offered_price_cents"] = \
                rnm_offer["offered_price_cents"]
        return result
    closed = transition(lot_id, "INVOICED",
                        winner_account_id=winner["bidder_account_id"],
                        winning_price_cents=price,
                        current_price_cents=price)
    invoice = _ensure_invoice(closed, now)
    token = issue_pay_token(lot_id, now=now)
    with _connect() as c:
        payload = {"lot_id": lot_id, "winning_price_cents": price,
                   "hammer_cents": int(invoice["hammer_cents"]),
                   "buyer_premium_cents": int(
                       invoice["buyer_premium_cents"]),
                   "amount_cents": int(invoice["amount_cents"]),
                   "invoice_id": invoice["id"],
                   "payment_deadline_at": invoice["payment_deadline_at"]}
        _notify(c, winner["bidder_account_id"], lot_id, "AUCTION_WON",
                payload)
        issued = dict(payload)
        issued["pay_page_url"] = token["pay_page_url"]
        _notify(c, winner["bidder_account_id"], lot_id, "INVOICE_ISSUED",
                issued)
        ranked = _ranked_active_bids(c, lot_id)
        losers = [b for b in ranked
                  if b["bidder_account_id"] != winner["bidder_account_id"]]
        # Spec §3.5: the runner-up's final OUTBID is deferred — they are
        # the second-chance candidate if the winner never pays.
        for bid in losers[1:]:
            _notify(c, bid["bidder_account_id"], lot_id, "OUTBID", {
                "lot_id": lot_id, "current_price_cents": price,
                "final": True})
    return {"lot_id": lot_id, "outcome": "invoiced",
            "winner_account_id": winner["bidder_account_id"],
            "winning_price_cents": price,
            "invoice_id": invoice["id"]}


def _insert_invoice(c, lot_id, winner_account_id, hammer_cents, now):
    """Create the OPEN invoice with the fee composition frozen on the
    row (Bill 2026-10-02): amount = hammer + 4% buyer premium. The
    hammer never moves after this write; settlement pays it in full."""
    hammer = int(hammer_cents)
    premium = buyer_premium_cents(hammer)
    invoice_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO invoices (id, lot_id, winner_account_id,"
        " amount_cents, hammer_cents, buyer_premium_cents, status,"
        " issued_at, payment_deadline_at) VALUES (?,?,?,?,?,?,'OPEN',?,?)",
        (invoice_id, lot_id, winner_account_id, hammer + premium,
         hammer, premium, _iso(now), _iso(now + PAYMENT_WINDOW)))
    return c.execute("SELECT * FROM invoices WHERE id = ?",
                     (invoice_id,)).fetchone()


def _ensure_invoice(lot, now=None):
    """Create the winner's invoice if missing (idempotent). A lot may
    accumulate VOID invoices across the second-chance ladder; the live
    one is the OPEN (or PAID) row, backstopped by the partial unique
    index uq_invoices_open_per_lot."""
    now = now or _now()
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM invoices WHERE lot_id = ?"
            " ORDER BY issued_at DESC", (lot["id"],)).fetchall()
        for row in rows:
            if row["status"] in ("OPEN", "PAID"):
                return row
        return _insert_invoice(c, lot["id"], lot["winner_account_id"],
                               int(lot["winning_price_cents"]), now)


def due_lots(now=None):
    """Lots the closer should process: LIVE past their close, plus any
    CLOSED lot whose fork never resolved (crash recovery, spec §3.4)."""
    now = now or _now()
    with _connect() as c:
        live = c.execute(
            "SELECT * FROM lots WHERE status = 'LIVE'"
            " AND current_close_at IS NOT NULL AND current_close_at <= ?"
            " ORDER BY current_close_at ASC", (_iso(now),)).fetchall()
        stuck = c.execute(
            "SELECT * FROM lots WHERE status = 'CLOSED'"
            " AND winner_account_id IS NULL").fetchall()
    return live + stuck


def run_closer(now=None):
    """One closer run (spec §3.2): every due lot processed in its own
    guarded close; one lot's failure never blocks the others."""
    now = now or _now()
    results = []
    for lot in due_lots(now):
        try:
            results.append(close_lot(lot["id"], now=now))
        except Exception as exc:  # noqa: BLE001 — per-lot isolation
            log.exception("closer failed on lot %s", lot["id"])
            results.append({"lot_id": lot["id"], "outcome": "error",
                            "error": str(exc)})
    summary = {"run_at": _iso(now), "lots_due": len(results),
               "results": results}
    for key in ("invoiced", "no_sale", "not_due", "skipped",
                "already_invoiced", "error"):
        summary[key] = sum(1 for r in results if r["outcome"] == key)
    return summary


def _closer_authorized():
    token = (request.headers.get("X-Auctions-Closer-Token", "")
             or request.args.get("token", ""))
    expected = os.environ.get("AUCTIONS_CLOSER_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Pay pages, Stripe Checkout, reminders, second chance (Slice 3, §§4–5, §7)
# ---------------------------------------------------------------------------
SECOND_CHANCE_WINDOW = timedelta(hours=72)  # offer expiry, spec §5.4


def _hash_token(raw_token):
    return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()


def pay_page_path(raw_token):
    return f"/pay/{raw_token}"


def _pay_page_url(raw_token):
    base = os.environ.get("AUCTIONS_PAY_BASE_URL", "").rstrip("/")
    return base + pay_page_path(raw_token) if base else pay_page_path(raw_token)


def _revoke_active_token(c, lot_id, reason, now=None):
    c.execute(
        "UPDATE pay_page_tokens SET revoked_at = ?, revoke_reason = ?"
        " WHERE lot_id = ? AND revoked_at IS NULL",
        (_iso(now or _now()), reason, lot_id))


def _mint_token(c, lot, invoice, now=None):
    """Mint a fresh pay token for (lot, invoice): any active predecessor
    is revoked first (revoke-and-replace, §4.2). Only the SHA-256 hash
    is stored; the raw value leaves with the caller and is never
    logged."""
    now = now or _now()
    _revoke_active_token(c, lot["id"], "REPLACED", now)
    raw = secrets.token_hex(32)
    token_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO pay_page_tokens (id, lot_id, invoice_id,"
        " winner_account_id, token_hash, issued_at)"
        " VALUES (?,?,?,?,?,?)",
        (token_id, lot["id"], invoice["id"], invoice["winner_account_id"],
         _hash_token(raw), _iso(now)))
    return {"token_id": token_id, "raw_token": raw,
            "pay_page_url": _pay_page_url(raw)}


def _open_invoice(c, lot_id):
    return c.execute(
        "SELECT * FROM invoices WHERE lot_id = ? AND status = 'OPEN'"
        " ORDER BY issued_at DESC LIMIT 1", (lot_id,)).fetchone()


def issue_pay_token(lot_id, now=None):
    """(Re)issue the active pay token for a lot's OPEN invoice. Returns
    the raw token + pay-page URL — the one place a raw token exists
    outside the winner's own browser."""
    now = now or _now()
    with _connect() as c:
        lot = _require_lot(c, lot_id)
        invoice = _open_invoice(c, lot_id)
        if not invoice:
            raise AuctionError("lot has no open invoice to pay")
        return _mint_token(c, lot, invoice, now)


def get_token_by_raw(raw_token):
    """Look up a pay token (any state) by its raw value. Hash-only
    storage means this is also the only lookup path."""
    if not raw_token:
        return None
    with _connect() as c:
        return c.execute(
            "SELECT * FROM pay_page_tokens WHERE token_hash = ?",
            (_hash_token(raw_token),)).fetchone()


def get_invoice(invoice_id):
    with _connect() as c:
        return c.execute("SELECT * FROM invoices WHERE id = ?",
                         (invoice_id,)).fetchone()


# --- Stripe session minting -------------------------------------------------
# Tests inject a stub via set_stripe_session_factory; production uses the
# stripe library with the api_key app.py configured for THIS brand
# service. No stripe_account is ever passed: auction charges land on
# the master account behind the brand service (manual settlement).
_stripe_session_factory = None


def set_stripe_session_factory(factory):
    global _stripe_session_factory
    _stripe_session_factory = factory


def _session_get(obj, key, default=None):
    if obj is None:
        return default
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        if key in obj:
            return obj[key]
    except Exception:  # noqa: BLE001 — StripeObject quirks
        pass
    return default


def _mint_checkout_session(lot, invoice, success_url, cancel_url):
    """Mint a FRESH Checkout session for the winning invoice (§4.4).
    Nothing is charged here — the winner completes checkout actively."""
    if _stripe_session_factory is not None:
        return _stripe_session_factory(lot, invoice, success_url,
                                       cancel_url)
    import stripe as stripe_lib
    if not stripe_lib.api_key:
        raise AuctionError(
            "Stripe is not configured on this service; cannot mint a "
            "checkout session")
    # Two line items (Slice 4): the hammer, then the 4% buyer premium
    # as its own line — the composition is transparent at the register.
    hammer = invoice.get("hammer_cents")
    if hammer is None:
        hammer = int(invoice["amount_cents"])
    premium = int(invoice.get("buyer_premium_cents") or 0)
    line_items = [{
        "price_data": {
            "currency": "usd",
            "unit_amount": int(hammer),
            "product_data": {
                "name": f"Auction lot — {lot['title']}",
                "tax_code": "txcd_99999999",
            },
        },
        "quantity": 1,
    }]
    if premium > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "unit_amount": premium,
                "product_data": {
                    "name": "Buyer premium (4% of hammer)",
                    "tax_code": "txcd_99999999",
                },
            },
            "quantity": 1,
        })
    return stripe_lib.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        metadata={
            "kind": "auction_pay",
            "lot_id": lot["id"],
            "invoice_id": invoice["id"],
            "winner_account_id": invoice["winner_account_id"],
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )


def handle_checkout_completed(session_obj, now=None):
    """checkout.session.completed for an auction pay session (§4.5).

    app.py verifies the Stripe signature before delegating; session
    metadata (kind=auction_pay) routes here. Idempotent: the invoice is
    re-read and its status checked before any write, and the UNIQUE
    stripe_payment_intent_id / stripe_checkout_session_id columns are
    the backstop wall — a replayed event lands on already_paid and
    writes nothing."""
    now = now or _now()
    session_id = _session_get(session_obj, "id")
    payment_intent = _session_get(session_obj, "payment_intent")
    meta = _session_get(session_obj, "metadata", {}) or {}
    invoice_id = _session_get(meta, "invoice_id")
    lot_id = _session_get(meta, "lot_id")
    if not session_id or not invoice_id:
        raise AuctionError("auction checkout session is missing ids")
    if not payment_intent:
        raise AuctionError("checkout session has no payment_intent yet")
    with _connect() as c:
        invoice = c.execute("SELECT * FROM invoices WHERE id = ?",
                            (invoice_id,)).fetchone()
        if not invoice:
            raise AuctionError("invoice not found for checkout session")
        if lot_id and invoice["lot_id"] != lot_id:
            raise AuctionError("session lot does not match the invoice")
        lot = _require_lot(c, invoice["lot_id"])
        if invoice["status"] == "PAID":
            if invoice["stripe_checkout_session_id"] == session_id:
                return {"outcome": "already_paid",
                        "invoice_id": invoice["id"], "lot_id": lot["id"]}
            raise AuctionError(
                "invoice already paid via a different checkout session")
        if invoice["status"] != "OPEN":
            raise AuctionError(f"invoice is {invoice['status']}, not OPEN")
        if lot["status"] != "INVOICED":
            raise AuctionError(f"lot is {lot['status']}, not INVOICED")
        if _session_get(meta, "winner_account_id") \
                and _session_get(meta, "winner_account_id") \
                != invoice["winner_account_id"]:
            raise AuctionError("session winner does not match the invoice")
        c.execute(
            "UPDATE invoices SET status = 'PAID', paid_at = ?,"
            " stripe_payment_intent_id = ?, stripe_checkout_session_id = ?"
            " WHERE id = ?",
            (_iso(now), payment_intent, session_id, invoice["id"]))
        c.execute(
            "UPDATE lots SET status = 'PAID', updated_at = ? WHERE id = ?",
            (_iso(now), lot["id"]))
        _revoke_active_token(c, lot["id"], "PAID", now)
        _notify(c, invoice["winner_account_id"], lot["id"],
                "PAYMENT_CONFIRMED", {
                    "lot_id": lot["id"], "invoice_id": invoice["id"],
                    "amount_cents": int(invoice["amount_cents"]),
                    "paid_at": _iso(now)})
    return {"outcome": "paid", "invoice_id": invoice_id,
            "lot_id": lot["id"]}


# --- Reminders (spec §7 PAYMENT_REMINDER) -----------------------------------
def run_reminders(now=None):
    """+24h / +48h unpaid reminders. The reminder flags on the invoice
    are stamped in the same transaction as the outbox row — that is the
    sole dedupe mechanism, so repeat runs never re-send (§4.6)."""
    now = now or _now()
    sent = []
    with _connect() as c:
        open_invoices = c.execute(
            "SELECT i.* FROM invoices i JOIN lots l ON l.id = i.lot_id"
            " WHERE i.status = 'OPEN' AND l.status = 'INVOICED'"
            " ORDER BY i.issued_at ASC").fetchall()
    for invoice in open_invoices:
        issued = _parse_ts(invoice["issued_at"])
        for hours, flag in ((24, "reminder_24h_sent_at"),
                            (48, "reminder_48h_sent_at")):
            if invoice[flag] or now < issued + timedelta(hours=hours):
                continue
            with _connect() as c:
                fresh = c.execute(
                    "SELECT * FROM invoices WHERE id = ?",
                    (invoice["id"],)).fetchone()
                if not fresh or fresh["status"] != "OPEN" or fresh[flag]:
                    continue
                c.execute(
                    f"UPDATE invoices SET {flag} = ? WHERE id = ?",
                    (_iso(now), invoice["id"]))
                _notify(c, fresh["winner_account_id"], fresh["lot_id"],
                        "PAYMENT_REMINDER", {
                            "lot_id": fresh["lot_id"],
                            "invoice_id": fresh["id"],
                            "amount_cents": int(fresh["amount_cents"]),
                            "payment_deadline_at":
                                fresh["payment_deadline_at"],
                            "reminder_number": 1 if hours == 24 else 2})
            sent.append({"invoice_id": invoice["id"],
                         "reminder_number": 1 if hours == 24 else 2})
    return {"run_at": _iso(now), "reminders_sent": sent,
            "count": len(sent)}


# --- Second-chance ladder (spec §5) ------------------------------------------
def _offered_account_ids(c, lot_id):
    rows = c.execute(
        "SELECT DISTINCT offeree_account_id FROM second_chance_offers"
        " WHERE lot_id = ?", (lot_id,)).fetchall()
    return {r["offeree_account_id"] for r in rows}


def _ranked_eligible_bids(c, lot_id):
    """All non-voided bids in second-chance order (spec §2.6): highest
    max first, earliest placed wins ties. Losing bids sit OUTBID, so
    the ladder must rank ACTIVE + OUTBID together — ACTIVE alone is
    only ever the leader."""
    return c.execute(
        "SELECT * FROM bids WHERE lot_id = ?"
        " AND status IN ('ACTIVE','OUTBID')"
        " ORDER BY max_bid_cents DESC, placed_at ASC, id ASC",
        (lot_id,)).fetchall()


def _eligible_runner_up(c, lot, exclude_account_ids):
    """Next-best bid (§5.2–5.3): ranked by max desc / earliest placed;
    suspended or email-unverified bidders are skipped."""
    ranked = _ranked_eligible_bids(c, lot["id"])
    seen = set()
    for bid in ranked:
        bidder_id = bid["bidder_account_id"]
        if bidder_id in seen or bidder_id in exclude_account_ids:
            continue
        seen.add(bidder_id)
        account = c.execute("SELECT * FROM accounts WHERE id = ?",
                            (bidder_id,)).fetchone()
        if not account or account["is_suspended"] \
                or not account["email_verified_at"]:
            continue
        return bid
    return None


def _relist_lot(c, lot, now):
    """Ladder exhaustion (§5.6): INVOICED -> RELISTED -> DRAFT with the
    winner fields cleared atomically; the lot re-enters moderation."""
    c.execute(
        "UPDATE second_chance_offers SET status = 'CANCELLED'"
        " WHERE lot_id = ? AND status = 'PENDING'", (lot["id"],))
    c.execute(
        "UPDATE lots SET status = 'RELISTED', winner_account_id = NULL,"
        " winning_price_cents = NULL, updated_at = ? WHERE id = ?",
        (_iso(now), lot["id"]))
    c.execute(
        "UPDATE lots SET status = 'DRAFT', updated_at = ? WHERE id = ?",
        (_iso(now), lot["id"]))
    _audit(c, lot["id"], None, "EDITED",
           "second-chance ladder exhausted; relisted to DRAFT")
    _notify(c, lot["seller_account_id"], lot["id"], "LOT_RELISTED", {
        "lot_id": lot["id"], "lot_title": lot["title"]})


def _insert_offer(c, lot, bid, original_invoice_id, offer_kind, now):
    """Write one PENDING second-chance offer + its outbox row. The
    offered price is always the offeree's own max (§5.3)."""
    offer_id = str(uuid.uuid4())
    expires = now + SECOND_CHANCE_WINDOW
    c.execute(
        "INSERT INTO second_chance_offers (id, lot_id,"
        " original_invoice_id, offer_kind, offeree_account_id,"
        " offered_price_cents, status, offered_at, expires_at,"
        " created_at) VALUES (?,?,?,?,?,?,'PENDING',?,?,?)",
        (offer_id, lot["id"], original_invoice_id, offer_kind,
         bid["bidder_account_id"], int(bid["max_bid_cents"]),
         _iso(now), _iso(expires), _iso(now)))
    _notify(c, bid["bidder_account_id"], lot["id"],
            "SECOND_CHANCE_OFFER", {
                "lot_id": lot["id"], "lot_title": lot["title"],
                "offer_kind": offer_kind,
                "offered_price_cents": int(bid["max_bid_cents"]),
                "offer_expires_at": _iso(expires)})
    return {"outcome": "offered", "lot_id": lot["id"],
            "offer_id": offer_id, "offer_kind": offer_kind,
            "offeree_account_id": bid["bidder_account_id"],
            "offered_price_cents": int(bid["max_bid_cents"])}


def _advance_ladder(c, lot, original_invoice_id, now):
    """Offer the lot to the next eligible runner-up, or relist when the
    ladder is exhausted. Runs inside the caller's transaction. Anyone
    who already defaulted on this lot (a VOID invoice in their name)
    is never re-offered it."""
    exclude = _offered_account_ids(c, lot["id"])
    exclude |= {r["winner_account_id"] for r in c.execute(
        "SELECT DISTINCT winner_account_id FROM invoices"
        " WHERE lot_id = ? AND status = 'VOID'",
        (lot["id"],)).fetchall()}
    if lot["winner_account_id"]:
        exclude.add(lot["winner_account_id"])
    bid = _eligible_runner_up(c, lot, exclude)
    if bid is None:
        _relist_lot(c, lot, now)
        return {"outcome": "relisted", "lot_id": lot["id"]}
    return _insert_offer(c, lot, bid, original_invoice_id,
                         "UNPAID_WINNER", now)


def _offer_reserve_not_met(c, lot, high_bid, now):
    """Gap-check C4: a reserve-not-met close has no invoice to void,
    but §7 still promises the high bidder a second-chance offer at
    their own max. Returns the offer result, or None when the high
    bidder is no longer eligible (suspended / unverified)."""
    if high_bid is None:
        return None
    account = c.execute("SELECT * FROM accounts WHERE id = ?",
                        (high_bid["bidder_account_id"],)).fetchone()
    if not account or account["is_suspended"] \
            or not account["email_verified_at"]:
        return None
    return _insert_offer(c, lot, high_bid, None, "RESERVE_NOT_MET", now)


def run_second_chance_sweep(now=None):
    """The 72h sweep (§§5.1, 5.6): void unpaid winner invoices, revoke
    their tokens, and start the ladder; expire stale PENDING offers and
    advance theirs. Idempotent — voided invoices and resolved offers
    are never reprocessed."""
    now = now or _now()
    results = []
    with _connect() as c:
        due = c.execute(
            "SELECT i.*, l.status AS lot_status FROM invoices i"
            " JOIN lots l ON l.id = i.lot_id"
            " WHERE i.status = 'OPEN' AND l.status = 'INVOICED'"
            " AND i.payment_deadline_at <= ?"
            " ORDER BY i.payment_deadline_at ASC",
            (_iso(now),)).fetchall()
    for row in due:
        with _connect() as c:
            invoice = c.execute("SELECT * FROM invoices WHERE id = ?",
                                (row["id"],)).fetchone()
            if not invoice or invoice["status"] != "OPEN":
                continue
            lot = _require_lot(c, invoice["lot_id"])
            if lot["status"] != "INVOICED":
                continue
            c.execute(
                "UPDATE invoices SET status = 'VOID', voided_at = ?,"
                " void_reason = 'UNPAID_DEADLINE' WHERE id = ?",
                (_iso(now), invoice["id"]))
            _revoke_active_token(c, lot["id"], "UNPAID_DEADLINE", now)
            results.append(_advance_ladder(c, lot, invoice["id"], now))
    with _connect() as c:
        expired = c.execute(
            "SELECT * FROM second_chance_offers WHERE status = 'PENDING'"
            " AND expires_at <= ? ORDER BY expires_at ASC",
            (_iso(now),)).fetchall()
    for offer in expired:
        with _connect() as c:
            fresh = c.execute(
                "SELECT * FROM second_chance_offers WHERE id = ?",
                (offer["id"],)).fetchone()
            if not fresh or fresh["status"] != "PENDING":
                continue
            c.execute(
                "UPDATE second_chance_offers SET status = 'EXPIRED'"
                " WHERE id = ?", (offer["id"],))
            _notify(c, fresh["offeree_account_id"], fresh["lot_id"],
                    "SECOND_CHANCE_EXPIRED", {
                        "lot_id": fresh["lot_id"],
                        "offered_price_cents":
                            int(fresh["offered_price_cents"])})
            lot = _require_lot(c, fresh["lot_id"])
            if lot["status"] == "INVOICED":
                results.append(_advance_ladder(
                    c, lot, fresh["original_invoice_id"], now))
            elif lot["status"] == "NO_SALE" \
                    and fresh["offer_kind"] == "RESERVE_NOT_MET":
                # Gap-check C4: the high-bidder offer lapsed — relist.
                _relist_lot(c, lot, now)
                results.append({"outcome": "relisted",
                                "lot_id": lot["id"]})
    return {"run_at": _iso(now), "results": results,
            "count": len(results)}


def get_offer(offer_id):
    with _connect() as c:
        return c.execute(
            "SELECT * FROM second_chance_offers WHERE id = ?",
            (offer_id,)).fetchone()


def _load_pending_offer(c, offer_id, account_id, now):
    offer = c.execute("SELECT * FROM second_chance_offers WHERE id = ?",
                      (offer_id,)).fetchone()
    if not offer:
        raise AuctionError("offer not found")
    if offer["offeree_account_id"] != account_id:
        raise PermissionDenied("this offer belongs to another account")
    if offer["status"] != "PENDING":
        raise AuctionError(f"offer is already {offer['status']}")
    if _parse_ts(offer["expires_at"]) <= now:
        raise AuctionError("offer has expired")
    return offer


def accept_second_chance(offer_id, account_id, now=None):
    """Accept path (§5.5): atomic winner swap — offer ACCEPTED, lot
    winner fields overwritten in place (status stays INVOICED), new
    OPEN invoice at the offered price, fresh pay token minted. A
    RESERVE_NOT_MET offer (gap-check C4) instead moves its NO_SALE lot
    to INVOICED with the high bidder as winner at the offered price."""
    now = now or _now()
    with _connect() as c:
        offer = _load_pending_offer(c, offer_id, account_id, now)
        lot = _require_lot(c, offer["lot_id"])
        is_rnm = offer["offer_kind"] == "RESERVE_NOT_MET"
        if is_rnm:
            if lot["status"] != "NO_SALE":
                raise AuctionError(
                    f"lot is {lot['status']}, not NO_SALE")
        elif lot["status"] != "INVOICED":
            raise AuctionError(f"lot is {lot['status']}, not INVOICED")
        c.execute(
            "UPDATE second_chance_offers SET status = 'ACCEPTED',"
            " responded_at = ? WHERE id = ?", (_iso(now), offer_id))
        c.execute(
            "UPDATE lots SET status = 'INVOICED',"
            " winner_account_id = ?,"
            " winning_price_cents = ?, current_price_cents = ?,"
            " leading_bidder_id = ?, updated_at = ? WHERE id = ?",
            (account_id, int(offer["offered_price_cents"]),
             int(offer["offered_price_cents"]), account_id,
             _iso(now), lot["id"]))
        invoice = _insert_invoice(c, lot["id"], account_id,
                                  int(offer["offered_price_cents"]), now)
        invoice_id = invoice["id"]
        lot = _require_lot(c, lot["id"])
        token = _mint_token(c, lot, invoice, now)
        _notify(c, account_id, lot["id"], "INVOICE_ISSUED", {
            "lot_id": lot["id"], "invoice_id": invoice_id,
            "hammer_cents": int(invoice["hammer_cents"]),
            "buyer_premium_cents": int(invoice["buyer_premium_cents"]),
            "amount_cents": int(invoice["amount_cents"]),
            "payment_deadline_at": invoice["payment_deadline_at"],
            "pay_page_url": token["pay_page_url"]})
    return {"outcome": "accepted", "offer_id": offer_id,
            "lot_id": lot["id"], "invoice_id": invoice_id,
            "hammer_cents": int(invoice["hammer_cents"]),
            "buyer_premium_cents": int(invoice["buyer_premium_cents"]),
            "amount_cents": int(invoice["amount_cents"]),
            "pay_page_url": token["pay_page_url"]}


def decline_second_chance(offer_id, account_id, now=None):
    """Decline path (§5.6): mark DECLINED and immediately advance the
    ladder to the next eligible runner-up (or relist). A declined
    RESERVE_NOT_MET offer relists directly — the high bidder was the
    only offer on that path (gap-check C4)."""
    now = now or _now()
    with _connect() as c:
        offer = _load_pending_offer(c, offer_id, account_id, now)
        lot = _require_lot(c, offer["lot_id"])
        c.execute(
            "UPDATE second_chance_offers SET status = 'DECLINED',"
            " responded_at = ? WHERE id = ?", (_iso(now), offer_id))
        if offer["offer_kind"] == "RESERVE_NOT_MET":
            if lot["status"] == "NO_SALE":
                _relist_lot(c, lot, now)
                return {"outcome": "declined", "offer_id": offer_id,
                        "lot_id": lot["id"], "lot_outcome": "relisted"}
            return {"outcome": "declined", "offer_id": offer_id}
        if lot["status"] != "INVOICED":
            return {"outcome": "declined", "offer_id": offer_id}
        result = _advance_ladder(c, lot, offer["original_invoice_id"],
                                 now)
    return {**result, "outcome": "declined", "offer_id": offer_id}


def run_payment_maintenance(now=None):
    """One payment-maintenance pass (reminders + second-chance sweep),
    run by the same Render Cron that runs the closer (§4.6)."""
    now = now or _now()
    return {"reminders": run_reminders(now),
            "second_chance": run_second_chance_sweep(now)}


# --- Settlements -------------------------------------------------------------

def get_settlement(lot_id):
    with _connect() as c:
        return c.execute("SELECT * FROM settlements WHERE lot_id = ?",
                         (lot_id,)).fetchone()


def record_settlement(lot_id, recorded_by_id, payout_method,
                      payout_reference=None, delivery_confirmed_at=None,
                      notes=None, now=None):
    """Record the seller payout for a PAID lot (manual settlement,
    Phase 1 — payout moves off-Stripe).

    Fee model (Bill 2026-10-02): the platform fee is buyer-side (the
    4% premium frozen on the invoice), so the seller is paid the
    hammer in full — gross = hammer, platform_fee_cents = 0, and the
    generated seller_payout_cents comes out equal to the hammer.
    Phase 1 has no time buffer: the admin's delivery confirmation is
    the release step, so buffer_release_at = payment cleared time.
    With delivery confirmed, the payout is released and the lot
    moves PAID -> SETTLED."""
    now = now or _now()
    recorder = get_account(recorded_by_id)
    if not recorder or not recorder["is_admin"]:
        raise PermissionDenied("only an admin can record a settlement")
    if not (payout_method or "").strip():
        raise AuctionError("payout_method is required")
    delivery = _parse_ts(delivery_confirmed_at) \
        if delivery_confirmed_at else None
    with _connect() as c:
        lot = _require_lot(c, lot_id)
        if lot["status"] != "PAID":
            raise AuctionError(f"lot is {lot['status']}, not PAID")
        invoice = c.execute(
            "SELECT * FROM invoices WHERE lot_id = ? AND status = 'PAID'"
            " ORDER BY issued_at DESC LIMIT 1", (lot_id,)).fetchone()
        if not invoice:
            raise AuctionError("lot has no PAID invoice to settle")
        if c.execute("SELECT id FROM settlements WHERE lot_id = ?",
                     (lot_id,)).fetchone():
            raise AuctionError("a settlement is already recorded for "
                               "this lot")
        hammer = invoice.get("hammer_cents")
        if hammer is None:
            hammer = int(invoice["amount_cents"])
        settlement_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO settlements (id, lot_id, invoice_id,"
            " seller_account_id, gross_amount_cents, platform_fee_cents,"
            " payout_method, payout_reference, payment_cleared_at,"
            " delivery_confirmed_at, buffer_release_at, released_at,"
            " notes, recorded_by_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (settlement_id, lot_id, invoice["id"],
             lot["seller_account_id"], int(hammer), 0,
             payout_method.strip(), payout_reference,
             invoice["paid_at"],
             _iso(delivery) if delivery else None,
             invoice["paid_at"],
             _iso(now) if delivery else None,
             notes, recorded_by_id, _iso(now), _iso(now)))
        row = c.execute("SELECT * FROM settlements WHERE id = ?",
                        (settlement_id,)).fetchone()
    if delivery:
        transition(lot_id, "SETTLED", actor_account_id=recorded_by_id)
    return row


# ---------------------------------------------------------------------------
# Comments & Q&A, watchlist, bidder profiles (Slice 4, best-practices #1/#5)
# ---------------------------------------------------------------------------
_PUBLIC_COMMENT_STATUSES = ("VISIBLE",)


def public_comment_dict(comment, author=None):
    """What the lot page shows. Hidden comments keep their body here
    only for the admin view (caller filters); email never appears."""
    if not comment:
        return None
    return {
        "id": comment["id"],
        "lot_id": comment["lot_id"],
        "parent_id": comment["parent_id"],
        "body": comment["body"],
        "is_seller": bool(comment["is_seller"]),
        "status": comment["status"],
        "created_at": comment["created_at"],
        "author": {
            "id": comment["account_id"],
            "display_name": author["display_name"] if author else None,
        },
    }


def list_comments(lot_id, include_hidden=False):
    sql = "SELECT * FROM comments WHERE lot_id = ?"
    if not include_hidden:
        sql += " AND status = 'VISIBLE'"
    sql += " ORDER BY created_at ASC, id ASC"
    with _connect() as c:
        rows = c.execute(sql, (lot_id,)).fetchall()
        out = []
        for row in rows:
            author = c.execute(
                "SELECT display_name FROM accounts WHERE id = ?",
                (row["account_id"],)).fetchone()
            out.append(public_comment_dict(row, author))
    return out


def post_comment(account_id, lot_id, body, parent_id=None):
    """Post a lot comment (best-practices #1). Same integrity gate as
    bidding: a real, email-verified, unsuspended account on the lot's
    brand. The seller's comments are flagged; other comments notify
    the seller (COMMENT_QUESTION), seller comments notify watchers
    and bidders (SELLER_REPLIED) via the §7 outbox."""
    account = get_account(account_id)
    if not account:
        raise AuctionError("account not found")
    if account["is_suspended"]:
        raise PermissionDenied("suspended accounts cannot comment")
    if not account["email_verified_at"]:
        raise PermissionDenied("email must be verified before commenting")
    body = (body or "").strip()
    if not body:
        raise AuctionError("comment body is required")
    if len(body) > COMMENT_MAX_CHARS:
        raise AuctionError(
            f"comment is limited to {COMMENT_MAX_CHARS} characters")
    with _connect() as c:
        lot = _require_lot(c, lot_id)
        if account["brand"] != lot["brand"]:
            raise AuctionError("account belongs to a different brand")
        if parent_id:
            parent = c.execute(
                "SELECT * FROM comments WHERE id = ?",
                (parent_id,)).fetchone()
            if not parent or parent["lot_id"] != lot_id:
                raise AuctionError("parent comment not found on this lot")
            if parent["status"] != "VISIBLE":
                raise AuctionError("cannot reply to a hidden comment")
        is_seller = account_id == lot["seller_account_id"]
        comment_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO comments (id, lot_id, account_id, parent_id,"
            " body, is_seller, status, created_at) VALUES"
            " (?,?,?,?,?,?,'VISIBLE',?)",
            (comment_id, lot_id, account_id, parent_id, body,
             is_seller, _iso(_now())))
        row = c.execute("SELECT * FROM comments WHERE id = ?",
                        (comment_id,)).fetchone()
        if is_seller:
            recipients = {r["account_id"] for r in c.execute(
                "SELECT account_id FROM watchlist WHERE lot_id = ?",
                (lot_id,)).fetchall()}
            recipients |= {r["bidder_account_id"] for r in c.execute(
                "SELECT DISTINCT bidder_account_id FROM bids"
                " WHERE lot_id = ? AND status != 'VOIDED'",
                (lot_id,)).fetchall()}
            recipients.discard(account_id)
            for rid in recipients:
                _notify(c, rid, lot_id, "SELLER_REPLIED", {
                    "lot_id": lot_id, "lot_title": lot["title"],
                    "comment_id": comment_id})
        else:
            _notify(c, lot["seller_account_id"], lot_id,
                    "COMMENT_QUESTION", {
                        "lot_id": lot_id, "lot_title": lot["title"],
                        "comment_id": comment_id})
    return public_comment_dict(row, account)


def set_comment_hidden(comment_id, admin_account_id, hidden=True,
                       note=None):
    """Admin moderation: hide (or restore) a comment. The hide is
    stamped on the row itself — who, when, why — so the moderation
    trail survives without a second table."""
    with _connect() as c:
        row = c.execute("SELECT * FROM comments WHERE id = ?",
                        (comment_id,)).fetchone()
        if not row:
            raise AuctionError("comment not found")
        if hidden:
            c.execute(
                "UPDATE comments SET status = 'HIDDEN', hidden_at = ?,"
                " hidden_by_id = ?, hide_note = ? WHERE id = ?",
                (_iso(_now()), admin_account_id, note, comment_id))
        else:
            c.execute(
                "UPDATE comments SET status = 'VISIBLE',"
                " hidden_at = NULL, hidden_by_id = NULL,"
                " hide_note = NULL WHERE id = ?", (comment_id,))
        row = c.execute("SELECT * FROM comments WHERE id = ?",
                        (comment_id,)).fetchone()
        author = c.execute(
            "SELECT display_name FROM accounts WHERE id = ?",
            (row["account_id"],)).fetchone()
    return public_comment_dict(row, author)


def is_watching(account_id, lot_id):
    if not account_id:
        return False
    with _connect() as c:
        return c.execute(
            "SELECT id FROM watchlist WHERE account_id = ? AND lot_id = ?",
            (account_id, lot_id)).fetchone() is not None


def set_watch(account_id, lot_id, watching):
    """Idempotent watch/unwatch. Returns the resulting state."""
    account = get_account(account_id)
    if not account:
        raise AuctionError("account not found")
    if account["is_suspended"]:
        raise PermissionDenied("suspended accounts cannot watch lots")
    lot = get_lot(lot_id)
    if not lot:
        raise AuctionError("lot not found")
    with _connect() as c:
        if watching:
            if _DIALECT == "postgres":
                stmt = ("INSERT INTO watchlist (id, account_id, lot_id,"
                        " created_at) VALUES (?,?,?,?)"
                        " ON CONFLICT (account_id, lot_id) DO NOTHING")
            else:
                stmt = ("INSERT OR IGNORE INTO watchlist (id, account_id,"
                        " lot_id, created_at) VALUES (?,?,?,?)")
            c.execute(stmt, (str(uuid.uuid4()), account_id, lot_id,
                             _iso(_now())))
        else:
            c.execute(
                "DELETE FROM watchlist WHERE account_id = ? AND lot_id = ?",
                (account_id, lot_id))
    return is_watching(account_id, lot_id)


def watched_lots(account_id):
    with _connect() as c:
        rows = c.execute(
            "SELECT l.* FROM watchlist w JOIN lots l ON l.id = w.lot_id"
            " WHERE w.account_id = ?"
            " ORDER BY w.created_at DESC", (account_id,)).fetchall()
    return rows


def bidder_profile(account_id):
    """Public, read-only bidder profile (best-practices #5): member
    since, activity counts, and closed-lot bid history. Amounts shown
    are effective (public-at-close) prices — another bidder's max is
    never exposed, and live-lot bids are not listed at all."""
    account = get_account(account_id)
    if not account:
        raise AuctionError("account not found")
    with _connect() as c:
        def count(sql, params):
            return c.execute(sql, params).fetchone()["n"]
        bids_placed = count(
            "SELECT COUNT(*) AS n FROM bids WHERE bidder_account_id = ?"
            " AND status != 'VOIDED'", (account_id,))
        won = count(
            "SELECT COUNT(*) AS n FROM lots WHERE winner_account_id = ?",
            (account_id,))
        sold = count(
            "SELECT COUNT(*) AS n FROM lots WHERE seller_account_id = ?"
            " AND status IN ('PAID','SETTLED')", (account_id,))
        history = c.execute(
            "SELECT b.effective_price_cents, b.placed_at, l.id AS lot_id,"
            " l.title AS lot_title, l.status AS lot_status,"
            " l.winner_account_id FROM bids b JOIN lots l"
            " ON l.id = b.lot_id WHERE b.bidder_account_id = ?"
            " AND b.status != 'VOIDED'"
            " AND l.status IN ('CLOSED','NO_SALE','INVOICED','PAID',"
            " 'SETTLED') ORDER BY b.placed_at DESC LIMIT 10",
            (account_id,)).fetchall()
    return {
        "id": account["id"],
        "brand": account["brand"],
        "display_name": account["display_name"],
        "member_since": account["created_at"],
        "bids_placed": bids_placed,
        "auctions_won": won,
        "lots_sold": sold,
        "recent_bids": [{
            "lot_id": r["lot_id"],
            "lot_title": r["lot_title"],
            "amount_cents": int(r["effective_price_cents"]),
            "placed_at": r["placed_at"],
            "lot_status": r["lot_status"],
            "won": r["winner_account_id"] == account_id,
        } for r in history],
    }


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
            image_keys=data.get("image_keys"),
            flaws=data.get("flaws"),
            video_keys=data.get("video_keys"),
            id_photo_keys=data.get("id_photo_keys"),
            no_ai_photos_attested=bool(
                data.get("no_ai_photos_attested")))
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
    return jsonify([admin_lot_dict(lot) for lot in lots])


@bp.get("/api/auctions/lots/<lot_id>/checklist")
def api_lot_checklist(lot_id):
    """The moderation listing-standard checklist for one lot (admin
    only — the detail carries reserve/attestation context)."""
    try:
        _require_admin()
    except AuctionError as exc:
        return _err(exc)
    lot = get_lot(lot_id)
    if not lot:
        return jsonify({"error": "lot not found"}), 404
    return jsonify(moderation_checklist(lot))


@bp.post("/api/auctions/lots/<lot_id>/listing")
def api_update_lot_listing(lot_id):
    """Seller fixes listing fields on their own DRAFT lot (the
    send-back loop after a moderation checklist failure)."""
    try:
        account = _require_account()
        data = request.get_json(silent=True) or {}
        fields = {k: data[k] for k in
                  ("description", "condition_notes", "flaws",
                   "image_keys", "video_keys", "id_photo_keys",
                   "no_ai_photos_attested") if k in data}
        lot = update_lot_listing(lot_id, account["id"], **fields)
    except AuctionError as exc:
        return _err(exc)
    view = public_lot_dict(lot, account["id"])
    view["moderation_checklist"] = moderation_checklist(lot)
    return jsonify(view)


@bp.get("/api/auctions/lots/<lot_id>/comments")
def api_list_comments(lot_id):
    lot = get_lot(lot_id)
    if not lot:
        return jsonify({"error": "lot not found"}), 404
    account = _current_account()
    is_owner = account and account["id"] == lot["seller_account_id"]
    if lot["status"] in ("DRAFT", "IN_MODERATION", "REJECTED") \
            and not is_owner and not _is_admin_request():
        return jsonify({"error": "lot not found"}), 404
    return jsonify(list_comments(
        lot_id, include_hidden=_is_admin_request()))


@bp.post("/api/auctions/lots/<lot_id>/comments")
def api_post_comment(lot_id):
    try:
        account = _require_account()
        data = request.get_json(silent=True) or {}
        comment = post_comment(
            account["id"], lot_id, data.get("body"),
            parent_id=data.get("parent_id"))
    except AuctionError as exc:
        return _err(exc)
    return jsonify(comment), 201


@bp.post("/api/auctions/comments/<comment_id>/moderate")
def api_moderate_comment(comment_id):
    try:
        _require_admin()
        data = request.get_json(silent=True) or {}
        hidden = (data.get("action") or "hide").strip().lower() != "restore"
        comment = set_comment_hidden(
            comment_id, _admin_actor_id(), hidden=hidden,
            note=data.get("note"))
    except AuctionError as exc:
        return _err(exc)
    return jsonify(comment)


@bp.post("/api/auctions/lots/<lot_id>/watch")
def api_set_watch(lot_id):
    try:
        account = _require_account()
        data = request.get_json(silent=True) or {}
        watching = data.get("watching", True)
        if isinstance(watching, str):
            watching = watching.strip().lower() in ("1", "true", "yes",
                                                    "on")
        state = set_watch(account["id"], lot_id, bool(watching))
    except AuctionError as exc:
        return _err(exc)
    return jsonify({"lot_id": lot_id, "watching": state})


@bp.get("/api/auctions/watchlist")
def api_watchlist():
    account = _require_account()
    rows = watched_lots(account["id"])
    return jsonify([public_lot_dict(r, account["id"]) for r in rows])


@bp.get("/api/auctions/bidders/<account_id>")
def api_bidder_profile(account_id):
    try:
        profile = bidder_profile(account_id)
    except AuctionError as exc:
        return _err(exc)
    return jsonify(profile)


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


def _request_ip():
    """Client IP for the bid audit log: first X-Forwarded-For hop when
    behind the Render proxy, else the direct peer."""
    if request.access_route:
        return request.access_route[0]
    return request.remote_addr


@bp.post("/api/auctions/lots/<lot_id>/bid")
def api_place_bid(lot_id):
    try:
        account = _require_account()
        data = request.get_json(silent=True) or {}
        result = place_bid(
            account["id"], lot_id, data.get("max_bid_cents"),
            ip_address=_request_ip(),
            user_agent=request.headers.get("User-Agent"))
    except AuctionError as exc:
        return _err(exc)
    return jsonify(result), 201


@bp.post("/api/auctions/closer/run")
def api_closer_run():
    """Token-gated closer endpoint for the Render Cron (spec §3.2). Not
    reachable without AUCTIONS_CLOSER_TOKEN; returns 404 when the token is
    not configured at all so the endpoint stays dark with the engine."""
    if not os.environ.get("AUCTIONS_CLOSER_TOKEN", ""):
        return jsonify({"error": "not found"}), 404
    if not _closer_authorized():
        return jsonify({"error": "forbidden"}), 403
    summary = run_closer()
    summary["payments"] = run_payment_maintenance()
    return jsonify(summary)


_PAY_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title></head>
<body><h1>{title}</h1><p>{body}</p></body></html>"""


@bp.get("/pay/<token>")
def pay_page(token):
    """Token pay page (winner-only, no-referrer/no-store): see
    _pay_page_impl. Every response from this route — page or redirect
    into Stripe — carries the two polish headers (Slice 4, §3.A)."""
    resp = make_response(_pay_page_impl(token))
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _pay_page_impl(token):
    """Winner pay page (§4.4): every click mints a FRESH Stripe Checkout
    session and redirects to it — a stale session URL in an old tab
    always self-heals. Nothing is charged until the winner completes
    checkout. A paid invoice renders a closed page instead."""
    token_row = get_token_by_raw(token)
    if token_row is None:
        return _PAY_PAGE_HTML.format(
            title="Link not found",
            body="This payment link is not valid."), 404
    if token_row["revoked_at"]:
        return _PAY_PAGE_HTML.format(
            title="Link replaced",
            body="This payment link has been replaced or has expired. "
                 "Use the newest link from your notifications."), 410
    invoice = get_invoice(token_row["invoice_id"])
    lot = get_lot(token_row["lot_id"])
    if not invoice or not lot:
        return _PAY_PAGE_HTML.format(
            title="Link not found",
            body="This payment link is not valid."), 404
    if invoice["status"] == "PAID":
        return _PAY_PAGE_HTML.format(
            title=f"Already paid · "
                  f"{BRAND_NAMES.get(lot['brand'], '')} Auctions",
            body=f"Payment for {_esc(lot['title'])} is complete."
            f"{_esc(_invoice_breakdown(invoice))} Thank you.")
    if invoice["status"] != "OPEN" or lot["status"] != "INVOICED":
        return _PAY_PAGE_HTML.format(
            title=f"No longer payable · "
                  f"{BRAND_NAMES.get(lot['brand'], '')} Auctions",
            body="This invoice is no longer open for payment."), 410
    if request.args.get("return"):
        return _PAY_PAGE_HTML.format(
            title=f"Payment processing · "
                  f"{BRAND_NAMES.get(lot['brand'], '')} Auctions",
            body=f"If you completed checkout, your payment is being"
            f" confirmed — this page will show as paid once Stripe"
            f" confirms it.{_esc(_invoice_breakdown(invoice))}")
    pay_url = request.url.split("?")[0]
    try:
        checkout = _mint_checkout_session(
            lot, invoice,
            success_url=pay_url + "?return=success",
            cancel_url=pay_url)
    except AuctionError as exc:
        return _PAY_PAGE_HTML.format(
            title=f"Checkout unavailable · "
                  f"{BRAND_NAMES.get(lot['brand'], '')} Auctions",
            body=str(exc)), 503
    url = _session_get(checkout, "url")
    if not url:
        return _PAY_PAGE_HTML.format(
            title=f"Checkout unavailable · "
                  f"{BRAND_NAMES.get(lot['brand'], '')} Auctions",
            body="Stripe did not return a checkout URL."), 502
    return redirect(url, code=302)


@bp.post("/api/auctions/second-chance/<offer_id>/accept")
def api_accept_second_chance(offer_id):
    try:
        account = _require_account()
        result = accept_second_chance(offer_id, account["id"])
    except AuctionError as exc:
        return _err(exc)
    return jsonify(result)


@bp.post("/api/auctions/second-chance/<offer_id>/decline")
def api_decline_second_chance(offer_id):
    try:
        account = _require_account()
        result = decline_second_chance(offer_id, account["id"])
    except AuctionError as exc:
        return _err(exc)
    return jsonify(result)


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
    return jsonify(public_lot_dict(
        lot, account_id=account["id"] if account else None))


@bp.get("/api/auctions/lots")
def api_list_lots():
    brand = request.args.get("brand") or _BRAND_CODE or None
    status_param = request.args.get("status")
    statuses = (tuple(s.strip().upper() for s in status_param.split(","))
                if status_param else ("LIVE", "SCHEDULED"))
    lots = list_public_lots(brand, statuses)
    account = _current_account()
    return jsonify([public_lot_dict(
        lot, account["id"] if account else None) for lot in lots])


# ---------------------------------------------------------------------------
# Public HTML pages (Slice 4, spec §6 as scoped by best-practices §3.B)
# ---------------------------------------------------------------------------
_PAGE_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#f6f1e7;color:#1b2a41;margin:0}
main{max-width:920px;margin:0 auto;padding:24px 16px 64px}
a{color:#1b2a41}
.card{background:#fff;border:1px solid #e2d9c3;border-radius:12px;
padding:20px;margin:16px 0}
.price{font-size:2rem;font-weight:700}
.accent{color:var(--accent)}
.flash{background:#fdf3d7;border:1px solid #e8a020;border-radius:8px;
padding:10px 14px;margin:12px 0}
button{background:var(--accent);border:0;border-radius:8px;color:#fff;
padding:10px 18px;font-weight:600;cursor:pointer}
input,textarea{width:100%;box-sizing:border-box;padding:10px;
border:1px solid #cbbd97;border-radius:8px;margin:6px 0}
.comment{border-top:1px solid #eee4cd;padding:10px 0}
.seller-badge{background:#1b2a41;color:#fff;border-radius:6px;
padding:1px 7px;font-size:.75rem;margin-left:6px}
img.lot-photo{max-width:100%;border-radius:8px;margin:8px 0}
img.brand-logo{width:46px;height:46px;border-radius:8px;
vertical-align:middle;margin-right:.5rem}
"""


def _page(title, body, brand=None):
    accent = BRAND_ACCENTS.get(brand, "#1b2a41")
    name = BRAND_NAMES.get(brand, "Auctions")
    home = BRAND_HOME_URLS.get(brand, BRAND_HOME_URLS["RE"])
    home_name = BRAND_NAMES.get(brand, BRAND_NAMES["RE"])
    logo = BRAND_LOGOS.get(brand, BRAND_LOGOS["RE"])
    canonical = request.url.split("?", 1)[0]
    description = (
        f"Live and upcoming {name} auctions, consignor intake, and "
        "account pages. No auctions are live unless a lot is listed here.")
    og_image = request.host_url.rstrip("/") + logo
    page_title = f"{title} · {name} Auctions"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,"
        "initial-scale=1\">"
        f"<title>{_esc(page_title)}</title>"
        f"<meta name=\"description\" content=\"{_esc(description)}\">"
        f"<link rel=\"canonical\" href=\"{_esc(canonical)}\">"
        f"<meta property=\"og:type\" content=\"website\">"
        f"<meta property=\"og:title\" content=\"{_esc(page_title)}\">"
        f"<meta property=\"og:description\" content=\"{_esc(description)}\">"
        f"<meta property=\"og:url\" content=\"{_esc(canonical)}\">"
        f"<meta property=\"og:image\" content=\"{_esc(og_image)}\">"
        f"<style>:root{{--accent:{accent}}}{_PAGE_CSS}</style></head>"
        f"<body><main><p><a href=\"{home}\"><img class=\"brand-logo\" "
        f"src=\"{logo}\" alt=\"{_esc(home_name)}\">&larr; Back to "
        f"{_esc(home_name)}</a></p>"
        f"<h1>{_esc(title)}</h1>{body}</main></body></html>")


def _flash():
    msg = request.args.get("msg", "").strip()
    if not msg:
        return ""
    return f"<div class=\"flash\">{_esc(msg)}</div>"


def _session_bar(account, brand=None):
    if account:
        return (f"<p>Signed in as {_esc(account['display_name'])} · "
                "<a href=\"/auctions/watchlist\">Watchlist</a> · "
                "<a href=\"/auctions/logout\">Sign out</a></p>")
    q = f"?brand={brand}" if brand in BRAND_NAMES else ""
    return (f"<p><a href=\"/auctions/login{q}\">Sign in</a> · "
            f"<a href=\"/auctions/register{q}\">Create account</a></p>")


def _money_form(form, key):
    text = (form.get(key) or "").strip().lstrip("$").replace(",", "")
    if not text:
        raise AuctionError(f"{key} is required")
    try:
        return int((Decimal(text) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise AuctionError(f"{key} must be a dollar amount")


def _comment_html(c):
    badge = ("<span class=\"seller-badge\">Seller</span>"
             if c["is_seller"] else "")
    return (
        f"<div class=\"comment\"><strong>"
        f"{_esc(c['author']['display_name'])}</strong>{badge} "
        f"<small>{_esc(c['created_at'])}</small>"
        f"<p>{_esc(c['body'])}</p></div>")


def _lot_card(lot):
    d = public_lot_dict(lot)
    price = (d["winning_price_cents"] if d.get("winning_price_cents")
             else d["current_price_cents"])
    return (
        f"<div class=\"card\"><h3><a href=\"/auctions/lot/"
        f"{_esc(lot['id'])}\">{_esc(lot['title'])}</a></h3>"
        f"<p class=\"price\">{_money(price)}</p>"
        f"<p>Status: {_esc(lot['status'])} · "
        f"{d.get('bid_count', 0)} bid(s)</p></div>")


def _cross_brand_card(target):
    """A cross-brand pointer card: on-brand lots stay put, but the
    sibling brand's auctions carry the categories named on the label
    (Bill's standing routing rule, 2026-10-02)."""
    if not target:
        return ""
    return (
        f"<div class=\"card\"><h3>Looking for {_esc(target['label'])}?</h3>"
        f"<p>{_esc(target['brand_name'])} auctions carry the "
        f"{_esc(target['label'])} side of the shop.</p>"
        f"<p><a href=\"/auctions?brand={_esc(target['brand'])}\">"
        f"See {_esc(target['brand_name'])} auctions &rarr;</a></p></div>")


# Consignor intake (Bill, 2026-10-05: "open and advertise" consignments
# ahead of the first live lots). Copy states only that we are accepting
# consignments for our first sales — it never claims a live lot exists
# and quotes no price or fee. The CTA is the brand's own register route
# (the seller intake: account creation with the "I plan to sell" flag).
_CONSIGN_COPY = {
    "RE": ("Have a classic to sell?",
           "Restoration Essentials auctions is now accepting consignments "
           "for our first sales — 1953–1973 American classics: muscle "
           "cars, trucks, and modern performance. Create a seller "
           "account and tell us about your car."),
    "IH": ("Have a classic motorcycle to sell?",
           "IronHead auctions is now accepting consignments for our "
           "first sales — classic motorcycles. Create a seller account "
           "and tell us about your bike."),
}


def _consign_card(brand):
    """The consignor-intake block for one brand's auctions index."""
    copy = _CONSIGN_COPY.get(brand)
    if not copy:
        return ""
    heading, text = copy
    return (
        f"<div class=\"card\"><h3>{_esc(heading)}</h3>"
        f"<p>{_esc(text)}</p>"
        f"<p><a href=\"/auctions/register?brand={_esc(brand)}\">"
        "Register to consign &rarr;</a></p></div>")


def _consign_cards(brand):
    """The consignor block for a known brand's index; on the brand-less
    index both brands' blocks show so every visitor sees a way in."""
    if brand in BRAND_NAMES:
        return _consign_card(brand)
    return _consign_card("RE") + _consign_card("IH")


def _lot_cross_brand_card(lot):
    """Lot-level routing: a lot whose category belongs on the sibling
    brand points the reader there; on-brand lots show the standing
    sibling link for their brand."""
    target = cross_brand_target(lot["brand"], lot["category"])
    if target:
        return _cross_brand_card(target)
    return _cross_brand_card(sibling_brand(lot["brand"]))


@bp.get("/auctions")
def auctions_index():
    brand = (request.args.get("brand") or _BRAND_CODE or "").strip() \
        .upper() or None
    lots = list_public_lots(brand, ("LIVE", "SCHEDULED"))
    cards = "".join(_lot_card(lot) for lot in lots) or (
        "<p>No auctions are live right now.</p>")
    # Standing cross-brand rule: the index always points at the sibling
    # brand's auctions (RE visitors get the motorcycle link, IH visitors
    # get the muscle-car/truck/modern-performance link).
    sibling = sibling_brand(brand) if brand else None
    return _page("Live & upcoming auctions",
                 _flash() + _session_bar(_current_account(), brand) + cards
                 + _consign_cards(brand)
                 + _cross_brand_card(sibling),
                 brand)


@bp.get("/auctions/lot/<lot_id>")
def auctions_lot_page(lot_id):
    lot = get_lot(lot_id)
    account = _current_account()
    is_owner = account and account["id"] == lot["seller_account_id"] \
        if lot else False
    if not lot or (lot["status"] in ("DRAFT", "IN_MODERATION", "REJECTED")
                   and not is_owner and not _is_admin_request()):
        req_brand = (request.args.get("brand") or "").strip().upper()
        if req_brand not in BRAND_NAMES:
            req_brand = _BRAND_CODE
        return _page("Not found", "<p>This lot is not available.</p>",
                     req_brand), 404
    d = public_lot_dict(lot, account["id"] if account else None)
    brand = lot["brand"]
    parts = [_flash(), _session_bar(account, brand)]
    if account and account["is_admin"]:
        admin_view = admin_lot_dict(lot)
        items = "".join(
            f"<li>{'✅' if i['ok'] else '❌'} {_esc(i['label'])} "
            f"({_esc(i['detail'])})</li>"
            for i in admin_view["moderation_checklist"]["items"])
        parts.append(
            f"<div class=\"card\"><h3>Moderation</h3><ul>{items}</ul>"
            f"<p>Reserve: {_money(admin_view['reserve_price_cents'])}"
            " (admin-only) · Attestation: "
            f"{'yes' if admin_view['no_ai_photos_attested'] else 'no'}"
            f"</p><p>ID photos: {len(admin_view['id_photo_keys'])}"
            "</p></div>")
    photos = "".join(
        f"<img class=\"lot-photo\" src=\"{_esc(k)}\""
        f" alt=\"{_esc(lot['title'])}\">" for k in d["image_keys"])
    videos = "".join(
        f"<p><video controls src=\"{_esc(k)}\"></video></p>"
        for k in d["video_keys"])
    price = (d["winning_price_cents"] if d.get("winning_price_cents")
             else d["current_price_cents"])
    reserve_line = ""
    if d["reserve_present"]:
        reserve_line = ("Reserve met" if d["reserve_met"]
                        else "Reserve not yet met")
    extended = (lot["extension_minutes_used"] or 0) > 0
    parts.append(f"""
<div class="card">
<p class="price">{_money(price)} <small>+ 4% buyer premium if you
win</small></p>
<p>{d['bid_count']} bid(s) · {reserve_line}</p>
<p>Closes <strong id="close-at">{_esc(lot['current_close_at'])}</strong>
{'— extended by soft-close bidding' if extended else ''}</p>
<p>Closes in <strong id="countdown">—</strong></p>
{photos}{videos}
<h3>About this lot</h3><p>{_esc(lot['description'])}</p>
<h3>Condition</h3><p>{_esc(lot['condition_notes']) or '—'}</p>
<h3>Known flaws</h3><p>{_esc(lot['flaws']) or '—'}</p>
<p>Seller <a href="/auctions/bidder/{_esc(lot['seller_account_id'])}">
{_esc(d['seller_display_name'])}</a></p>
</div>
<script>
const closeEl = document.getElementById('close-at');
const cd = document.getElementById('countdown');
function tick() {{
  const t = new Date(closeEl.textContent.trim()).getTime();
  let s = Math.max(0, Math.floor((t - Date.now()) / 1000));
  const dd = Math.floor(s / 86400); s -= dd * 86400;
  const hh = Math.floor(s / 3600); s -= hh * 3600;
  const mm = Math.floor(s / 60); const ss = s - mm * 60;
  cd.textContent = (dd ? dd + 'd ' : '') + hh + 'h ' + mm + 'm ' + ss + 's';
}}
tick(); setInterval(tick, 1000);
</script>""")
    if account:
        watching = d.get("watching")
        parts.append(f"""
<div class="card">
<form method="post" action="/auctions/lot/{_esc(lot_id)}/watch">
<button type="submit" name="watching" value="{'0' if watching else '1'}">
{'Unwatch this lot' if watching else 'Watch this lot'}</button></form>
</div>""")
        if lot["status"] == "LIVE":
            parts.append(f"""
<div class="card"><h3>Place a bid</h3>
<form method="post" action="/auctions/lot/{_esc(lot_id)}/bid">
<label>Your maximum bid (USD)
<input name="amount" inputmode="decimal" placeholder="e.g. 12500">
</label><button type="submit">Place bid</button></form>
<p>Proxy bidding: you pay the lowest price that still beats the
next bidder, never more than your max. Winning price + 4% buyer
premium is the amount due.</p></div>""")
    comments = "".join(_comment_html(c) for c in list_comments(lot_id))
    parts.append(f"""
<div class="card"><h3>Comments &amp; Q&amp;A</h3>
<p><small>{_esc(COMMENT_RULES)}</small></p>
{comments or '<p>No comments yet.</p>'}
{"<form method='post' action='/auctions/lot/" + _esc(lot_id) +
"/comment'><textarea name='body' rows='3' placeholder='Ask about "
"this lot…'></textarea><button type='submit'>Post comment</button>"
"</form>" if account else
f'<p><a href="/auctions/login?brand={brand}">Sign in</a> to '
"comment.</p>"}
</div>""")
    return _page(lot["title"],
                 "".join(parts) + _lot_cross_brand_card(lot), brand)


@bp.post("/auctions/lot/<lot_id>/bid")
def auctions_place_bid(lot_id):
    account = _require_account()
    amount = _money_form(request.form, "amount")
    place_bid(account["id"], lot_id, amount)
    return redirect(f"/auctions/lot/{lot_id}?msg=Bid+placed")


@bp.post("/auctions/lot/<lot_id>/comment")
def auctions_post_comment(lot_id):
    account = _require_account()
    parent = request.form.get("parent_id") or None
    post_comment(account["id"], lot_id, request.form.get("body"),
                 parent_id=parent)
    return redirect(f"/auctions/lot/{lot_id}?msg=Comment+posted")


@bp.post("/auctions/lot/<lot_id>/watch")
def auctions_set_watch(lot_id):
    account = _require_account()
    watching = (request.form.get("watching") or "1") not in ("0", "false")
    set_watch(account["id"], lot_id, watching)
    return redirect(
        f"/auctions/lot/{lot_id}?msg="
        + ("Watching+this+lot" if watching else "Removed+from+watchlist"))


@bp.get("/auctions/watchlist")
def auctions_watchlist_page():
    account = _current_account()
    if not account:
        return redirect("/auctions/login")
    cards = "".join(_lot_card(lot) for lot in
                    watched_lots(account["id"])) or (
        "<p>You're not watching any lots yet.</p>")
    return _page("Your watchlist", _flash() + cards, account["brand"])


@bp.get("/auctions/bidder/<account_id>")
def auctions_bidder_page(account_id):
    try:
        profile = bidder_profile(account_id)
    except AuctionError:
        return _page("Not found", "<p>Bidder not found.</p>",
                     _BRAND_CODE), 404
    history = "".join(
        f"<li>{_esc(b['lot_title'])} — {_money(b['amount_cents'])}"
        f" · {_esc(b['lot_status'])}"
        f"{' · won' if b['won'] else ''}</li>"
        for b in profile["recent_bids"]) or "<li>No bids yet.</li>"
    body = f"""
<div class="card">
<p>Member since {_esc(profile['member_since'])}</p>
<p>{profile['bids_placed']} bid(s) placed ·
{profile['auctions_won']} auction(s) won ·
{profile['lots_sold']} lot(s) sold</p>
<h3>Recent bids (closed lots)</h3><ul>{history}</ul></div>"""
    return _page(profile["display_name"], body, profile["brand"])


def _request_brand(fallback=None):
    """The brand a page should present as: an explicit valid ?brand=
    wins (cross-brand visitors keep their brand's chrome), then the
    caller's fallback, then the service's own brand code."""
    brand = (request.args.get("brand") or "").strip().upper()
    if brand in BRAND_NAMES:
        return brand
    if fallback in BRAND_NAMES:
        return fallback
    return _BRAND_CODE


@bp.get("/auctions/login")
def auctions_login_page():
    if _current_account():
        return redirect("/auctions")
    brand = _request_brand()
    return _page("Sign in", f"""{_flash()}
<form method="post" action="/auctions/login">
<input type="hidden" name="brand" value="{_esc(brand)}">
<label>Email <input name="email" type="email" required></label>
<label>Password <input name="password" type="password" required>
</label><button type="submit">Sign in</button></form>
<p>No account? <a href="/auctions/register?brand={_esc(brand)}">
Create one</a></p>""",
                 brand)


@bp.post("/auctions/login")
def auctions_login():
    brand = (request.form.get("brand") or "").strip().upper()
    if brand not in BRAND_NAMES:
        brand = _BRAND_CODE
    account = authenticate(brand, request.form.get("email"),
                           request.form.get("password"))
    if not account:
        return redirect(
            f"/auctions/login?brand={brand}&msg=Invalid+credentials")
    if account["is_suspended"]:
        return redirect(
            f"/auctions/login?brand={brand}&msg=Account+suspended")
    session[SESSION_KEY] = account["id"]
    return redirect(f"/auctions?brand={account['brand']}")


@bp.get("/auctions/logout")
def auctions_logout():
    session.pop(SESSION_KEY, None)
    return redirect("/auctions")


@bp.get("/auctions/register")
def auctions_register_page():
    if _current_account():
        return redirect("/auctions")
    brand = _request_brand() or "RE"
    return _page("Create account", f"""{_flash()}
<form method="post" action="/auctions/register">
<input type="hidden" name="brand" value="{_esc(brand)}">
<label>Display name <input name="display_name" required></label>
<label>Email <input name="email" type="email" required></label>
<label>Password (10+ characters, letters and numbers)
<input name="password" type="password" required></label>
<label><input type="checkbox" name="is_seller" value="1"
style="width:auto"> I plan to sell</label>
<button type="submit">Create account</button></form>""", brand)


@bp.post("/auctions/register")
def auctions_register():
    account = create_account(
        request.form.get("brand") or _BRAND_CODE,
        request.form.get("email"), request.form.get("display_name"),
        request.form.get("password"),
        is_seller=bool(request.form.get("is_seller")))
    session[SESSION_KEY] = account["id"]
    return redirect(f"/auctions?brand={account['brand']}")


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
