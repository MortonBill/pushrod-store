"""Native lead-capture tests (lead-magnet repair, 2026-10-08).

Covers POST /api/leads: happy path (row persisted), validation rejects,
and that the two new download pages route on their brand hosts.

Run:  python3 backend/test_leads.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["BRAND"] = "gateway"
_tmp = tempfile.mkdtemp(prefix="leads_test_")
os.environ["LEADS_DB"] = os.path.join(_tmp, "leads.db")

import app as store_app
import leads as leads_mod

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


client = store_app.app.test_client()

r = client.post("/api/leads", json={
    "brand": "pushrod", "kind": "newsletter",
    "email": "bill@aitoolsfortoday.com"})
check("pushrod lead accepted", r.status_code == 200 and r.get_json().get("ok") is True,
      f"{r.status_code} {r.get_data(as_text=True)[:120]}")

r = client.post("/api/leads", json={
    "brand": "sportroots", "kind": "coach-waitlist", "name": "Bill",
    "email": "bill@aitoolsfortoday.com",
    "fields": {"sport": "soccer", "age": "U10–U12"}})
check("coach waitlist accepted", r.status_code == 200 and r.get_json().get("ok") is True)

conn = leads_mod._db()
rows = conn.execute("SELECT brand, kind, email, fields FROM leads ORDER BY id").fetchall()
conn.close()
check("two rows persisted", len(rows) == 2, f"{len(rows)} rows")
check("fields json stored", bool(rows) and "soccer" in (rows[-1]["fields"] or ""))

r = client.post("/api/leads", json={"brand": "pushrod", "kind": "newsletter", "email": "nope"})
check("bad email rejected", r.status_code == 400)
r = client.post("/api/leads", json={"email": "a@b.co"})
check("missing brand/kind rejected", r.status_code == 400)

r = client.get("/checklist-download", headers={"Host": "everready-family.com"})
check("ER checklist-download page", r.status_code == 200 and b"checklist is ready" in r.get_data().lower())
r = client.get("/free-drills", headers={"Host": "sportrootsdrills.com"})
check("SR free-drills page", r.status_code == 200 and b"free drills" in r.get_data().lower())
r = client.get("/free-drills-download", headers={"Host": "sportrootsdrills.com"})
check("SR drills download page", r.status_code == 200 and b"sportroots-5-free-drills.pdf" in r.get_data())

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
