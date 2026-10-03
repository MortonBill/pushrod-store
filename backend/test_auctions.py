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
                   "moderation_actions", "notifications", "comments",
                   "watchlist"}
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

def lot_media(brand="RE"):
    """A listing that satisfies the Slice-4 moderation standard for
    the brand (photo minimum, walk-around + cold-start video, flaws
    section, VIN/title or frame-number photos, no-AI attestation)."""
    photos = 20 if brand == "RE" else 15
    id_photos = 2 if brand == "RE" else 1
    return {
        "flaws": "Two stone chips on the hood; small tear in the "
                 "driver's seat bolster.",
        "image_keys": [f"lot-img/{i:02d}.jpg" for i in range(photos)],
        "video_keys": ["lot-video/walkaround.mp4",
                       "lot-video/cold-start.mp4"],
        "id_photo_keys": [f"lot-id/{i}.jpg" for i in range(id_photos)],
        "no_ai_photos_attested": True,
    }


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
                      reserve_price_cents=900000,
                      condition_notes="Older restoration, presents well.",
                      **lot_media("RE"))
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
                       "Project car.", "muscle-cars", 100000,
                       condition_notes="Rolling project.", **lot_media())
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
                        "S-code.", "muscle-cars", 750000,
                        condition_notes="Strong driver.", **lot_media())
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
                       "Short bed.", "classic-trucks", 300000,
                       condition_notes="Original paint.", **lot_media())
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
    "category": "classic-trucks", "starting_price_cents": 400000,
    "reserve_price_cents": 900000,
    "condition_notes": "Solid, honest truck.",
    **lot_media("RE")})
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
        reserve_price_cents=reserve,
        condition_notes="Test lot, honest driver.", **lot_media(brand))
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
check("winner invoice freezes hammer + 4% buyer premium = amount due",
      inv is not None and inv["status"] == "OPEN"
      and inv["hammer_cents"] == 15500
      and inv["buyer_premium_cents"] == 620
      and inv["amount_cents"] == 16120
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

# --- Slice 3: pay tokens, checkout, reminders, second-chance ladder --------
# Domain + HTTP on DB_B (amod._DB_PATH already points there).
s3_seller = amod.create_account("RE", "s3-seller@example.com", "S3 Seller",
                                "password123", is_seller=True)
s3_win = amod.create_account("RE", "s3-win@example.com", "S3 Winner",
                             "password123")
s3_run = amod.create_account("RE", "s3-run@example.com", "S3 Runner",
                             "password123")
s3_third = amod.create_account("RE", "s3-third@example.com", "S3 Third",
                               "password123")
for _acct in (s3_win, s3_run, s3_third):
    amod.mark_email_verified(_acct["id"])

_stub_calls = {"n": 0}


def _stub_factory(lot, invoice, success_url, cancel_url):
    _stub_calls["n"] += 1
    return {"id": f"cs_test_s3_{_stub_calls['n']}",
            "url": f"https://checkout.stripe.test/pay/{_stub_calls['n']}",
            "payment_intent": "pi_test_s3"}


amod.set_stripe_session_factory(_stub_factory)


def s3_notes(lot_id):
    raw = sqlite3.connect(amod._DB_PATH)
    raw.row_factory = sqlite3.Row
    rows = [dict(r) for r in raw.execute(
        "SELECT * FROM notifications WHERE lot_id = ?", (lot_id,))]
    raw.close()
    return rows


def s3_raw(sql, params=()):
    raw = sqlite3.connect(amod._DB_PATH)
    raw.row_factory = sqlite3.Row
    rows = [dict(r) for r in raw.execute(sql, params)]
    raw.close()
    return rows


def make_due(lot_id, when):
    raw = sqlite3.connect(amod._DB_PATH)
    raw.execute("UPDATE lots SET current_close_at = ? WHERE id = ?",
                (iso(when), lot_id))
    raw.commit()
    raw.close()


t0 = datetime.now(timezone.utc)

# Pay flow: winner pays through a fresh-session-per-click pay page.
pay_lot = make_live_lot(10000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], pay_lot["id"], 20000)
amod.place_bid(s3_run["id"], pay_lot["id"], 15000)
make_due(pay_lot["id"], t0 - timedelta(minutes=1))
close_res = amod.close_lot(pay_lot["id"], now=t0)
check("slice3 close invoices the winner", close_res["outcome"] == "invoiced")
check("INVOICE_ISSUED carries the pay-page URL",
      any(n["event"] == "INVOICE_ISSUED"
          and "/pay/" in json.loads(n["payload"]).get("pay_page_url", "")
          for n in s3_notes(pay_lot["id"])))
tok1 = amod.issue_pay_token(pay_lot["id"], now=t0)
tok2 = amod.issue_pay_token(pay_lot["id"], now=t0)
check("reissue revokes the predecessor (audit rows kept)",
      amod.get_token_by_raw(tok1["raw_token"])["revoked_at"] is not None
      and amod.get_token_by_raw(tok2["raw_token"])["revoked_at"] is None
      and len(s3_raw("SELECT * FROM pay_page_tokens WHERE lot_id = ?",
                     (pay_lot["id"],))) >= 2)
raw = sqlite3.connect(amod._DB_PATH)
try:
    raw.execute(
        "INSERT INTO pay_page_tokens (id, lot_id, invoice_id,"
        " winner_account_id, token_hash, issued_at) VALUES"
        " ('11111111-1111-1111-1111-111111111111', ?, ?, ?, 'x', ?)",
        (pay_lot["id"], close_res["invoice_id"], s3_win["id"], iso(t0)))
    raw.commit()
    check("partial unique index blocks a second ACTIVE token", False)
