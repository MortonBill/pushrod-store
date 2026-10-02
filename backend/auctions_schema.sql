-- ============================================================================
-- Auction Engine — Postgres schema (PRODUCTION source of truth)
-- Source: auction-engine-spec-claude-2026-10-02.md §1 (Claude, 2026-10-02),
-- extracted verbatim for §§1.2–1.11. Two repairs, marked inline:
--   (1) `invoices` is created before `pay_page_tokens` (the spec lists the
--       token table first, but it REFERENCES invoices — Postgres requires
--       the referenced table to exist).
--   (2) §1.12 `second_chance_offers` was truncated mid-DDL in the saved
--       spec; the remaining columns are completed following the `invoices`
--       pattern (status + offered/expires/responded timestamps).
-- Added beyond the saved spec (enums for both exist in §1.3, table bodies
-- were in the unsaved §§6–7): `moderation_actions` (moderation queue audit)
-- and `notifications` (event outbox). Both are marked [§6/§7-derived].
--
-- Runtime note: backend/auctions.py carries a SQLite mirror of this schema
-- for local/dev per repo convention (see wholesale.py). This file is what a
-- production Postgres (Render, ~$6/mo per the migration plan) is built from.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), gen_random_bytes()
CREATE EXTENSION IF NOT EXISTS btree_gist; -- exclusion constraints on ranges (future)

-- ---------------------------------------------------------------- enums
CREATE TYPE lot_status AS ENUM (
    'DRAFT',          -- seller has submitted; not yet in mod queue
    'IN_MODERATION',  -- admin has claimed it for review
    'REJECTED',       -- admin rejected; terminal unless re-submitted
    'SCHEDULED',      -- approved, awaiting go-live datetime
    'RENDER_CHECK',   -- automated smoke-check in progress
    'RENDER_FAILED',  -- smoke-check failed; lot cannot go live
    'LIVE',           -- accepting bids
    'CLOSED',         -- bidding period ended; winner computation pending or done
    'NO_SALE',        -- closed with zero bids OR reserve not met; terminal
    'INVOICED',       -- winner identified; pay-page token issued
    'PAID',           -- Stripe Checkout confirmed
    'SETTLED',        -- seller payout recorded and released
    'RELISTED',       -- pulled back to DRAFT after unpaid/second-chance exhausted
    'CANCELLED'       -- admin cancelled at any pre-PAID stage; terminal
);

-- ALLOWED TRANSITIONS (enforced in application layer — backend/auctions.py
-- TRANSITIONS — from spec §1.3):
-- DRAFT             -> IN_MODERATION, CANCELLED
-- IN_MODERATION     -> SCHEDULED, REJECTED, DRAFT (send back), CANCELLED
-- REJECTED          -> DRAFT (re-submit)
-- SCHEDULED         -> RENDER_CHECK, CANCELLED
-- RENDER_CHECK      -> LIVE, RENDER_FAILED
-- RENDER_FAILED     -> SCHEDULED (reschedule after fix), CANCELLED
-- LIVE              -> CLOSED
-- CLOSED            -> NO_SALE, INVOICED       <- the critical fork; see below
-- NO_SALE           -> RELISTED                <- only allowed transition
-- INVOICED          -> PAID, RELISTED          <- RELISTED when second-chance exhausted
-- PAID              -> SETTLED
-- SETTLED           -> (terminal)
-- CANCELLED         -> (terminal)
-- RELISTED          -> DRAFT                   <- restarts lifecycle

CREATE TYPE brand_id AS ENUM ('RE', 'IH');

CREATE TYPE bid_status AS ENUM (
    'ACTIVE',    -- currently valid
    'OUTBID',    -- superseded by a higher proxy
    'VOIDED'     -- admin-voided; excluded from all winner/price calculations
);

CREATE TYPE offer_status AS ENUM (
    'PENDING',
    'ACCEPTED',
    'DECLINED',
    'EXPIRED',
    'CANCELLED'  -- admin cancelled before expiry
);

