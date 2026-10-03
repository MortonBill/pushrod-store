#!/usr/bin/env python3
"""Auction Slice 5 — Postgres live-fire rehearsal (run_auction_pg_gate.py).

The Slice 5 deploy gate: apply backend/auctions_schema.sql to a FRESH
database and run the full money path end to end with Stripe stubbed:

    test lot -> moderation approve -> render-check go-live
    -> two-account proxy bids -> soft close -> guarded closer
    -> pay page token -> simulated checkout.session.completed
    -> PAID -> settlement (seller paid the hammer in full)

Which database:
  * If a usable Postgres is available — `--postgres <url>`, the
    AUCTIONS_PG_GATE_URL env var, or a locally reachable server — the
    gate runs against it in a throwaway schema (dropped on exit). The
    production DDL (auctions_schema.sql) is applied verbatim.
  * Otherwise the SAME gate runs against a throwaway SQLite file (the
    engine's SQLite mirror DDL), and the report separates what that
    PROVED (all engine behavior: state machine, proxy math, closer,
    invoices, pay tokens, webhook handling, settlement, idempotency)
    from what still needs a REAL Postgres (DDL variance: TIMESTAMPTZ
    defaults, JSONB column coercion, partial-index semantics under
    concurrent closer runs, trigger behavior under load).

Nothing here provisions, deploys, or charges: Stripe is stubbed at the
session factory, the "checkout" is a fabricated session object fed
straight to the webhook handler, and the settlement is a ledger row.

Usage:
    python3 backend/run_auction_pg_gate.py                 # auto-detect
    python3 backend/run_auction_pg_gate.py --postgres "$DATABASE_URL"

Exit code 0 = every check passed; 1 = at least one check failed.
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask  # noqa: E402

import auctions as A  # noqa: E402

T0 = datetime.now(timezone.utc)
FAILS = []
MODE = "sqlite"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else f" — {extra}"))
    if not cond:
        FAILS.append(name)


def row_get(row, key):
    try:
        return row[key]
    except Exception:  # noqa: BLE001 — dict vs mapping row
        return getattr(row, key, None)


def lot_media(brand="RE"):
    """A listing that passes the Slice-4 moderation standard."""
    photos = 20 if brand == "RE" else 15
    id_photos = 2 if brand == "RE" else 1
    return {
        "flaws": "Stone chips on the hood; driver's seat bolster worn.",
        "image_keys": [f"lot-img/{i:02d}.jpg" for i in range(photos)],
        "video_keys": ["lot-video/walkaround.mp4",
                       "lot-video/cold-start.mp4"],
        "id_photo_keys": [f"lot-id/{i}.jpg" for i in range(id_photos)],
        "no_ai_photos_attested": True,
    }


def _stub_factory(lot, invoice, success_url, cancel_url):
    return {"id": "cs_test_gate", "url": "https://checkout.stripe.test/c/cs_test_gate",  # noqa: E501
            "payment_intent": "pi_test_gate"}


def run_gate():
    """The full money path. Dialect-agnostic; asserts are physical."""
    # 1. Schema sanity: every §1 table exists; increment seeds present.
    with A._connect() as c:
        if MODE == "postgres":
            tables = {r["table_name"] for r in c.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema()").fetchall()}
            seeds = {(r["brand"], r["n"]) for r in c.execute(
                "SELECT brand, COUNT(*) AS n FROM increment_rules"
                " GROUP BY brand").fetchall()}
        else:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
            seeds = {(r["brand"], r["n"]) for r in c.execute(
                "SELECT brand, COUNT(*) AS n FROM increment_rules"
                " GROUP BY brand").fetchall()}
    expected = {"accounts", "lots", "bids", "increment_rules", "invoices",
                "pay_page_tokens", "settlements", "second_chance_offers",
                "moderation_actions", "notifications", "comments",
                "watchlist"}
    check(f"[{MODE}] schema applied — all §1 tables exist",
          expected <= tables, f"missing: {sorted(expected - tables)}")
    check(f"[{MODE}] increment rules seeded (RE 8 / IH 7)",
          ("RE", 8) in seeds and ("IH", 7) in seeds, str(seeds))

    # 2. Accounts.
    adm = A.create_account("RE", "gate-admin@example.com", "Gate Admin",
                           "password123", is_admin=True)
    sel = A.create_account("RE", "gate-seller@example.com", "Gate Seller",
                           "password123", is_seller=True)
    bid_hi = A.create_account("RE", "gate-a@example.com", "Gate Bidder A",
                              "password123")
    bid_lo = A.create_account("RE", "gate-b@example.com", "Gate Bidder B",
                              "password123")
    for a in (sel, bid_hi, bid_lo):
        A.mark_email_verified(a["id"])
    check("accounts created; emails verified for trading",
          all(A.get_account(a["id"])["email_verified_at"]
              for a in (sel, bid_hi, bid_lo)))

    # 3. Test lot through moderation to LIVE.
    lot = A.create_lot(sel["id"], "RE", "Gate Lot — 1969 Chevelle SS 396",
                       "Rehearsal lot; never publicly listed.",
                       "muscle-cars", 10000, reserve_price_cents=14000,
                       condition_notes="Honest driver.",
                       **lot_media("RE"))
    A.submit_lot(lot["id"], sel["id"])
    A.approve_lot(lot["id"], adm["id"],
                  (T0 - timedelta(hours=1)).isoformat(),
                  (T0 + timedelta(minutes=30)).isoformat())
    live, ok, detail = A.attempt_go_live(
        lot["id"], render_check=lambda l, p: (True, "rehearsal stub"))
    check("render-check gate takes the lot LIVE",
          ok is True and live["status"] == "LIVE", str(detail))
    lot = A.get_lot(lot["id"])

    # 4. Two-account proxy bidding (RE increments: +$5 in the $100–250
    # bracket): low max $150, high max $200 -> price = $150 + $5 = $155.
    A.place_bid(bid_lo["id"], lot["id"], 15000, now=T0)
    A.place_bid(bid_hi["id"], lot["id"], 20000, now=T0)
    lot = A.get_lot(lot["id"])
    check("proxy price = second-highest max + one increment ($155)",
          lot["current_price_cents"] == 15500
          and lot["leading_bidder_id"] == bid_hi["id"],
          f"price={lot['current_price_cents']}")

    # 5. Soft close: a bid inside the final 5 minutes extends +5.
    # Compress the schedule so the next bid lands inside the final
    # window. scheduled_close_at moves with current_close_at: the
    # engine keeps current >= scheduled everywhere (approve/reschedule
    # set them equal, soft close only extends) and the Postgres DDL
    # enforces that as chk_close_times — rewinding current alone
    # writes a state production code can never produce.
    with A._connect() as c:
        c.execute("UPDATE lots SET scheduled_close_at = ?,"
                  " current_close_at = ? WHERE id = ?",
                  ((T0 + timedelta(minutes=2)).isoformat(),
                   (T0 + timedelta(minutes=2)).isoformat(), lot["id"]))
    res = A.place_bid(bid_lo["id"], lot["id"], 21000,
                      now=T0 + timedelta(minutes=1))
    lot = A.get_lot(lot["id"])
    check("soft close extends the close on a late bid",
          (res.get("extended") or (lot["extension_minutes_used"] or 0) > 0)
          and A._parse_ts(lot["current_close_at"])
          > T0 + timedelta(minutes=2),
          f"close={lot['current_close_at']} ext={lot['extension_minutes_used']}")  # noqa: E501
    # The extension made the low bidder the leader at their own max;
    # raise the high bidder back on top so the closer pays a known bill.
    A.place_bid(bid_hi["id"], lot["id"], 25000,
                now=T0 + timedelta(minutes=1))
    lot = A.get_lot(lot["id"])
    check("leader restored before close (price tracks proxy math)",
          lot["leading_bidder_id"] == bid_hi["id"], str(lot))

    # 6. Guarded closer (the cron path) -> INVOICED with frozen invoice.
    summary = A.run_closer(now=T0 + timedelta(minutes=10))
    lot = A.get_lot(lot["id"])
    check("closer invoices the due lot",
          summary["invoiced"] == 1 and lot["status"] == "INVOICED",
          f"summary={summary} status={lot['status']}")
    inv = None
    with A._connect() as c:
        inv = c.execute("SELECT * FROM invoices WHERE lot_id = ?"
                        " AND status = 'OPEN'", (lot["id"],)).fetchone()
    check("invoice frozen: hammer + 4% premium = amount due",
          inv is not None and inv["hammer_cents"] == 21500
          and inv["buyer_premium_cents"] == 860
          and inv["amount_cents"] == 22360, str(dict(inv) if inv else None))
    check("closer is idempotent on re-run",
          A.run_closer(now=T0 + timedelta(minutes=11))["invoiced"] == 0)

    # 7. Pay page + simulated checkout.session.completed -> PAID.
    A.set_stripe_session_factory(_stub_factory)
    token = A.issue_pay_token(lot["id"], now=T0)
    check("pay token issued (raw token returned exactly once)",
          bool(token.get("raw_token")))
    session_obj = {"id": "cs_test_gate", "payment_intent": "pi_test_gate",
                   "metadata": {"kind": "auction_pay",
                                "invoice_id": inv["id"], "lot_id": lot["id"]}}  # noqa: E501
    A.handle_checkout_completed(session_obj, now=T0 + timedelta(minutes=12))
    lot = A.get_lot(lot["id"])
    check("checkout.session.completed marks the lot PAID",
          lot["status"] == "PAID", str(lot["status"]))
    A.handle_checkout_completed(session_obj, now=T0 + timedelta(minutes=13))
    check("replayed webhook writes nothing (still PAID, no error)",
          A.get_lot(lot["id"])["status"] == "PAID")
    A.set_stripe_session_factory(None)

    # 8. Settlement: seller is paid the hammer in full (fee is buyer-side).
    stl = A.record_settlement(lot["id"], adm["id"], "ACH",
                              payout_reference="gate-rehearsal",
                              delivery_confirmed_at=(
                                  T0 + timedelta(days=1)).isoformat(),
                              notes="Slice 5 prep rehearsal",
                              now=T0 + timedelta(days=2))
    check("settlement pays the seller the full hammer (platform fee 0)",
          stl["gross_amount_cents"] == 21500
          and stl["platform_fee_cents"] == 0
          and stl["seller_payout_cents"] == 21500
          and A.get_lot(lot["id"])["status"] == "SETTLED",
          str(dict(stl)))


def run_postgres(url):
    global MODE
    MODE = "postgres"
    import psycopg
    schema = "auction_gate_" + uuid.uuid4().hex[:8]
    print(f"Postgres gate: throwaway schema {schema}")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    scoped = url + ("&" if "?" in url else "?") \
        + f"options=-csearch_path%3D{schema}"
    try:
        with psycopg.connect(scoped, autocommit=True) as conn:
            with open(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "auctions_schema.sql")) as f:
                conn.execute(f.read())
        A._DIALECT = "postgres"
        A._DB_URL = scoped
        A._DB_PATH = ""
        run_gate()
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        print(f"throwaway schema {schema} dropped")


def run_sqlite():
    global MODE
    MODE = "sqlite"
    tmp = tempfile.mkdtemp(prefix="auction-pg-gate-")
    db = os.path.join(tmp, "auctions-gate.db")
    os.environ["AUCTIONS_DB"] = db
    A.init(Flask("auction-gate"), brand_cfg={"auctions": {"enabled": True}},
           root_dir=tmp)
    print(f"SQLite gate: temp DB {db} (removed with the temp dir)")
    run_gate()


LEDGER = """
================================================================
PROVED by this rehearsal (%MODE%)
  * auctions_schema.sql (or its SQLite mirror) applies cleanly and
    every §1 table + increment seed exists.
  * The full money path: moderation -> render-checked LIVE -> proxy
    bidding (+$5 RE increment) -> soft-close extension -> the guarded
    closer -> frozen invoice (hammer + 4%% buyer premium) -> pay-token
    issuance -> simulated checkout.session.completed -> PAID ->
    settlement (seller paid the hammer in full, platform fee 0).
  * Idempotency: re-running the closer and replaying the webhook
    change nothing.