except sqlite3.IntegrityError:
    raw.rollback()
    check("partial unique index blocks a second ACTIVE token", True)
raw.close()
r = anon.get("/pay/" + tok2["raw_token"])
check("pay page redirects to a fresh Stripe session",
      r.status_code == 302 and _stub_calls["n"] == 1, str(r.status_code))
r = anon.get("/pay/" + tok2["raw_token"])
check("every pay-page click mints a NEW session (no caching)",
      r.status_code == 302 and _stub_calls["n"] == 2)
r = anon.get("/pay/" + tok1["raw_token"])
check("revoked token returns 410", r.status_code == 410)
r = anon.get("/pay/" + "0" * 64)
check("unknown token returns 404", r.status_code == 404)

paid = amod.handle_checkout_completed({
    "id": "cs_test_paid_1", "payment_intent": "pi_test_s3",
    "metadata": {"kind": "auction_pay", "lot_id": pay_lot["id"],
                 "invoice_id": close_res["invoice_id"],
                 "winner_account_id": s3_win["id"]}}, now=t0)
inv_paid = amod.get_invoice(close_res["invoice_id"])
check("checkout.session.completed marks invoice + lot PAID",
      paid["outcome"] == "paid" and inv_paid["status"] == "PAID"
      and inv_paid["stripe_checkout_session_id"] == "cs_test_paid_1"
      and amod.get_lot(pay_lot["id"])["status"] == "PAID")
check("payment revokes the pay token + notifies PAYMENT_CONFIRMED once",
      amod.get_token_by_raw(tok2["raw_token"])["revoked_at"] is not None
      and sum(1 for n in s3_notes(pay_lot["id"])
              if n["event"] == "PAYMENT_CONFIRMED") == 1)
replay = amod.handle_checkout_completed({
    "id": "cs_test_paid_1", "payment_intent": "pi_test_s3",
    "metadata": {"kind": "auction_pay", "lot_id": pay_lot["id"],
                 "invoice_id": close_res["invoice_id"],
                 "winner_account_id": s3_win["id"]}}, now=t0)
check("webhook replay is idempotent (already_paid, no double-write)",
      replay["outcome"] == "already_paid"
      and sum(1 for n in s3_notes(pay_lot["id"])
              if n["event"] == "PAYMENT_CONFIRMED") == 1)
expect_raises("a different session on a PAID invoice is a conflict",
              lambda: amod.handle_checkout_completed({
                  "id": "cs_test_other", "payment_intent": "pi_other",
                  "metadata": {"kind": "auction_pay",
                               "lot_id": pay_lot["id"],
                               "invoice_id": close_res["invoice_id"],
                               "winner_account_id": s3_win["id"]}}, now=t0),
              (amod.AuctionError,))

# Reminders: +24h/+48h exactly once each.
rem_lot = make_live_lot(10000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], rem_lot["id"], 12000)
make_due(rem_lot["id"], t0 - timedelta(minutes=1))
amod.close_lot(rem_lot["id"], now=t0)
r24 = amod.run_reminders(now=t0 + timedelta(hours=25))
check("+24h reminder fires once",
      r24["count"] == 1 and r24["reminders_sent"][0]["reminder_number"] == 1)
check("+24h reminder never repeats on the next pass",
      amod.run_reminders(now=t0 + timedelta(hours=26))["count"] == 0)
r48 = amod.run_reminders(now=t0 + timedelta(hours=49))
check("+48h reminder fires once",
      r48["count"] == 1 and r48["reminders_sent"][0]["reminder_number"] == 2)
check("reminder notifications carry both numbers",
      {json.loads(n["payload"])["reminder_number"]
       for n in s3_notes(rem_lot["id"])
       if n["event"] == "PAYMENT_REMINDER"} == {1, 2})

# Second-chance ladder: no-pay winner -> runner-up at their max -> accept.
sc_lot = make_live_lot(10000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], sc_lot["id"], 30000)
amod.place_bid(s3_run["id"], sc_lot["id"], 25000)
amod.place_bid(s3_third["id"], sc_lot["id"], 28000)
make_due(sc_lot["id"], t0 - timedelta(minutes=1))
sc_close = amod.close_lot(sc_lot["id"], now=t0)
sweep = amod.run_second_chance_sweep(now=t0 + timedelta(hours=73))
offers = [x for x in sweep["results"] if x["outcome"] == "offered"
          and x["lot_id"] == sc_lot["id"]]
check("72h unpaid voids the invoice and offers the runner-up at their max",
      len(offers) == 1 and offers[0]["offeree_account_id"] == s3_third["id"]
      and offers[0]["offered_price_cents"] == 28000
      and amod.get_invoice(sc_close["invoice_id"])["status"] == "VOID",
      str(sweep))
offer_id = offers[0]["offer_id"]
expect_raises("only the offeree can accept their offer",
              lambda: amod.accept_second_chance(offer_id, s3_run["id"],
                                                now=t0 + timedelta(hours=74)),
              (amod.PermissionDenied,))
t1 = t0 + timedelta(hours=74)
accepted = amod.accept_second_chance(offer_id, s3_third["id"], now=t1)
sc_lot_now = amod.get_lot(sc_lot["id"])
check("accept swaps the winner atomically and issues a new invoice + token",
      accepted["hammer_cents"] == 28000
      and accepted["buyer_premium_cents"] == 1120
      and accepted["amount_cents"] == 29120
      and sc_lot_now["winner_account_id"] == s3_third["id"]
      and sc_lot_now["winning_price_cents"] == 28000
      and sc_lot_now["status"] == "INVOICED"
      and amod.get_invoice(accepted["invoice_id"])["status"] == "OPEN"
      and "/pay/" in accepted["pay_page_url"], str(accepted))