CREATE TYPE invoice_status AS ENUM (
    'OPEN',
    'PAID',
    'VOID'       -- voided when second-chance supersedes or admin cancels
);

CREATE TYPE moderation_action_type AS ENUM (
    'SUBMITTED',
    'CLAIMED',
    'APPROVED',
    'REJECTED',
    'SENT_BACK',
    'EDITED',
    'RENDER_PASS',
    'RENDER_FAIL',
    'CANCELLED'
);

CREATE TYPE notification_event AS ENUM (
    'OUTBID',
    'WINNING',          -- your proxy now leads after someone else bid
    'AUCTION_WON',
    'INVOICE_ISSUED',
    'PAYMENT_REMINDER',
    'PAYMENT_CONFIRMED',
    'SECOND_CHANCE_OFFER',
    'SECOND_CHANCE_EXPIRED',
    'LOT_RELISTED',
    'LOT_CANCELLED',
    'RESERVE_NOT_MET'   -- internal/seller only
);

-- ---------------------------------------------------------------- accounts
CREATE TABLE accounts (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand                       brand_id NOT NULL,
    email                       text NOT NULL,
    email_verified_at           timestamptz,          -- NULL = not verified; bids blocked
    display_name                text NOT NULL,
    password_hash               text NOT NULL,        -- bcrypt
    is_admin                    boolean NOT NULL DEFAULT false,
    is_seller                   boolean NOT NULL DEFAULT false,
    is_suspended                boolean NOT NULL DEFAULT false,
    stripe_customer_id          text,                 -- per-brand Stripe customer
    stripe_connect_account_id   text,                 -- Phase 2; NULL in Phase 1
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_accounts_brand_email UNIQUE (brand, email)
);

CREATE INDEX idx_accounts_brand ON accounts (brand);
CREATE INDEX idx_accounts_email ON accounts (email);

-- ---------------------------------------------------------------- lots
-- The critical invariant: the transition out of CLOSED is the only place a
-- winner or no-sale determination is recorded, atomically with the status.
-- There is NO `reserve_met` boolean column — reserve outcome is DERIVED at
-- close time and selects NO_SALE vs INVOICED. A lot can therefore never be
-- both SOLD and reserve-not-met (the old Polsia contradiction).
CREATE TABLE lots (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand                   brand_id NOT NULL,
    seller_account_id       uuid NOT NULL REFERENCES accounts(id),

    -- Content
    title                   text NOT NULL CHECK (char_length(title) BETWEEN 3 AND 200),
    description             text NOT NULL,
    category                text NOT NULL,
    condition_notes         text,
    image_keys              text[] NOT NULL DEFAULT '{}',  -- S3/R2 object keys

    -- Pricing
    starting_price_cents    bigint NOT NULL CHECK (starting_price_cents >= 0),
    reserve_price_cents     bigint,                        -- NULL = no reserve
    -- NEVER EXPOSED TO BIDDERS. Queried only inside closer + admin.

    -- Live pricing state (denormalized for read performance; authoritative on bids table)
    current_price_cents     bigint NOT NULL DEFAULT 0,
    -- 0 until first bid; equals winning_price calculation at close

    leading_bidder_id       uuid REFERENCES accounts(id),
    -- NULL until first bid placed

    -- Scheduling
    scheduled_start_at      timestamptz,
    scheduled_close_at      timestamptz,               -- original scheduled close
    current_close_at        timestamptz,               -- mutated by soft-close extensions
    extension_minutes_used  integer NOT NULL DEFAULT 0 CHECK (extension_minutes_used >= 0),
    -- CAP: 120 minutes = 24 extensions of 5 min each

    -- Status
    status                  lot_status NOT NULL DEFAULT 'DRAFT',

    -- Winner (set atomically when transitioning CLOSED->INVOICED)
    winner_account_id       uuid REFERENCES accounts(id),
    winning_price_cents     bigint,

    -- Moderation
    moderated_by_id         uuid REFERENCES accounts(id),
    moderation_note         text,

    -- Render check
    last_render_check_at    timestamptz,
    last_render_check_ok    boolean,

    -- Audit
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    -- ----------------------------------------------------------------
    -- STRUCTURAL INVARIANTS
    -- ----------------------------------------------------------------

    -- A winner exists iff the lot is in a post-win status
    CONSTRAINT chk_winner_consistency CHECK (
        (status IN ('INVOICED', 'PAID', 'SETTLED')
            AND winner_account_id IS NOT NULL
            AND winning_price_cents IS NOT NULL
            AND winning_price_cents > 0)
        OR
        (status NOT IN ('INVOICED', 'PAID', 'SETTLED')
            AND winner_account_id IS NULL
            AND winning_price_cents IS NULL)
    ),

    -- NO_SALE must never have a winner
    CONSTRAINT chk_no_sale_no_winner CHECK (
        NOT (status = 'NO_SALE' AND winner_account_id IS NOT NULL)
    ),

    -- Reserve must be >= starting price if set
    CONSTRAINT chk_reserve_gte_start CHECK (
        reserve_price_cents IS NULL
        OR reserve_price_cents >= starting_price_cents
    ),

    -- Soft-close cap: 120 minutes
    CONSTRAINT chk_extension_cap CHECK (extension_minutes_used <= 120),

    -- current_close_at must be >= scheduled_close_at when set
    CONSTRAINT chk_close_times CHECK (
        current_close_at IS NULL
        OR scheduled_close_at IS NULL
        OR current_close_at >= scheduled_close_at
    ),

    -- Seller cannot be the leading bidder (belt + suspenders; also enforced in app)
    CONSTRAINT chk_seller_not_leader CHECK (
        leading_bidder_id IS NULL
        OR leading_bidder_id <> seller_account_id
    )
);