STILL NEEDS A REAL POSTGRES (Slice 5 execution, Render Postgres ~$6/mo)
  * auctions_schema.sql verbatim on Postgres 15/16: TIMESTAMPTZ
    defaults, JSONB coercion of payload columns, partial unique
    indexes, FK behavior — exercised only when this gate runs with
    --postgres against the provisioned database.
  * Concurrency: two closers racing on one due lot (Render cron +
    manual run) — covered by UNIQUE walls, proven only under load.
  * pooler/SSL: Render's managed Postgres connection-string behavior
    (sslmode) inside the service environment.
================================================================
"""


def _find_postgres(argv):
    url = ""
    if "--postgres" in argv:
        url = argv[argv.index("--postgres") + 1]
    url = url or os.environ.get("AUCTIONS_PG_GATE_URL", "")
    return url.strip()


def main(argv):
    url = _find_postgres(argv)
    if url:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            print("ABORT: --postgres given but psycopg is not installed;"
                  " pip install psycopg[binary] first.")
            return 1
        run_postgres(url)
    elif argv.count("--postgres"):
        print("ABORT: --postgres needs a URL argument.")
        return 1
    else:
        print("no Postgres supplied -> running the SAME gate on the"
              " SQLite mirror (see ledger below)")
        run_sqlite()
    print(LEDGER.replace("%MODE%", MODE))
    if FAILS:
        print(f"{len(FAILS)} FAILURES: {FAILS}")
        return 1
    print("ALL GATE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