# The new winner also never pays: ladder advances to the third bidder,
# who declines -> ladder exhausted -> relisted back to DRAFT.
sweep2 = amod.run_second_chance_sweep(now=t1 + timedelta(hours=73))
offers2 = [x for x in sweep2["results"] if x["outcome"] == "offered"
           and x["lot_id"] == sc_lot["id"]]
check("second no-pay advances the ladder to the next bidder at their max",
      len(offers2) == 1
      and offers2[0]["offeree_account_id"] == s3_run["id"]
      and offers2[0]["offered_price_cents"] == 25000, str(sweep2))
declined = amod.decline_second_chance(offers2[0]["offer_id"], s3_run["id"],
                                      now=t1 + timedelta(hours=74))
sc_final = amod.get_lot(sc_lot["id"])
check("final decline exhausts the ladder and relists the lot to DRAFT",
      declined["outcome"] == "declined" and sc_final["status"] == "DRAFT"
      and sc_final["winner_account_id"] is None
      and any(n["event"] == "LOT_RELISTED"
              and n["account_id"] == s3_seller["id"]
              for n in s3_notes(sc_lot["id"])), str(declined))

# Offer expiry path: PENDING offer lapses -> EXPIRED -> ladder exhausts.
exp_lot = make_live_lot(5000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], exp_lot["id"], 9000)
amod.place_bid(s3_run["id"], exp_lot["id"], 8000)
make_due(exp_lot["id"], t0 - timedelta(minutes=1))
amod.close_lot(exp_lot["id"], now=t0)
amod.run_second_chance_sweep(now=t0 + timedelta(hours=73))
sweep3 = amod.run_second_chance_sweep(
    now=t0 + timedelta(hours=73 + 72, minutes=1))
check("expired offer advances to exhaustion and relists",
      amod.get_lot(exp_lot["id"])["status"] == "DRAFT"
      and any(n["event"] == "SECOND_CHANCE_EXPIRED"
              for n in s3_notes(exp_lot["id"])), str(sweep3))
check("second-chance sweep is idempotent on a settled ladder",
      amod.run_second_chance_sweep(
          now=t0 + timedelta(hours=200))["count"] == 0)

# --- Slice 3 gap-check C3: bid IP audit (§8.2) ----------------------------
ip_lot = make_live_lot(10000, the_seller=s3_seller)
ip_res = amod.place_bid(s3_win["id"], ip_lot["id"], 15000,
                        ip_address="203.0.113.9",
                        user_agent="auction-test/1.0")
ip_bid = s3_raw("SELECT * FROM bids WHERE id = ?", (ip_res["bid_id"],))[0]
check("accepted bid row stores ip_address + user_agent",
      ip_bid["ip_address"] == "203.0.113.9"
      and ip_bid["user_agent"] == "auction-test/1.0")
attempts = s3_raw("SELECT * FROM bid_attempts WHERE lot_id = ?",
                  (ip_lot["id"],))
check("accepted bid attempt is audited with IP + bid link",
      any(a["outcome"] == "ACCEPTED" and a["ip_address"] == "203.0.113.9"
          and a["user_agent"] == "auction-test/1.0"
          and a["bid_id"] == ip_res["bid_id"] for a in attempts),
      str(attempts))
expect_raises("below-minimum competing bid is rejected",
              lambda: amod.place_bid(s3_run["id"], ip_lot["id"], 100,
                                     ip_address="203.0.113.10",
                                     user_agent="auction-test/1.0"),
              (amod.AuctionError,))
attempts = s3_raw("SELECT * FROM bid_attempts WHERE lot_id = ?",
                  (ip_lot["id"],))
check("rejected bid attempt is audited with its IP (no bid written)",
      any(a["outcome"] == "REJECTED" and a["ip_address"] == "203.0.113.10"
          and a["bid_id"] is None for a in attempts)
      and len(s3_raw("SELECT * FROM bids WHERE lot_id = ?",
                     (ip_lot["id"],))) == 1, str(attempts))
expect_raises("self-bid is rejected",
              lambda: amod.place_bid(s3_seller["id"], ip_lot["id"], 99000,
                                     ip_address="203.0.113.11"),
              (amod.PermissionDenied,))
attempts = s3_raw("SELECT * FROM bid_attempts WHERE lot_id = ?",
                  (ip_lot["id"],))
check("rejected self-bid attempt is audited too",
      any(a["outcome"] == "REJECTED" and a["ip_address"] == "203.0.113.11"
          for a in attempts))

# --- Slice 3 gap-check C4: reserve-not-met high-bidder offer --------------
rnm_lot = make_live_lot(1000, reserve=50000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], rnm_lot["id"], 20000)
amod.place_bid(s3_run["id"], rnm_lot["id"], 12000)
make_due(rnm_lot["id"], t0 - timedelta(minutes=1))
rnm_close = amod.close_lot(rnm_lot["id"], now=t0)
check("reserve-not-met close offers the lot to the high bidder at their max",
      rnm_close["outcome"] == "no_sale"
      and rnm_close.get("second_chance_offeree_account_id") == s3_win["id"]
      and rnm_close.get("second_chance_offered_price_cents") == 20000
      and amod.get_lot(rnm_lot["id"])["status"] == "NO_SALE",
      str(rnm_close))
