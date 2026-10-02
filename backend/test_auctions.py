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

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL AUCTION TESTS PASSED")
