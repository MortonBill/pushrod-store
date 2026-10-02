"""Auction engine tests — Slice 1 (data layer + lifecycle + moderation).

Covers, without network or real keys:
  1. schema — all §1 tables exist on the SQLite mirror, increment-rule
     seeds, CHECK constraints backstop the winner invariant (a lot can
     never be INVOICED without a winner, nor hold a winner pre-win);
  2. accounts — seller/bidder/admin creation, duplicate + auth rules;
  3. lifecycle — the spec §1.3 transition table: happy path DRAFT ->
     IN_MODERATION -> SCHEDULED -> RENDER_CHECK -> LIVE, illegal moves
     raise, INVOICED requires winner fields atomically;
  4. moderation queue — claim/approve/reject/send-back/reschedule/cancel,
     audit rows written for every moderation action;
  5. the go-live gate — a SCHEDULED lot is NOT live; only attempt_go_live
     with a passing public-render check reaches LIVE; a failing check
     parks the lot at RENDER_FAILED; the reserve amount never appears in
     the public representation;
  6. the Flask surface — register/login/create/submit/queue/approve/
     go-live end to end, admin-token gating, DRAFT lots invisible to the
     public.

Run: ./.venv/bin/python backend/test_auctions.py   (from pushrod-store/)
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="pushrod-auctions-test-")
DB_A = os.path.join(TMP, "auctions-a.db")
DB_B = os.path.join(TMP, "auctions-b.db")

os.environ["AUCTIONS_DB"] = DB_A
os.environ.pop("AUCTIONS_ENABLED", None)
os.environ.pop("AUCTIONS_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("AUCTIONS_RENDER_CHECK_URL_TEMPLATE", None)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def expect_raises(name, fn, exc_types=(Exception,)):
    try:
        fn()
    except exc_types as exc:
        check(name, True)
        return exc
    except Exception as exc:  # wrong failure mode is still a failure
        check(name, False, f"raised {type(exc).__name__}: {exc}")
        return None
    check(name, False, "no exception raised")
    return None


def past(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# 0. Opt-in: the engine stays dark unless a brand enables it.
# ---------------------------------------------------------------------------
from flask import Flask  # noqa: E402
import auctions as amod  # noqa: E402

check("init returns False when auctions not enabled",
      amod.init(Flask("auctions-off"), brand_cfg={}, root_dir=TMP) is False)

enabled = amod.init(Flask("auctions-domain"),
                    brand_cfg={"auctions": {"enabled": True}}, root_dir=TMP)
check("init enables with brand yaml opt-in", enabled is True)

# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------
import sqlite3  # noqa: E402

raw = sqlite3.connect(DB_A)
tables = {r[0] for r in raw.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
expected_tables = {"accounts", "lots", "bids", "increment_rules", "invoices",
                   "pay_page_tokens", "settlements", "second_chance_offers",
                   "moderation_actions", "notifications"}
check("schema creates every spec §1 table", expected_tables <= tables,
      f"missing: {expected_tables - tables}")
seed_counts = dict(raw.execute(
    "SELECT brand, COUNT(*) FROM increment_rules GROUP BY brand"))
check("increment rules seeded per spec §1.8 (RE 8 / IH 7)",
      seed_counts.get("RE") == 8 and seed_counts.get("IH") == 7,
      str(seed_counts))
raw.close()

# ---------------------------------------------------------------------------
# 2. Accounts
# ---------------------------------------------------------------------------
seller = amod.create_account("RE", "seller@example.com", "Seller One",
                             "password123", is_seller=True)
bidder = amod.create_account("RE", "bidder@example.com", "Bidder One",
                             "password123")
admin = amod.create_account("RE", "admin@example.com", "Admin One",
                            "password123", is_admin=True)
ih_seller = amod.create_account("IH", "ih-seller@example.com", "IH Seller",
                                "password123", is_seller=True)
check("accounts created with uuid ids", len(seller["id"]) == 36)
expect_raises("duplicate email per brand rejected",
              lambda: amod.create_account("RE", "seller@example.com", "Dupe",
                                          "password123"),
              (amod.AuctionError,))
check("same email allowed on the other brand",
      amod.create_account("IH", "seller@example.com", "Other Brand",
                          "password123")["brand"] == "IH")
check("authenticate accepts correct password",
      amod.authenticate("RE", "seller@example.com", "password123") is not None)
check("authenticate rejects wrong password",
      amod.authenticate("RE", "seller@example.com", "nope-nope") is None)
check("new accounts start with unverified email",
      seller["email_verified_at"] is None)
amod.mark_email_verified(bidder["id"])
check("mark_email_verified sets the timestamp",
      amod.get_account(bidder["id"])["email_verified_at"] is not None)

# ---------------------------------------------------------------------------
# 3. Lot creation rules
# ---------------------------------------------------------------------------
expect_raises("non-seller cannot create lots",
              lambda: amod.create_lot(bidder["id"], "RE", "A Valid Title",
                                      "desc", "cars", 1000),
              (amod.PermissionDenied,))
expect_raises("reserve below starting price rejected",
              lambda: amod.create_lot(seller["id"], "RE", "A Valid Title",
                                      "desc", "cars", 1000,
                                      reserve_price_cents=500),
              (amod.AuctionError,))
expect_raises("lot brand must match seller brand",
              lambda: amod.create_lot(seller["id"], "IH", "A Valid Title",
                                      "desc", "bikes", 1000),
              (amod.AuctionError,))

lot = amod.create_lot(seller["id"], "RE", "1969 Chevelle SS 396",
                      "Numbers-matching big block.", "muscle-cars", 500000,
                      reserve_price_cents=900000)
check("lot created as DRAFT", lot["status"] == "DRAFT")
pub = amod.public_lot_dict(lot)
check("public dict hides the reserve amount",
      "reserve_price_cents" not in pub and "900000" not in json.dumps(pub))
check("public dict shows reserve presence only",
      pub["reserve_present"] is True and pub["reserve_met"] is None)
check("DRAFT lot is not live", pub["is_live"] is False)

raw = sqlite3.connect(DB_A)
raw.execute("UPDATE accounts SET is_suspended = 1 WHERE id = ?",
            (ih_seller["id"],))
raw.commit()
raw.close()
expect_raises("suspended seller cannot create lots",
              lambda: amod.create_lot(ih_seller["id"], "IH", "A Valid Title",
                                      "desc", "bikes", 1000),
              (amod.PermissionDenied,))
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE accounts SET is_suspended = 0 WHERE id = ?",
            (ih_seller["id"],))
raw.commit()
raw.close()

# ---------------------------------------------------------------------------
# 4. Lifecycle: illegal moves raise; the winner invariant holds twice over
# ---------------------------------------------------------------------------
expect_raises("DRAFT -> LIVE is illegal (must pass moderation + render check)",
              lambda: amod.transition(lot["id"], "LIVE"), (amod.AuctionError,))
expect_raises("DRAFT -> SCHEDULED is illegal",
              lambda: amod.transition(lot["id"], "SCHEDULED"),
              (amod.AuctionError,))
expect_raises("unknown status rejected",
              lambda: amod.transition(lot["id"], "SOLD"), (amod.AuctionError,))

raw = sqlite3.connect(DB_A)
try:
    raw.execute("UPDATE lots SET status = 'INVOICED' WHERE id = ?",
                (lot["id"],))
    raw.commit()
    check("DB CHECK blocks INVOICED without a winner", False)
except sqlite3.IntegrityError:
    raw.rollback()
    check("DB CHECK blocks INVOICED without a winner", True)
try:
    raw.execute("UPDATE lots SET winner_account_id = ? WHERE id = ?",
                (bidder["id"], lot["id"]))
    raw.commit()
    check("DB CHECK blocks a winner on a pre-win lot", False)
except sqlite3.IntegrityError:
    raw.rollback()
    check("DB CHECK blocks a winner on a pre-win lot", True)
try:
    raw.execute("UPDATE lots SET leading_bidder_id = seller_account_id"
                " WHERE id = ?", (lot["id"],))
    raw.commit()
    check("DB CHECK blocks seller as leading bidder", False)
except sqlite3.IntegrityError:
    raw.rollback()
    check("DB CHECK blocks seller as leading bidder", True)
try:
    raw.execute("UPDATE lots SET extension_minutes_used = 121 WHERE id = ?",
                (lot["id"],))
    raw.commit()
    check("DB CHECK enforces the 120-minute soft-close cap", False)
except sqlite3.IntegrityError:
    raw.rollback()
    check("DB CHECK enforces the 120-minute soft-close cap", True)
raw.close()

# Drive a second lot to CLOSED by hand (close logic is Slice 2) to prove the
# CLOSED -> INVOICED fork can only fire with an atomic winner write.
lot2 = amod.create_lot(seller["id"], "RE", "1970 Plymouth Satellite",
                       "Project car.", "muscle-cars", 100000)
amod.submit_lot(lot2["id"], seller["id"])
amod.approve_lot(lot2["id"], admin["id"], past(2), future(1))
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE lots SET status = 'CLOSED' WHERE id = ?", (lot2["id"],))
raw.commit()
raw.close()
expect_raises("CLOSED -> INVOICED without winner fields raises",
              lambda: amod.transition(lot2["id"], "INVOICED"),
              (amod.AuctionError,))
won = amod.transition(lot2["id"], "INVOICED", winner_account_id=bidder["id"],
                      winning_price_cents=150000)
check("CLOSED -> INVOICED with atomic winner write succeeds",
      won["status"] == "INVOICED" and won["winner_account_id"] == bidder["id"])
check("public dict derives reserve_met after close",
      amod.public_lot_dict(won)["reserve_met"] is True)
amod.transition(lot2["id"], "PAID")
settled = amod.transition(lot2["id"], "SETTLED")
check("INVOICED -> PAID -> SETTLED chain works", settled["status"] == "SETTLED")
expect_raises("SETTLED is terminal",
              lambda: amod.transition(lot2["id"], "DRAFT"),
              (amod.AuctionError,))

# ---------------------------------------------------------------------------
# 5. Moderation queue
# ---------------------------------------------------------------------------
expect_raises("only the seller can submit their lot",
              lambda: amod.submit_lot(lot["id"], bidder["id"]),
              (amod.PermissionDenied,))
amod.submit_lot(lot["id"], seller["id"])
check("submitted lot is IN_MODERATION",
      amod.get_lot(lot["id"])["status"] == "IN_MODERATION")
ih_lot = amod.create_lot(ih_seller["id"], "IH", "1974 Ducati 750 GT",
                         "Bevel drive.", "motorcycles", 250000)
amod.submit_lot(ih_lot["id"], ih_seller["id"])
queue_all = amod.moderation_queue()
queue_re = amod.moderation_queue("RE")
check("moderation queue lists submitted lots",
      any(l["id"] == lot["id"] for l in queue_all))
check("moderation queue filters by brand",
      any(l["id"] == lot["id"] for l in queue_re)
      and all(l["brand"] == "RE" for l in queue_re))

amod.claim_lot(lot["id"], admin["id"])
check("claim records the moderating admin",
      amod.get_lot(lot["id"])["moderated_by_id"] == admin["id"])
expect_raises("approve requires a schedule",
              lambda: amod.approve_lot(lot["id"], admin["id"], None, None),
              (amod.AuctionError,))
expect_raises("approve requires close after start",
              lambda: amod.approve_lot(lot["id"], admin["id"],
                                       future(2), future(1)),
              (amod.AuctionError,))

# Reject -> resubmit path, then send-back path, then approve.
amod.reject_lot(ih_lot["id"], admin["id"], note="needs better photos")
check("reject lands at REJECTED",
      amod.get_lot(ih_lot["id"])["status"] == "REJECTED")
amod.transition(ih_lot["id"], "DRAFT")
amod.submit_lot(ih_lot["id"], ih_seller["id"])
amod.send_back_lot(ih_lot["id"], admin["id"], note="add VIN photo")
check("send-back returns the lot to DRAFT",
      amod.get_lot(ih_lot["id"])["status"] == "DRAFT")

approved = amod.approve_lot(lot["id"], admin["id"], past(1), future(2),
                            note="good to go")
check("approval lands at SCHEDULED", approved["status"] == "SCHEDULED")
check("a SCHEDULED lot still does not read as live",
      amod.public_lot_dict(approved)["is_live"] is False)

history_actions = [row["action"] for row in amod.moderation_history(lot["id"])]
check("audit trail records submit/claim/approve in order",
      history_actions == ["SUBMITTED", "CLAIMED", "APPROVED"],
      str(history_actions))

# ---------------------------------------------------------------------------
# 6. The go-live gate
# ---------------------------------------------------------------------------
early = amod.create_lot(seller["id"], "RE", "1968 Mustang GT Fastback",
                        "S-code.", "muscle-cars", 750000)
amod.submit_lot(early["id"], seller["id"])
amod.approve_lot(early["id"], admin["id"], future(1), future(3))
expect_raises("go-live before the scheduled start raises (scheduled != live)",
              lambda: amod.attempt_go_live(early["id"]), (amod.AuctionError,))
amod.cancel_lot(early["id"], admin["id"], note="test cleanup")
check("cancel from SCHEDULED works",
      amod.get_lot(early["id"])["status"] == "CANCELLED")
expect_raises("CANCELLED is terminal",
              lambda: amod.transition(early["id"], "DRAFT"),
              (amod.AuctionError,))

failed = amod.attempt_go_live(lot["id"], render_check=lambda l, p: (False, "boom"))
# attempt_go_live returns (lot, ok, detail); unpack properly below.
lot_after, ok, detail = failed
check("failing render check parks the lot at RENDER_FAILED",
      lot_after["status"] == "RENDER_FAILED" and ok is False)
check("render check result recorded on the lot",
      lot_after["last_render_check_ok"] in (0, False)
      and lot_after["last_render_check_at"] is not None)
check("RENDER_FAILED lot is not live",
      amod.public_lot_dict(lot_after)["is_live"] is False)
expect_raises("RENDER_FAILED cannot jump straight to LIVE",
              lambda: amod.transition(lot["id"], "LIVE"), (amod.AuctionError,))

amod.reschedule_lot(lot["id"], admin["id"], past(1), future(2),
                    note="fixed the page")
check("reschedule returns the lot to SCHEDULED",
      amod.get_lot(lot["id"])["status"] == "SCHEDULED")
live_lot, ok, detail = amod.attempt_go_live(
    lot["id"], render_check=lambda l, p: (True, "ok"))
check("passing render check takes the lot LIVE",
      ok is True and live_lot["status"] == "LIVE")
check("LIVE lot reads as live", amod.public_lot_dict(live_lot)["is_live"] is True)
history_actions = [row["action"] for row in amod.moderation_history(lot["id"])]
check("audit trail records render fail, reschedule, then pass",
      history_actions[-3:] == ["RENDER_FAIL", "EDITED", "RENDER_PASS"],
      str(history_actions))

# Default (structural) render check: no URL template configured.
lot3 = amod.create_lot(seller["id"], "RE", "1972 Chevrolet C10",
                       "Short bed.", "classic-trucks", 300000)
amod.submit_lot(lot3["id"], seller["id"])
amod.approve_lot(lot3["id"], admin["id"], past(1), future(2))
lot3_live, ok3, detail3 = amod.attempt_go_live(lot3["id"])
check("default structural render check passes a complete lot",
      ok3 is True and lot3_live["status"] == "LIVE", detail3)

# ---------------------------------------------------------------------------
# 7. Flask surface (full app import, auctions enabled by env)
# ---------------------------------------------------------------------------
os.environ["AUCTIONS_DB"] = DB_B
os.environ["AUCTIONS_ENABLED"] = "1"
os.environ["AUCTIONS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["BRAND"] = "gateway"
os.environ["PRINTFUL_DRY_RUN"] = "1"

import app as store_app  # noqa: E402

client = store_app.app.test_client()
ADMIN = {"X-Auctions-Admin-Token": "test-admin-token"}

r = client.post("/api/auctions/accounts", json={
    "brand": "RE", "email": "api-seller@example.com", "display_name": "API",
    "password": "password123", "is_seller": True})
check("API registers a seller", r.status_code == 201, str(r.get_json()))
r = client.post("/api/auctions/login", json={
    "brand": "RE", "email": "api-seller@example.com",
    "password": "password123"})
check("API login works", r.status_code == 200, str(r.get_json()))
r = client.post("/api/auctions/lots", json={
    "title": "1966 Ford Bronco", "description": "Early Bronco.",
    "category": "classic-trucks", "starting_price_cents": 400000})
check("API creates a lot", r.status_code == 201, str(r.get_json()))
api_lot = r.get_json()
check("API lot response hides the reserve amount",
      "reserve_price_cents" not in api_lot)

anon = store_app.app.test_client()
r = anon.get(f"/api/auctions/lots/{api_lot['id']}")
check("DRAFT lot is invisible to the public (404)", r.status_code == 404)
r = client.post(f"/api/auctions/lots/{api_lot['id']}/submit")
check("API submit works", r.status_code == 200
      and r.get_json()["status"] == "IN_MODERATION", str(r.get_json()))
r = client.get("/api/auctions/moderation/queue")
check("moderation queue refuses non-admins", r.status_code == 403)
r = client.get("/api/auctions/moderation/queue", headers=ADMIN)
check("admin token opens the moderation queue",
      r.status_code == 200 and any(
          l["id"] == api_lot["id"] for l in r.get_json()), str(r.get_json()))
r = client.post(f"/api/auctions/lots/{api_lot['id']}/moderate", headers=ADMIN,
                json={"action": "approve", "scheduled_start_at": past(1),
                      "scheduled_close_at": future(2)})
check("API approve schedules the lot",
      r.status_code == 200 and r.get_json()["status"] == "SCHEDULED",
      str(r.get_json()))
r = client.post(f"/api/auctions/lots/{api_lot['id']}/go-live")
check("go-live refuses non-admins", r.status_code == 403)
r = client.post(f"/api/auctions/lots/{api_lot['id']}/go-live", headers=ADMIN)
check("API go-live takes the lot LIVE after the render check",
      r.status_code == 200 and r.get_json()["status"] == "LIVE"
      and r.get_json()["render_check"]["ok"] is True, str(r.get_json()))
r = client.get(f"/api/auctions/lots/{api_lot['id']}")
check("LIVE lot is publicly visible", r.status_code == 200
      and r.get_json()["is_live"] is True)
r = client.get("/api/auctions/lots?brand=RE")
check("public list carries the LIVE lot",
      any(l["id"] == api_lot["id"] for l in r.get_json()))

# ---------------------------------------------------------------------------
# 8. Slice 2 — proxy bidding, soft close, closer (spec §§2–3)
# Domain level on DB_A. Helper drives a lot to LIVE with a chosen close.
# ---------------------------------------------------------------------------
from datetime import timezone as _tz  # noqa: E402

# Section 7's app import re-pointed the module at DB_B; the domain tests
# below run against DB_A again.
os.environ["AUCTIONS_DB"] = DB_A
amod._DIALECT = "sqlite"
amod._DB_PATH = DB_A

bidder2 = amod.create_account("RE", "bidder2@example.com", "Bidder Two",
                              "password123")
bidder3 = amod.create_account("RE", "bidder3@example.com", "Bidder Three",
                              "password123")
unverified = amod.create_account("RE", "unverified@example.com", "Un Verified",
                                 "password123")
for _acct in (bidder2, bidder3, seller):
    amod.mark_email_verified(_acct["id"])


def iso(dt):
    return dt.isoformat()


def make_live_lot(starting, reserve=None, close_in_minutes=60, brand="RE",
                  the_seller=None):
    the_seller = the_seller or seller
    new_lot = amod.create_lot(
        the_seller["id"], brand, "Slice2 Lot " + os.urandom(3).hex(),
        "desc", "cars" if brand == "RE" else "bikes", starting,
        reserve_price_cents=reserve)
    amod.submit_lot(new_lot["id"], the_seller["id"])
    now = datetime.now(timezone.utc)
    amod.approve_lot(new_lot["id"], admin["id"], iso(now - timedelta(hours=1)),
                     iso(now + timedelta(minutes=close_in_minutes)))
    live, ok_live, _d = amod.attempt_go_live(
        new_lot["id"], render_check=lambda l, p: (True, "ok"))
    assert ok_live, "test lot failed to go live"
    return amod.get_lot(new_lot["id"])


def lot_bids(lot_id):
    raw = sqlite3.connect(DB_A)
    raw.row_factory = sqlite3.Row
    rows = [dict(r) for r in raw.execute(
        "SELECT * FROM bids WHERE lot_id = ? ORDER BY placed_at ASC",
        (lot_id,))]
    raw.close()
    return rows


def lot_notifications(lot_id):
    raw = sqlite3.connect(DB_A)
    raw.row_factory = sqlite3.Row
    rows = [dict(r) for r in raw.execute(
        "SELECT * FROM notifications WHERE lot_id = ?", (lot_id,))]
    raw.close()
    return rows


# --- §2.2 integrity gates --------------------------------------------------
gate_lot = make_live_lot(10000)
expect_raises("seller cannot bid on their own lot",
              lambda: amod.place_bid(seller["id"], gate_lot["id"], 20000),
              (amod.PermissionDenied,))
expect_raises("unverified email cannot bid",
              lambda: amod.place_bid(unverified["id"], gate_lot["id"], 20000),
              (amod.PermissionDenied,))
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE accounts SET is_suspended = 1 WHERE id = ?",
            (bidder3["id"],))
raw.commit()
raw.close()
expect_raises("suspended account cannot bid",
              lambda: amod.place_bid(bidder3["id"], gate_lot["id"], 20000),
              (amod.PermissionDenied,))
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE accounts SET is_suspended = 0 WHERE id = ?",
            (bidder3["id"],))
raw.commit()
raw.close()
expect_raises("bid below the starting price rejected",
              lambda: amod.place_bid(bidder["id"], gate_lot["id"], 9999),
              (amod.AuctionError,))
expect_raises("bid on a non-live lot rejected",
              lambda: amod.place_bid(bidder["id"], early["id"], 800000),
              (amod.AuctionError,))
expect_raises("cross-brand bid rejected",
              lambda: amod.place_bid(bidder["id"], ih_lot["id"], 300000),
              (amod.AuctionError,))

# --- §2.3 proxy math (RE increments: $5.00 in the $50–$249.99 band) --------
r1 = amod.place_bid(bidder["id"], gate_lot["id"], 10000)
check("first bid leads at the starting price",
      r1["outcome"] == "leading" and r1["current_price_cents"] == 10000)
r2 = amod.place_bid(bidder2["id"], gate_lot["id"], 20000)
check("higher max wins at second max + increment",
      r2["outcome"] == "leading" and r2["current_price_cents"] == 10500)
rows = lot_bids(gate_lot["id"])
check("losing incumbent flips to OUTBID",
      rows[0]["status"] == "OUTBID" and rows[1]["status"] == "ACTIVE")
check("winner price never exceeds the winner max",
      lot_bids(gate_lot["id"])[1]["effective_price_cents"] == 10500)
expect_raises("bid below current price + increment rejected",
              lambda: amod.place_bid(bidder3["id"], gate_lot["id"], 10750),
              (amod.AuctionError,))
r3 = amod.place_bid(bidder3["id"], gate_lot["id"], 15000)
check("lower competing max loses and pushes the price to its max + increment",
      r3["outcome"] == "outbid" and r3["current_price_cents"] == 15500)
expect_raises("leader max must strictly increase on a self-raise",
              lambda: amod.place_bid(bidder2["id"], gate_lot["id"], 15000),
              (amod.AuctionError,))
r4 = amod.place_bid(bidder2["id"], gate_lot["id"], 30000)
check("leader self-raise keeps the price and the lead",
      r4["outcome"] == "raised" and r4["current_price_cents"] == 15500
      and r4["is_leading"] is True)
notes = {(n["account_id"], n["event"]) for n in lot_notifications(gate_lot["id"])}
check("OUTBID + WINNING notifications written on displacement",
      (bidder["id"], "OUTBID") in notes
      and (bidder2["id"], "WINNING") in notes, str(notes))
pub = amod.public_lot_dict(amod.get_lot(gate_lot["id"]),
                           account_id=bidder["id"])
check("public view shows bid count and minimum next bid, never others' max",
      pub["bid_count"] == 4 and pub["minimum_next_bid_cents"] == 16000
      and "30000" not in json.dumps(pub)
      and "20000" not in json.dumps(pub), str(pub))
check("viewer sees their own max and standing only",
      pub["your_max_bid_cents"] == 10000 and pub["you_are_leading"] is False)

# --- §2.5 tie: earliest max wins, price rises to the tied max --------------
tie_lot = make_live_lot(5000)
amod.place_bid(bidder["id"], tie_lot["id"], 12000)
rt = amod.place_bid(bidder2["id"], tie_lot["id"], 12000)
check("tied max loses to the earlier bid at the tied price",
      rt["outcome"] == "outbid" and rt["current_price_cents"] == 12000)
check("tie keeps the earliest bidder in the lead",
      amod.get_lot(tie_lot["id"])["leading_bidder_id"] == bidder["id"])

# --- §3.1 soft close (+120-minute cap) -------------------------------------
soft_lot = make_live_lot(1000, close_in_minutes=4)
before = amod.get_lot(soft_lot["id"])
rs = amod.place_bid(bidder["id"], soft_lot["id"], 5000)
after = amod.get_lot(soft_lot["id"])
check("bid inside the final 5 minutes extends the close by 5 minutes",
      rs["extension_triggered"] is True
      and after["extension_minutes_used"] == 5
      and after["current_close_at"] > before["current_close_at"])
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE lots SET extension_minutes_used = 118,"
            " current_close_at = ? WHERE id = ?",
            (iso(datetime.now(timezone.utc) + timedelta(minutes=3)),
             soft_lot["id"]))
raw.commit()
raw.close()
rs2 = amod.place_bid(bidder2["id"], soft_lot["id"], 6000)
after2 = amod.get_lot(soft_lot["id"])
check("extension stops at the 120-minute cap",
      rs2["extension_triggered"] is True
      and after2["extension_minutes_used"] == 120)
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE lots SET current_close_at = ? WHERE id = ?",
            (iso(datetime.now(timezone.utc) + timedelta(minutes=2)),
             soft_lot["id"]))
raw.commit()
raw.close()
rs3 = amod.place_bid(bidder["id"], soft_lot["id"], 7000)
check("no extension once the cap is exhausted",
      rs3["extension_triggered"] is False
      and amod.get_lot(soft_lot["id"])["extension_minutes_used"] == 120)

# --- §3.3/§3.4 closer: fork, invoice, idempotency --------------------------
now = datetime.now(timezone.utc)
done = amod.close_lot(gate_lot["id"], now=now)  # close is +60m: not due
check("closer leaves a lot whose close has not arrived alone",
      done["outcome"] == "not_due"
      and amod.get_lot(gate_lot["id"])["status"] == "LIVE")
# Make it due by moving the close into the past, then close it.
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE lots SET current_close_at = ? WHERE id = ?",
            (iso(now - timedelta(minutes=1)), gate_lot["id"]))
raw.commit()
raw.close()
res = amod.close_lot(gate_lot["id"], now=now)
closed_lot = amod.get_lot(gate_lot["id"])
check("due lot closes INVOICED to the proxy winner at the computed price",
      res["outcome"] == "invoiced"
      and closed_lot["status"] == "INVOICED"
      and closed_lot["winner_account_id"] == bidder2["id"]
      and closed_lot["winning_price_cents"] == 15500, str(res))
raw = sqlite3.connect(DB_A)
raw.row_factory = sqlite3.Row
inv = raw.execute("SELECT * FROM invoices WHERE lot_id = ?",
                  (gate_lot["id"],)).fetchone()
raw.close()
check("winner invoice created OPEN at the winning price with a 72h deadline",
      inv is not None and inv["status"] == "OPEN"
      and inv["amount_cents"] == 15500
      and inv["payment_deadline_at"] is not None)
again = amod.close_lot(gate_lot["id"], now=now)
check("closer re-run is idempotent (already_invoiced, no duplicate invoice)",
      again["outcome"] == "already_invoiced"
      and again["invoice_id"] == inv["id"])
check("close notified the winner (AUCTION_WON + INVOICE_ISSUED)",
      {(n["account_id"], n["event"]) for n in lot_notifications(gate_lot["id"])}
      >= {(bidder2["id"], "AUCTION_WON"), (bidder2["id"], "INVOICE_ISSUED")})

# Reserve not met -> NO_SALE, winner fields stay NULL.
res_lot = make_live_lot(1000, reserve=50000)
amod.place_bid(bidder["id"], res_lot["id"], 10000)
amod.place_bid(bidder2["id"], res_lot["id"], 20000)
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE lots SET current_close_at = ? WHERE id = ?",
            (iso(now - timedelta(minutes=1)), res_lot["id"]))
raw.commit()
raw.close()
res2 = amod.close_lot(res_lot["id"], now=now)
no_sale = amod.get_lot(res_lot["id"])
check("reserve not met closes NO_SALE with no winner written",
      res2["outcome"] == "no_sale" and no_sale["status"] == "NO_SALE"
      and no_sale["winner_account_id"] is None
      and no_sale["winning_price_cents"] is None, str(res2))
check("seller gets RESERVE_NOT_MET; reserve amount leaks nowhere public",
      any(n["event"] == "RESERVE_NOT_MET"
          and n["account_id"] == seller["id"]
          for n in lot_notifications(res_lot["id"]))
      and "50000" not in json.dumps(amod.public_lot_dict(no_sale)))

# Zero bids -> NO_SALE; crash CLOSED lot self-heals through run_closer.
empty_lot = make_live_lot(1000)
stuck_lot = make_live_lot(1000)
amod.place_bid(bidder["id"], stuck_lot["id"], 5000)
raw = sqlite3.connect(DB_A)
raw.execute("UPDATE lots SET current_close_at = ? WHERE id IN (?, ?)",
            (iso(now - timedelta(minutes=1)), empty_lot["id"],
             stuck_lot["id"]))
raw.execute("UPDATE lots SET status = 'CLOSED' WHERE id = ?",
            (stuck_lot["id"],))
raw.commit()
raw.close()
summary = amod.run_closer(now=now + timedelta(minutes=1))
by_lot = {r["lot_id"]: r["outcome"] for r in summary["results"]}
check("run_closer closes a zero-bid lot as no_sale",
      by_lot.get(empty_lot["id"]) == "no_sale", str(by_lot))
check("run_closer self-heals a crash-stuck CLOSED lot",
      by_lot.get(stuck_lot["id"]) == "invoiced", str(by_lot))
summary2 = amod.run_closer(now=now + timedelta(minutes=2))
tracked = {r["lot_id"] for r in summary2["results"]}
check("run_closer second pass never reprocesses a closed lot (idempotent)",
      not ({gate_lot["id"], res_lot["id"], empty_lot["id"], stuck_lot["id"]}
           & tracked), str(summary2))

# --- HTTP: bid endpoint + token-gated closer -------------------------------
# The store app (and the blueprint's requests) run against DB_B.
os.environ["AUCTIONS_DB"] = DB_B
amod._DB_PATH = DB_B
os.environ["AUCTIONS_CLOSER_TOKEN"] = "test-closer-token"
bidder_client = store_app.app.test_client()
r = bidder_client.post("/api/auctions/accounts", json={
    "brand": "RE", "email": "api-bidder@example.com",
    "display_name": "API Bidder", "password": "password123"})
check("API registers a bidder", r.status_code == 201, str(r.get_json()))
api_bidder_id = r.get_json()["id"]
r = bidder_client.post("/api/auctions/lots/" + api_lot["id"] + "/bid",
                       json={"max_bid_cents": 500000})
check("API bid refuses an unverified email (403)",
      r.status_code == 403, str(r.get_json()))
amod.mark_email_verified(api_bidder_id)
r = bidder_client.post("/api/auctions/login", json={
    "brand": "RE", "email": "api-bidder@example.com",
    "password": "password123"})
check("API bidder login works", r.status_code == 200, str(r.get_json()))
r = bidder_client.post("/api/auctions/lots/" + api_lot["id"] + "/bid",
                       json={"max_bid_cents": 500000})
check("API bid places a proxy max on the LIVE lot",
      r.status_code == 201 and r.get_json()["outcome"] == "leading"
      and r.get_json()["current_price_cents"] == 400000, str(r.get_json()))
r = bidder_client.post("/api/auctions/lots/" + api_lot["id"] + "/bid",
                       json={"max_bid_cents": 500000})
check("API self-raise at the same max is refused",
      r.status_code == 400, str(r.get_json()))
r = bidder_client.get("/api/auctions/lots/" + api_lot["id"])
check("API lot view shows the bid state without others' max",
      r.status_code == 200 and r.get_json()["bid_count"] == 1
      and r.get_json()["your_max_bid_cents"] == 500000)
r = anon.post("/api/auctions/closer/run")
check("closer endpoint refuses requests without the cron token",
      r.status_code == 403, str(r.get_json()))
r = anon.post("/api/auctions/closer/run?token=wrong-token")
check("closer endpoint refuses a wrong token", r.status_code == 403)
r = anon.post("/api/auctions/closer/run",
              headers={"X-Auctions-Closer-Token": "test-closer-token"})
check("closer endpoint runs with the cron token",
      r.status_code == 200 and "results" in r.get_json(), str(r.get_json()))
del os.environ["AUCTIONS_CLOSER_TOKEN"]
r = anon.post("/api/auctions/closer/run",
              headers={"X-Auctions-Closer-Token": "test-closer-token"})
check("closer endpoint stays dark when no token is configured",
      r.status_code == 404, str(r.get_json()))

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL AUCTION TESTS PASSED")