rnm_offer_id = rnm_close["second_chance_offer_id"]
expect_raises("only the high bidder can accept the RNM offer",
              lambda: amod.accept_second_chance(
                  rnm_offer_id, s3_run["id"], now=t0 + timedelta(hours=1)),
              (amod.PermissionDenied,))
rnm_accepted = amod.accept_second_chance(rnm_offer_id, s3_win["id"],
                                         now=t0 + timedelta(hours=1))
rnm_now = amod.get_lot(rnm_lot["id"])
check("RNM accept moves NO_SALE -> INVOICED at the offered price + token",
      rnm_accepted["hammer_cents"] == 20000
      and rnm_accepted["buyer_premium_cents"] == 800
      and rnm_accepted["amount_cents"] == 20800
      and rnm_now["status"] == "INVOICED"
      and rnm_now["winner_account_id"] == s3_win["id"]
      and rnm_now["winning_price_cents"] == 20000
      and amod.get_invoice(rnm_accepted["invoice_id"])["status"] == "OPEN"
      and "/pay/" in rnm_accepted["pay_page_url"], str(rnm_accepted))

# RNM decline path: offer declined -> lot relists to DRAFT.
rnm2_lot = make_live_lot(1000, reserve=50000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], rnm2_lot["id"], 20000)
make_due(rnm2_lot["id"], t0 - timedelta(minutes=1))
rnm2_close = amod.close_lot(rnm2_lot["id"], now=t0)
rnm2_declined = amod.decline_second_chance(
    rnm2_close["second_chance_offer_id"], s3_win["id"],
    now=t0 + timedelta(hours=1))
check("RNM decline relists the lot to DRAFT",
      rnm2_declined["outcome"] == "declined"
      and rnm2_declined.get("lot_outcome") == "relisted"
      and amod.get_lot(rnm2_lot["id"])["status"] == "DRAFT",
      str(rnm2_declined))

# RNM expiry path: offer lapses in the sweep -> lot relists to DRAFT.
rnm3_lot = make_live_lot(1000, reserve=50000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], rnm3_lot["id"], 20000)
make_due(rnm3_lot["id"], t0 - timedelta(minutes=1))
amod.close_lot(rnm3_lot["id"], now=t0)
amod.run_second_chance_sweep(now=t0 + timedelta(hours=73))
check("expired RNM offer relists the lot to DRAFT",
      amod.get_lot(rnm3_lot["id"])["status"] == "DRAFT")

amod.set_stripe_session_factory(None)

# --- Slice 4: fee freeze, settlement, listing standard, public face --------
# Domain + HTTP on DB_B (amod._DB_PATH already points there).
s4_admin = amod.create_account("RE", "s4-admin@example.com", "S4 Admin",
                               "password123", is_admin=True)

# Premium math (4% of hammer, rounded half-up to the cent).
check("buyer premium: 4% of hammer, frozen per invoice",
      amod.buyer_premium_cents(0) == 0
      and amod.buyer_premium_cents(100) == 4
      and amod.buyer_premium_cents(15500) == 620
      and amod.buyer_premium_cents(28000) == 1120
      and amod.buyer_premium_cents(112) == 4
      and amod.buyer_premium_cents(113) == 5
      and amod.buyer_premium_cents(125) == 5)

# Close-time composition carried in the winner notifications.
pay_inv = amod.get_invoice(close_res["invoice_id"])
check("close notifications carry the frozen hammer/premium/total",
      all(json.loads(n["payload"]).get("hammer_cents") == 15500
          and json.loads(n["payload"]).get("buyer_premium_cents") == 620
          and json.loads(n["payload"]).get("amount_cents") == 16120
          for n in s3_notes(pay_lot["id"])
          if n["event"] in ("AUCTION_WON", "INVOICE_ISSUED")))
check("invoice breakdown line reads hammer + 4% premium = total",
      amod._invoice_breakdown(pay_inv)
      == " Hammer $155.00 + buyer premium (4%) $6.20 = $161.20 total.")

# Settlement: seller paid the hammer in full; platform fee is buyer-side.
expect_raises("non-admin cannot record a settlement",
              lambda: amod.record_settlement(
                  pay_lot["id"], s3_seller["id"], "check"),
              (amod.PermissionDenied,))
stl = amod.record_settlement(pay_lot["id"], s4_admin["id"], "check",
                             payout_reference="chk-1001",
                             delivery_confirmed_at=iso(
                                 t0 + timedelta(days=2)),
                             notes="Slice-4 test settlement")
check("settlement pays the seller the hammer in full (fee = 0)",
      stl["gross_amount_cents"] == 15500
      and stl["platform_fee_cents"] == 0
      and stl["seller_payout_cents"] == 15500
      and amod.get_lot(pay_lot["id"])["status"] == "SETTLED")
expect_raises("a settled lot cannot be settled twice",
              lambda: amod.record_settlement(
                  pay_lot["id"], s4_admin["id"], "check"),
              (amod.AuctionError,))

# Listing standard: approval is blocked until the checklist passes.
s4_seller = amod.create_account("RE", "s4-seller@example.com", "S4 Seller",
                                "password123", is_seller=True)
s4_bidder = amod.create_account("RE", "s4-bidder@example.com", "S4 Bidder",
                                "password123")
for _acct in (s4_seller, s4_bidder):
    amod.mark_email_verified(_acct["id"])
thin = amod.create_lot(s4_seller["id"], "RE", "1967 Camaro RS",
                       "Needs the full listing treatment.", "muscle-cars",
                       200000)