CREATE INDEX idx_lots_brand ON lots (brand);
CREATE INDEX idx_lots_status ON lots (status);
CREATE INDEX idx_lots_current_close_at ON lots (current_close_at) WHERE status = 'LIVE';
CREATE INDEX idx_lots_seller ON lots (seller_account_id);
CREATE INDEX idx_lots_brand_status ON lots (brand, status);

-- ---------------------------------------------------------------- bids
CREATE TABLE bids (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id              uuid NOT NULL REFERENCES lots(id),
    bidder_account_id   uuid NOT NULL REFERENCES accounts(id),

    -- The bidder's secret maximum. Never shown to anyone except the bidder + admins.
    max_bid_cents       bigint NOT NULL CHECK (max_bid_cents > 0),

    -- The computed price at which this bid is currently "active" —
    -- i.e. the minimum amount this bid needs to lead.
    -- Updated by the proxy engine when a new bid arrives.
    effective_price_cents bigint NOT NULL CHECK (effective_price_cents > 0),

    status              bid_status NOT NULL DEFAULT 'ACTIVE',

    -- Proxy metadata
    proxy_rank          integer,
    -- Tiebreak: when two max_bids are equal, the earlier-placed bid wins.
    -- proxy_rank is NULL for all but the top-2 bids at any moment.

    placed_at           timestamptz NOT NULL DEFAULT now(),

    -- Bid-time client fingerprint (Slice 3, gap-check C3 / spec §8.2):
    -- recorded on every accepted bid; rejected attempts land in
    -- bid_attempts below with the same fields.
    ip_address          text,
    user_agent          text,

    voided_at           timestamptz,
    voided_by_id        uuid REFERENCES accounts(id),
    void_reason         text,

    -- Soft-close trigger: did placing this bid extend the auction?
    triggered_extension boolean NOT NULL DEFAULT false,

    created_at          timestamptz NOT NULL DEFAULT now(),

    -- Invariants
    CONSTRAINT chk_void_fields CHECK (
        (status = 'VOIDED') = (voided_at IS NOT NULL)
    ),
    CONSTRAINT chk_effective_lte_max CHECK (
        effective_price_cents <= max_bid_cents
    )
);

CREATE INDEX idx_bids_lot ON bids (lot_id);
CREATE INDEX idx_bids_bidder ON bids (bidder_account_id);
CREATE INDEX idx_bids_lot_status ON bids (lot_id, status);
CREATE INDEX idx_bids_lot_placed ON bids (lot_id, placed_at);