amod.submit_lot(thin["id"], s4_seller["id"])
exc = expect_raises("approval blocked while the listing standard is unmet",
                    lambda: amod.approve_lot(thin["id"], s4_admin["id"],
                                             past(1), future(2)),
                    (amod.AuctionError,))
check("block message names the missing items",
      exc is not None and "at least 20 photos" in str(exc)
      and "no-AI-photos attestation" in str(exc), str(exc))
check("checklist reports every gap on the thin lot",
      amod.moderation_checklist(amod.get_lot(thin["id"]))["passed"] is False
      and len(amod.moderation_checklist(
          amod.get_lot(thin["id"]))["missing"]) == 6)
expect_raises("only the seller can edit the listing",
              lambda: amod.update_lot_listing(thin["id"], s4_bidder["id"],
                                              flaws="x"),
              (amod.PermissionDenied,))
amod.send_back_lot(thin["id"], s4_admin["id"], note="finish the listing")
fixed = amod.update_lot_listing(
    thin["id"], s4_seller["id"],
    condition_notes="Fresh rotisserie restoration.", **lot_media("RE"))
check("seller listing update lands the checklist fields",
      fixed["flaws"] is not None
      and fixed["no_ai_photos_attested"] in (1, True)
      and amod.moderation_checklist(fixed)["passed"] is True)
amod.submit_lot(thin["id"], s4_seller["id"])
approved_thin = amod.approve_lot(thin["id"], s4_admin["id"], past(1),
                                 future(2))
check("approval succeeds once the checklist passes",
      approved_thin["status"] == "SCHEDULED")
admin_view = amod.admin_lot_dict(amod.get_lot(thin["id"]))
check("admin view carries reserve, ID photos, attestation, checklist",
      admin_view["reserve_price_cents"] is None
      and len(admin_view["id_photo_keys"]) == 2
      and admin_view["no_ai_photos_attested"] is True
      and admin_view["moderation_checklist"]["passed"] is True)

# Comments & Q&A: verified gate, seller flag, notifications, moderation.
amod.mark_email_verified(s3_seller["id"])
c_lot = make_live_lot(10000, the_seller=s3_seller)
s4_unverified = amod.create_account("RE", "s4-unverified@example.com",
                                    "S4 Unverified", "password123")
expect_raises("unverified email cannot comment",
              lambda: amod.post_comment(s4_unverified["id"], c_lot["id"],
                                        "Nice car!"),
              (amod.PermissionDenied,))
q = amod.post_comment(s4_bidder["id"], c_lot["id"],
                      "Does it have the original build sheet?")
check("bidder question posts; seller is notified (COMMENT_QUESTION)",
      q["is_seller"] is False
      and any(n["event"] == "COMMENT_QUESTION"
              and n["account_id"] == s3_seller["id"]
              for n in s3_notes(c_lot["id"])))
amod.set_watch(s4_bidder["id"], c_lot["id"], True)
a = amod.post_comment(s3_seller["id"], c_lot["id"],
                      "Yes — build sheet and Protect-o-Plate included.")
check("seller reply is flagged; watchers notified (SELLER_REPLIED)",
      a["is_seller"] is True
      and any(n["event"] == "SELLER_REPLIED"
              and n["account_id"] == s4_bidder["id"]
              for n in s3_notes(c_lot["id"])))
check("public comment list hides nothing yet",
      [c["body"] for c in amod.list_comments(c_lot["id"])]
      == ["Does it have the original build sheet?",
          "Yes — build sheet and Protect-o-Plate included."])
hidden = amod.set_comment_hidden(q["id"], s4_admin["id"],
                                 note="asks for contact info")
check("admin hide stamps the comment; public list excludes it",
      hidden["status"] == "HIDDEN"
      and [c["body"] for c in amod.list_comments(c_lot["id"])]
      == ["Yes — build sheet and Protect-o-Plate included."]
      and any(c["id"] == q["id"]
              for c in amod.list_comments(c_lot["id"],
                                         include_hidden=True)))
expect_raises("empty comment is rejected",
              lambda: amod.post_comment(s4_bidder["id"], c_lot["id"], "  "),
              (amod.AuctionError,))

# Watchlist: idempotent set/unset.
check("watch is idempotent; watched lots list once",
      amod.set_watch(s4_bidder["id"], c_lot["id"], True) is True
      and amod.set_watch(s4_bidder["id"], c_lot["id"], True) is True
      and [l["id"] for l in amod.watched_lots(s4_bidder["id"])]
      == [c_lot["id"]])
check("unwatch clears the lot",
      amod.set_watch(s4_bidder["id"], c_lot["id"], False) is False
      and amod.watched_lots(s4_bidder["id"]) == [])

# Bidder profile: counts right; another bidder's max never appears.
prof = amod.bidder_profile(s3_win["id"])
check("bidder profile counts wins and bids without leaking any max",
      prof["display_name"] == "S3 Winner"
      and prof["auctions_won"] >= 1 and prof["bids_placed"] >= 1
      and "20000" not in json.dumps(prof), json.dumps(prof))
check("seller profile counts the settled lot as sold",
      amod.bidder_profile(s3_seller["id"])["lots_sold"] >= 1)

# --- Slice 4 HTTP: public pages, comments, watchlist, pay headers ---------
# Re-arm the stub Checkout factory so pay pages redirect instead of 503.
amod.set_stripe_session_factory(_stub_factory)
pp_lot = make_live_lot(10000, the_seller=s3_seller)
amod.place_bid(s3_win["id"], pp_lot["id"], 20000)
amod.place_bid(s3_run["id"], pp_lot["id"], 15000)
make_due(pp_lot["id"], t0 - timedelta(minutes=1))
pp_close = amod.close_lot(pp_lot["id"], now=t0)
pp_tok = amod.issue_pay_token(pp_lot["id"], now=t0)
check("slice4 pay fixture closes INVOICED at 15500 + 620 premium",
      pp_close["outcome"] == "invoiced"
      and amod.get_invoice(pp_close["invoice_id"])["amount_cents"] == 16120)
sc_token = pp_tok["raw_token"]
r = anon.get("/pay/" + sc_token + "?return=success")
check("pay return page shows the hammer + premium breakdown",
      r.status_code == 200
      and "$155.00 + buyer premium (4%) $6.20 = $161.20 total."
      in r.get_data(as_text=True), r.get_data(as_text=True)[:200])
check("pay return page carries no-referrer + no-store",
      r.headers.get("Referrer-Policy") == "no-referrer"
      and r.headers.get("Cache-Control") == "no-store")
r = anon.get("/pay/" + sc_token)
check("pay redirect carries no-referrer + no-store",
      r.status_code == 302
      and r.headers.get("Referrer-Policy") == "no-referrer"
      and r.headers.get("Cache-Control") == "no-store",
      str(r.status_code))
r = anon.get("/pay/" + "0" * 64)
check("unknown pay token 404s with the same headers",
      r.status_code == 404
      and r.headers.get("Referrer-Policy") == "no-referrer"
      and r.headers.get("Cache-Control") == "no-store")
amod.set_stripe_session_factory(None)

# Public lot page on the API lot (LIVE since section 7, on DB_B).
amod.mark_email_verified(api_lot["seller_account_id"])
r = anon.get("/auctions")
check("auctions index lists the live lot",
      r.status_code == 200 and "1966 Ford Bronco"
      in r.get_data(as_text=True), str(r.status_code))
r = anon.get("/auctions/lot/" + api_lot["id"])
page_html = r.get_data(as_text=True)
check("lot page shows price, premium notice, reserve state, bid form",
      r.status_code == 200 and "+ 4% buyer premium" in page_html
      and "Reserve" in page_html and "Place bid" not in page_html
      and "1966 Ford Bronco" in page_html, page_html[:300])
check("lot page never leaks the reserve amount or a bidder max",
      "900000" not in page_html and "500000" not in page_html
      and "$5,000.00" not in page_html, page_html[:300])
r = bidder_client.post("/auctions/lot/" + api_lot["id"] + "/bid",
                       data={"amount": "6000"})
check("HTML bid form places a self-raise and redirects back",
      r.status_code == 302, str(r.status_code))
r = bidder_client.post("/api/auctions/lots/" + api_lot["id"] + "/comments",
                       json={"body": "Rust in the bed corners?"})
check("comment posts through the API",
      r.status_code == 201 and r.get_json()["is_seller"] is False,
      str(r.get_json()))
api_comment_id = r.get_json()["id"]
r = anon.get("/api/auctions/lots/" + api_lot["id"] + "/comments")
check("comment appears on the public thread",
      r.status_code == 200 and any(
          c["body"] == "Rust in the bed corners?"
          for c in r.get_json()), str(r.get_json()))
r = client.post("/api/auctions/lots/" + api_lot["id"] + "/comments",
                json={"body": "Surface only — photos 12-14 show it."})
check("seller comment through the API carries the seller flag",
      r.status_code == 201 and r.get_json()["is_seller"] is True,
      str(r.get_json()))
r = anon.get("/api/auctions/comments/" + api_comment_id + "/moderate",
             json={"action": "hide"})
check("comment moderation refuses non-admins", r.status_code == 405
      or r.status_code == 404, str(r.status_code))
r = client.post("/api/auctions/comments/" + api_comment_id + "/moderate",
                headers=ADMIN, json={"action": "hide"})
check("admin hides a comment", r.status_code == 200
      and r.get_json()["status"] == "HIDDEN", str(r.get_json()))
r = anon.get("/api/auctions/lots/" + api_lot["id"] + "/comments")
check("hidden comment leaves the public thread but stays for admins",
      all(c["body"] != "Rust in the bed corners?" for c in r.get_json()))
r = client.get("/api/auctions/lots/" + api_lot["id"] + "/comments",
               headers=ADMIN)
check("admin thread view still shows the hidden comment",
      any(c["body"] == "Rust in the bed corners?" for c in r.get_json()))
r = anon.get("/auctions/lot/" + api_lot["id"])
check("lot page comment thread shows the seller badge",
      "Seller" in r.get_data(as_text=True)
      and "Surface only" in r.get_data(as_text=True))
r = bidder_client.post("/api/auctions/lots/" + api_lot["id"] + "/watch",
                       json={"watching": True})
check("watch API turns watching on",
      r.status_code == 200 and r.get_json()["watching"] is True,
      str(r.get_json()))
r = bidder_client.get("/api/auctions/watchlist")
check("watchlist API carries the watched lot",
      r.status_code == 200 and any(
          l["id"] == api_lot["id"] for l in r.get_json()),
      str(r.get_json()))
r = bidder_client.get("/api/auctions/bidders/" + api_bidder_id)
prof_api = r.get_json()
check("bidder profile API exposes counts, never a max",
      r.status_code == 200 and prof_api["display_name"] == "API Bidder"
      and prof_api["bids_placed"] >= 1
      and "500000" not in json.dumps(prof_api), str(prof_api))