-- Used by the winner computation query
CREATE INDEX idx_bids_lot_active_max
    ON bids (lot_id, max_bid_cents DESC, placed_at ASC)
    WHERE status = 'ACTIVE';

-- ------------------------------------------------------------ bid attempts
-- Audit log for EVERY bid attempt, accepted or rejected (spec §8.2).
-- No FKs on lot/account: a rejected attempt may reference a lot or
-- account that failed validation, and the attempt must still be kept.
-- IPs are stored as text (proxies may present non-inet forms).
CREATE TABLE IF NOT EXISTS bid_attempts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id              text,
    bidder_account_id   text,
    max_bid_cents       bigint,
    bid_id              text,              -- set when the attempt produced a bid
    ip_address          text,
    user_agent          text,
    outcome             text NOT NULL CHECK (outcome IN ('ACCEPTED','REJECTED')),
    reason              text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bid_attempts_lot ON bid_attempts (lot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bid_attempts_bidder ON bid_attempts (bidder_account_id, created_at);

-- ---------------------------------------------------------------- increment rules
-- Per-brand increment table. Rows are mutually exclusive price ranges.
-- "If current price is in [range_low_cents, range_high_cents), increment = increment_cents"
-- range_high_cents NULL = unbounded upper end.
CREATE TABLE increment_rules (
    id                  serial PRIMARY KEY,
    brand               brand_id NOT NULL,
    range_low_cents     bigint NOT NULL CHECK (range_low_cents >= 0),
    range_high_cents    bigint,   -- NULL = no upper bound
    increment_cents     bigint NOT NULL CHECK (increment_cents > 0),
    effective_from      date NOT NULL DEFAULT CURRENT_DATE,
    -- allows future rule changes without breaking historical lots
    effective_to        date,

    CONSTRAINT uq_increment_brand_range UNIQUE (brand, range_low_cents, effective_from),
    CONSTRAINT chk_range_order CHECK (
        range_high_cents IS NULL OR range_high_cents > range_low_cents
    )
);

-- RestorationEssentials (cars, up to tens of thousands)
INSERT INTO increment_rules (brand, range_low_cents, range_high_cents, increment_cents) VALUES
    ('RE',       0,      2500,      100),    -- $0 – $24.99 -> $1.00 increment
    ('RE',    2500,      5000,      250),    -- $25 – $49.99 -> $2.50
    ('RE',    5000,     25000,      500),    -- $50 – $249.99 -> $5.00
    ('RE',   25000,    100000,     1000),    -- $250 – $999.99 -> $10.00
    ('RE',  100000,    500000,     2500),    -- $1,000 – $4,999.99 -> $25.00
    ('RE',  500000,   1000000,     5000),    -- $5,000 – $9,999.99 -> $50.00
    ('RE', 1000000,   2500000,    10000),    -- $10,000 – $24,999.99 -> $100.00
    ('RE', 2500000,       NULL,    25000);   -- $25,000+ -> $250.00

-- IronHead (vintage motorcycle parts/guides, ~1/10th scale)
INSERT INTO increment_rules (brand, range_low_cents, range_high_cents, increment_cents) VALUES
    ('IH',       0,      1000,       50),   -- $0 – $9.99 -> $0.50
    ('IH',    1000,      2500,      100),   -- $10 – $24.99 -> $1.00
    ('IH',    2500,     10000,      250),   -- $25 – $99.99 -> $2.50
    ('IH',   10000,     25000,      500),   -- $100 – $249.99 -> $5.00
    ('IH',   25000,    100000,     1000),   -- $250 – $999.99 -> $10.00
    ('IH',  100000,    250000,     2500),   -- $1,000 – $2,499.99 -> $25.00
    ('IH',  250000,       NULL,     5000);  -- $2,500+ -> $50.00

CREATE INDEX idx_increment_brand_low ON increment_rules (brand, range_low_cents);

-- ---------------------------------------------------------------- invoices
-- NOTE (repair 1): created before pay_page_tokens; see header.
CREATE TABLE invoices (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id                      uuid NOT NULL REFERENCES lots(id),
    winner_account_id           uuid NOT NULL REFERENCES accounts(id),

    amount_cents                bigint NOT NULL CHECK (amount_cents > 0),
    status                      invoice_status NOT NULL DEFAULT 'OPEN',

    -- Stripe data
    -- We do NOT store Checkout session IDs here because sessions are minted
    -- fresh on each pay-page click. We store the completed session on payment.
    stripe_payment_intent_id    text UNIQUE,   -- set when payment confirmed
    stripe_checkout_session_id  text UNIQUE,   -- the session that completed
    stripe_customer_id          text,

    issued_at                   timestamptz NOT NULL DEFAULT now(),
    paid_at                     timestamptz,
    voided_at                   timestamptz,
    void_reason                 text,

    -- Reminder tracking
    reminder_24h_sent_at        timestamptz,
    reminder_48h_sent_at        timestamptz,

    -- Deadline: 72h after issued_at; cron checks this
    payment_deadline_at         timestamptz NOT NULL
        GENERATED ALWAYS AS (issued_at + INTERVAL '72 hours') STORED,

    -- Slice 3: one OPEN invoice per lot (partial unique index below);
    -- voided invoices remain for audit, so no UNIQUE(lot_id) — a
    -- second-chance accept legitimately issues a second invoice.

    CONSTRAINT chk_paid_fields CHECK (
        (status = 'PAID') =
        (paid_at IS NOT NULL
            AND stripe_payment_intent_id IS NOT NULL
            AND stripe_checkout_session_id IS NOT NULL)
    ),

    CONSTRAINT chk_void_fields CHECK (
        (status = 'VOID') = (voided_at IS NOT NULL)
    )
);

CREATE INDEX idx_invoices_lot ON invoices (lot_id);
CREATE INDEX idx_invoices_status ON invoices (status);
CREATE INDEX idx_invoices_deadline ON invoices (payment_deadline_at) WHERE status = 'OPEN';
CREATE UNIQUE INDEX uq_invoices_open_per_lot ON invoices (lot_id)
    WHERE status = 'OPEN';

-- ---------------------------------------------------------------- pay page tokens
CREATE TABLE pay_page_tokens (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id          uuid NOT NULL REFERENCES lots(id),
    invoice_id      uuid NOT NULL REFERENCES invoices(id),
    winner_account_id uuid NOT NULL REFERENCES accounts(id),

    -- HMAC-SHA256 token: see spec §4 for format
    token_hash      text NOT NULL,         -- SHA-256 hex of the raw token
    -- The raw token is never stored; only the hash.

    issued_at       timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz,
    revoke_reason   text,

    -- Slice 3 (resolved): the partial unique index below enforces one
    -- ACTIVE token per lot while revoked predecessors stay for audit.

    CONSTRAINT chk_revoke_fields CHECK (
        (revoked_at IS NULL) = (revoke_reason IS NULL)
    )
);

CREATE INDEX idx_pay_page_tokens_token_hash ON pay_page_tokens (token_hash);
CREATE INDEX idx_pay_page_tokens_lot ON pay_page_tokens (lot_id);
CREATE UNIQUE INDEX uq_pay_page_active_token_per_lot ON pay_page_tokens (lot_id)
    WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------- settlements
CREATE TABLE settlements (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id                  uuid NOT NULL REFERENCES lots(id),
    invoice_id              uuid NOT NULL REFERENCES invoices(id),
    seller_account_id       uuid NOT NULL REFERENCES accounts(id),

    gross_amount_cents      bigint NOT NULL CHECK (gross_amount_cents > 0),
    platform_fee_cents      bigint NOT NULL CHECK (platform_fee_cents >= 0),
    seller_payout_cents     bigint NOT NULL
        GENERATED ALWAYS AS (gross_amount_cents - platform_fee_cents) STORED,

    -- Off-Stripe payout method (check, bank transfer, etc.)
    payout_method           text NOT NULL,
    payout_reference        text,             -- check number, transfer ID, etc.

    -- Timeline
    payment_cleared_at      timestamptz NOT NULL,   -- when Stripe payment cleared
    delivery_confirmed_at   timestamptz,            -- set by admin after delivery
    buffer_release_at       timestamptz,            -- delivery_confirmed_at + buffer
    released_at             timestamptz,            -- actual payout date

    -- Phase 2: stripe_transfer_id will go here
    stripe_transfer_id      text,                   -- NULL in Phase 1

    notes                   text,
    recorded_by_id          uuid NOT NULL REFERENCES accounts(id),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_settlement_lot UNIQUE (lot_id),

    CONSTRAINT chk_release_timeline CHECK (
        released_at IS NULL
        OR (delivery_confirmed_at IS NOT NULL AND released_at >= buffer_release_at)
    )
);

CREATE INDEX idx_settlements_lot ON settlements (lot_id);
CREATE INDEX idx_settlements_seller ON settlements (seller_account_id);

-- ---------------------------------------------------------------- second chance offers
-- NOTE (repair 2): the saved spec truncated this DDL after offered_price_cents.
-- Remaining columns completed following the invoices pattern; offer expiry is
-- 72h per the migration plan's second-chance step (§4: unpaid at 72h ->
-- second-chance offer; the offer itself needs its own deadline).
CREATE TABLE second_chance_offers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id              uuid NOT NULL REFERENCES lots(id),
    original_invoice_id uuid REFERENCES invoices(id),
    -- The invoice that was voided/unpaid that triggered this.
    -- NULL for RESERVE_NOT_MET offers (gap-check C4): a reserve-miss
    -- close has no invoice, but the high bidder still gets an offer.

    offer_kind          text NOT NULL DEFAULT 'UNPAID_WINNER'
        CHECK (offer_kind IN ('UNPAID_WINNER','RESERVE_NOT_MET')),

    offeree_account_id  uuid NOT NULL REFERENCES accounts(id),
    offered_price_cents bigint NOT NULL CHECK (offered_price_cents > 0),
    -- = offeree's max_bid at time of offer (their bid's max_bid_cents)

    status              offer_status NOT NULL DEFAULT 'PENDING',
    offered_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL
        GENERATED ALWAYS AS (offered_at + INTERVAL '72 hours') STORED,
    responded_at        timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT chk_offer_response CHECK (
        (status IN ('ACCEPTED', 'DECLINED')) = (responded_at IS NOT NULL)
    )
);