r = anon.get("/auctions/bidder/" + api_bidder_id)
check("public bidder page renders",
      r.status_code == 200 and "API Bidder" in r.get_data(as_text=True))
r = client.get("/api/auctions/lots/" + api_lot["id"] + "/checklist")
check("checklist endpoint refuses non-admins", r.status_code == 403)
r = client.get("/api/auctions/lots/" + api_lot["id"] + "/checklist",
               headers=ADMIN)
check("checklist endpoint returns the passing standard",
      r.status_code == 200 and r.get_json()["passed"] is True,
      str(r.get_json()))
r = anon.get("/auctions/login")
check("login page renders", r.status_code == 200)
r = anon.get("/auctions/register")
check("register page renders", r.status_code == 200)

# --- Slice 5 prep: cross-brand routing (Bill's standing rule) ------------
# RE directs motorcycles to IronHead; IH directs muscle cars / trucks /
# modern performance to RestorationEssentials. Data-driven map in
# auctions.py (CROSS_BRAND_ROUTES) + index/lot-page links.
check("RE motorcycle lots route to IronHead",
      amod.cross_brand_target("RE", "motorcycles")["brand"] == "IH")
check("IH muscle-car lots route to RestorationEssentials",
      amod.cross_brand_target("IH", "muscle-cars")["brand"] == "RE")
check("IH truck lots route to RestorationEssentials",
      amod.cross_brand_target("IH", "classic-trucks")["brand"] == "RE")
check("on-brand categories do not route",
      amod.cross_brand_target("RE", "muscle-cars") is None
      and amod.cross_brand_target("IH", "motorcycles") is None)
check("unknown brands/categories do not route",
      amod.cross_brand_target("XX", "motorcycles") is None
      and amod.cross_brand_target("RE", "watches") is None)
r = anon.get("/auctions?brand=RE")
check("RE auction index links to IronHead auctions",
      r.status_code == 200 and "/auctions?brand=IH" in r.get_data(as_text=True)
      and "IronHead auctions" in r.get_data(as_text=True))
r = anon.get("/auctions?brand=IH")
check("IH auction index links to the RE auctions",
      r.status_code == 200 and "/auctions?brand=RE" in r.get_data(as_text=True)
      and "RestorationEssentials auctions" in r.get_data(as_text=True))
r = anon.get("/auctions/lot/" + api_lot["id"])
check("RE lot page carries the motorcycle cross-link to IronHead",
      r.status_code == 200 and "/auctions?brand=IH" in r.get_data(as_text=True))

# --- Slice 5 fix: brand context survives auth pages (Bill 2026-10-02) ---
# An IronHead visitor following Sign in / Create account from the IH
# index keeps IronHead chrome and registers an IronHead account; the
# service's own brand stays the default with no ?brand=.
r = anon.get("/auctions/login?brand=IH")
check("IH login page carries IronHead branding",
      r.status_code == 200
      and "IronHead Auctions" in r.get_data(as_text=True)
      and 'value="IH"' in r.get_data(as_text=True))
r = anon.get("/auctions/register?brand=IH")
check("IH register page carries IronHead branding + brand field",
      r.status_code == 200
      and "IronHead Auctions" in r.get_data(as_text=True)
      and 'value="IH"' in r.get_data(as_text=True))
r = anon.get("/auctions/login")
check("login page defaults to the service brand (no SkillForge)",
      r.status_code == 200
      and "SkillForge" not in r.get_data(as_text=True))
r = anon.get("/auctions?brand=IH")
check("IH index sign-in links keep the IH brand",
      r.status_code == 200
      and "/auctions/login?brand=IH" in r.get_data(as_text=True)
      and "/auctions/register?brand=IH" in r.get_data(as_text=True))
r = anon.get("/auctions/lot/nope?brand=IH")
check("unknown-lot 404 keeps the requested brand",
      r.status_code == 404
      and "IronHead Auctions" in r.get_data(as_text=True))

# --- Slice 5 prep: IronHead catalog seed (real guide products only) -------
import csv as _csv  # noqa: E402
from catalog import load_catalog as _load_catalog  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))
_ih_csv = os.path.join(_REPO_ROOT, "data", "ironhead-catalog.csv")
_ih_json = os.path.join(_REPO_ROOT, "data", "ironhead-prices.json")
with open(_ih_csv, newline="", encoding="utf-8") as f:
    _ih_rows = list(_csv.DictReader(f))
with open(_ih_json, encoding="utf-8") as f:
    _ih_price_doc = json.load(f)
_ih_prices = {p["sku"]: p["msrp"] for p in _ih_price_doc["products"]}
check("IronHead catalog rows all carry an IH- SKU",
      len(_ih_rows) >= 7 and all(
          (r.get("sku") or "").strip().startswith("IH-")
          for r in _ih_rows))
check("IronHead price map covers every catalog row",
      all((r.get("sku") or "").strip() in _ih_prices for r in _ih_rows))
check("IronHead prices are the canonical confirmed prices",
      _ih_prices.get("IH-SHOVELHEAD-RESTORATION") == 29.95
      and _ih_prices.get("IH-CB750-SOHC") == 29.95
      and _ih_prices.get("IH-KZ1000") == 29.95
      and _ih_prices.get("IH-DUCATI-BEVEL") == 29.95
      and _ih_prices.get("IH-SHOVELHEAD-BUYERS-GUIDE") == 19.95
      and _ih_prices.get("IH-HONDA-SOHC-6PACK") == 129.0
      and _ih_prices.get("IH-FULL-CATALOG-PASS") == 449.0)
check("no duplicate IronHead SKUs",
      len({(r.get("sku") or "").strip() for r in _ih_rows}) == len(_ih_rows))
_ih_products = {p["sku"]: p for p in _load_catalog(_ih_csv, _ih_json)}
check("catalog loader keeps every IronHead row (IH- prefix registered)",
      len(_ih_products) == len(_ih_rows), str(sorted(_ih_products)))
check("guides are priced, digital, and flagged awaiting delivery files",
      all(p["price"] is not None and p["fulfillment_type"] == "digital"
          and p["purchasable"] is False for p in _ih_products.values()))

# ---------------------------------------------------------------------------
# 7. Slice 5 fix: the Postgres DDL script never goes through psycopg's
#    placeholder machinery. (Live deploy dep-db041h0u01pc738pdkfg died at
#    boot: psycopg.ProgrammingError — _init_schema sent auctions_schema.sql
#    through _Conn.execute(), which always binds a params tuple, and
#    psycopg's scan rejects the script's literal '%'.) Proven here with a
#    placeholder-strict cursor that replicates psycopg 3's rules; the
#    same rules were probe-verified against a live database.
# ---------------------------------------------------------------------------


def _psycopg_placeholder_scan(sql, params):
    """Stand-in for psycopg 3's bound-query scan: with params bound
    (anything but None), every '%' must open a %s/%b/%t placeholder, a
    %% literal, or a %(name)s placeholder. With params=None the driver
    sends the query uninterpreted. Returns the '%' count either way."""
    count = sql.count("%")
    if params is None:
        return count
    i, n = 0, len(sql)
    while i < n:
        if sql[i] != "%":
            i += 1
            continue
        nxt = sql[i + 1] if i + 1 < n else ""
        if nxt == "%":
            i += 2
        elif nxt in "sbt":
            i += 2
        elif nxt == "(":
            end = sql.find(")s", i)
            if end < 0:
                raise ValueError("incomplete placeholder: '%'")
            i = end + 2
        else:
            raise ValueError("incomplete placeholder: '%'")
    return count


class _PlaceholderStrictCursor:
    """Records every (sql, params) call and applies psycopg's scan."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        _psycopg_placeholder_scan(sql, params)
        self.calls.append((sql, params))


with open(os.path.join(os.path.dirname(os.path.abspath(amod.__file__)),
                       "auctions_schema.sql")) as f:
    _pg_script = f.read()

check("postgres schema script carries a literal '%' (the deploy killer)",
      _pg_script.count("%") >= 2)
expect_raises("bound-params scan rejects the raw script (the old bug)",
              lambda: _psycopg_placeholder_scan(_pg_script, ()))
check("unbound send passes the script through untouched",
      _psycopg_placeholder_scan(_pg_script, None) == _pg_script.count("%"))

_pg_cursor = _PlaceholderStrictCursor()
_pg_conn = amod._Conn.__new__(amod._Conn)
_pg_conn._raw = None
_pg_conn._cur = _pg_cursor
_save_dialect = amod._DIALECT
amod._DIALECT = "postgres"
try:
    _pg_conn.execute_script(_pg_script)
    check("execute_script sends the DDL with NO params bound",
          _pg_cursor.calls == [(_pg_script, None)])
    expect_raises("_Conn.execute() on the script still raises (why the "
                  "DDL needed its own path)",
                  lambda: _pg_conn.execute(_pg_script), (ValueError,))
    amod._DIALECT = "sqlite"
    expect_raises("execute_script refuses the sqlite dialect",
                  lambda: _pg_conn.execute_script(_pg_script),
                  (RuntimeError,))
finally:
    amod._DIALECT = _save_dialect

import contextlib as _ctxlib

_strict_cursor2 = _PlaceholderStrictCursor()
_init_conn = amod._Conn.__new__(amod._Conn)
_init_conn._raw = None
_init_conn._cur = _strict_cursor2


@_ctxlib.contextmanager
def _fake_pg_connect():
    yield _init_conn


_save_connect = amod._connect
amod._connect = _fake_pg_connect
amod._DIALECT = "postgres"
try:
    amod._init_schema()
    check("_init_schema (postgres) ships the script byte-identical, "
          "unbound", _strict_cursor2.calls == [(_pg_script, None)],
          str(_strict_cursor2.calls)[:120])
finally:
    amod._connect = _save_connect
    amod._DIALECT = _save_dialect

# DDL variance guards (Slice 5 live-fire fallout, real Postgres): the
# two 72h deadlines are engine-frozen plain columns (timestamptz +
# interval is not immutable, so GENERATED rejected them), and the
# script stays boot-idempotent (_init_schema re-applies it every boot).
_sql_lines = _pg_script.split("\n")
check("DDL: no GENERATED deadline over timestamptz arithmetic",
      "GENERATED ALWAYS AS (issued_at" not in _pg_script
      and "GENERATED ALWAYS AS (offered_at" not in _pg_script)
check("DDL: every CREATE TABLE is boot-guarded",
      all(("CREATE TABLE IF NOT EXISTS " in ln)
          for ln in _sql_lines if ln.startswith("CREATE TABLE")))
check("DDL: every enum CREATE TYPE sits inside a duplicate guard",
      all(_sql_lines[i - 1] == "DO $$ BEGIN"
          for i, ln in enumerate(_sql_lines)
          if ln.startswith("CREATE TYPE ")))

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL AUCTION TESTS PASSED")