CREATE INDEX idx_sco_lot ON second_chance_offers (lot_id);
CREATE INDEX idx_sco_status ON second_chance_offers (status);
CREATE INDEX idx_sco_offeree ON second_chance_offers (offeree_account_id);

-- ---------------------------------------------------------------- moderation actions [§6-derived]
-- The moderation_action_type enum exists in spec §1.3; the §6 body (queue
-- design) was not saved. This table is the queue's audit trail: every
-- moderation-relevant lifecycle move writes one row, atomically with it.
CREATE TABLE moderation_actions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lot_id              uuid NOT NULL REFERENCES lots(id),
    actor_account_id    uuid REFERENCES accounts(id),  -- NULL = system (render check)
    action              moderation_action_type NOT NULL,
    note                text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_mod_actions_lot ON moderation_actions (lot_id, created_at);
CREATE INDEX idx_mod_actions_action ON moderation_actions (action);

-- ---------------------------------------------------------------- notifications [§7-derived]
-- Event outbox: rows are written by the engine; delivery (email/push) is a
-- later slice. notification_event enum is spec §1.3.
CREATE TABLE notifications (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES accounts(id),
    lot_id              uuid REFERENCES lots(id),
    event               notification_event NOT NULL,
    payload             jsonb NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    read_at             timestamptz
);

CREATE INDEX idx_notifications_account ON notifications (account_id, created_at);
CREATE INDEX idx_notifications_lot ON notifications (lot_id);
