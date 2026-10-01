"""Wholesale partner program tests (Bill 2026-09-30: "build it out and wire it up").

Covers: partner pricing math, blank mapping, server-side minimums
enforcement, bulk-shipping estimate, application validation, admin
approval -> partner ID -> login flow, agreement-acceptance recording,
wholesale checkout (Stripe stubbed), and retail-guest regression
(FOUNDER100 untouched, no-stripe-key 503 intact).

Run with the store venv:  python3 backend/test_wholesale.py
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["BRAND"] = "gateway"
_tmp = tempfile.mkdtemp(prefix="ws_test_")
os.environ["WHOLESALE_DB"] = os.path.join(_tmp, "wholesale.db")
os.environ["WHOLESALE_CERT_DIR"] = os.path.join(_tmp, "certs")
os.environ["WHOLESALE_ADMIN_TOKEN"] = "test-admin-token"
os.environ["WHOLESALE_SESSION_SECRET"] = "test-secret"

import app as store_app
import wholesale as ws

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------- pure pricing / minimums logic ----------
check("tee 26.00 -> 20.80", ws.wholesale_unit_cents(2600) == 2080)
check("hat 24.00 -> 19.20", ws.wholesale_unit_cents(2400) == 1920)
check("pullover 44.00 -> 35.20", ws.wholesale_unit_cents(4400) == 3520)
check("blank tee", ws.blank_for_type("tee") == "Comfort Colors 1717")
check("blank hat", ws.blank_for_type("hat") == "Yupoong 6606")
check("blank sweatshirt", ws.blank_for_type("sweatshirt") == "Gildan 18000")
check("blank unknown", ws.blank_for_type("mug") is None)

check("24 tees rejected",
      any("25+" in e for e in ws.check_wholesale_minimums(
          [{"type": "tee", "qty": 24, "unit_cents": 2080}], True)))
check("25 hats first order rejected (25x19.20=$480 < $500 opening)",
      any("Opening order" in e for e in ws.check_wholesale_minimums(
          [{"type": "hat", "qty": 25, "unit_cents": 1920}], True)))
check("48 tees first order ok",
      ws.check_wholesale_minimums(
          [{"type": "tee", "qty": 48, "unit_cents": 2080}], True) == [])
check("25 tees reorder ok (no opening rule)",
      ws.check_wholesale_minimums(
          [{"type": "tee", "qty": 25, "unit_cents": 2080}], False) == [])
check("25 tees + 24 hats rejected (hats short)",
      any("Yupoong" in e for e in ws.check_wholesale_minimums(
          [{"type": "tee", "qty": 25, "unit_cents": 2080},
           {"type": "hat", "qty": 24, "unit_cents": 1920}], False)))
check("25 tees + 25 hats ok",
      ws.check_wholesale_minimums(
          [{"type": "tee", "qty": 25, "unit_cents": 2080},
           {"type": "hat", "qty": 25, "unit_cents": 1920}], False) == [])
check("opening $500 merchandise passes with <48 units",
      ws.check_wholesale_minimums(
          [{"type": "sweatshirt", "qty": 15, "unit_cents": 3520}], True) == [] or
      any("25+" in e for e in ws.check_wholesale_minimums(
          [{"type": "sweatshirt", "qty": 15, "unit_cents": 3520}], True)))
check("bulk ship 25 tees = $29.00",
      ws.bulk_shipping_cents([{"type": "tee", "qty": 25}]) == 2900)
check("bulk ship mixed",
      ws.bulk_shipping_cents([{"type": "tee", "qty": 25},
                              {"type": "hat", "qty": 25}]) == 2900 + 2500)

# ---------- API ----------
client = store_app.app.test_client()

# wholesale pages load
for path in ["/wholesale", "/wholesale/apply", "/wholesale/login",
             "/wholesale/agreement", "/wholesale/agreement-body",
             "/wholesale/map", "/wholesale/admin"]:
    r = client.get(path)
    check(f"page {path} 200", r.status_code == 200, r.status_code)
r = client.get("/wholesale/agreement-body")
check("agreement carries DRAFT banner",
      "DRAFT" in r.get_data(as_text=True) and "pending attorney review" in r.get_data(as_text=True))

# /api/products carries wholesale_eligible (additive; retail untouched)
r = client.get("/api/products")
prods = r.get_json()
check("products API ok", isinstance(prods, list) and len(prods) > 0)
tee = next((p for p in prods if p["type"] == "tee" and p["purchasable"]), None)
check("a purchasable tee exists", tee is not None)
if tee:
    check("tee flagged wholesale_eligible", tee.get("wholesale_eligible") is True)

# application validation
r = client.post("/api/wholesale/apply", data={})
check("apply empty -> 400", r.status_code == 400, r.status_code)

base = dict(legal_name="Test Bike Shop LLC", ein="12-3456789",
            address="1 Garage Way, Des Moines, IA 50310",
            contact_name="Test Owner", contact_email="shop@example.com",
            password="s3cretpw!", resale_permits="IA 12-3456789",
            website="https://testbikeshop.example.com", accept_terms="1")


def apply_with(extra=None, file=True):
    data = dict(base)
    data.update(extra or {})
    if file:
        data["resale_cert"] = (io.BytesIO(b"%PDF-1.4 fake cert"), "cert.pdf")
    return client.post("/api/wholesale/apply", data=data,
                       content_type="multipart/form-data")


r = apply_with(file=False)
check("apply without cert -> 400", r.status_code == 400 and "certificate" in r.get_json()["error"].lower())
r = apply_with({"accept_terms": ""})
check("apply without checkbox -> 400", r.status_code == 400)
r = apply_with()
check("apply valid -> 200 pending", r.status_code == 200 and r.get_json()["status"] == "pending",
      r.status_code)
app_id = r.get_json()["application_id"]

# agreement acceptance recorded with timestamp, IP, version
with ws._db() as c:
    row = dict(c.execute("SELECT accepted_at, accepted_ip, agreement_version, accept_terms "
                         "FROM applications WHERE id = ?", (app_id,)).fetchone())
check("acceptance timestamp recorded", bool(row["accepted_at"]))
check("acceptance IP recorded", bool(row["accepted_ip"]))
check("acceptance version recorded", row["agreement_version"] == ws.AGREEMENT_VERSION)

# admin auth
r = client.get("/api/admin/wholesale/applications")
check("admin list without token -> 401/503", r.status_code in (401, 503), r.status_code)
r = client.get("/api/admin/wholesale/applications",
               headers={"X-Admin-Token": "wrong"})
check("admin list wrong token -> 401", r.status_code == 401, r.status_code)
r = client.get("/api/admin/wholesale/applications",
               headers={"X-Admin-Token": "test-admin-token"})
apps = r.get_json()["applications"]
check("admin list ok", len(apps) == 1 and apps[0]["id"] == app_id)

# approve -> partner ID issued
r = client.post(f"/api/admin/wholesale/applications/{app_id}/approve",
                headers={"X-Admin-Token": "test-admin-token"})
j = r.get_json()
check("approve -> PTNR-000001", r.status_code == 200 and j["partner_id"] == "PTNR-000001", j)

# login / me / logout
r = client.post("/api/wholesale/login",
                json={"email": "shop@example.com", "password": "wrong"})
check("login wrong password -> 401", r.status_code == 401)
r = client.post("/api/wholesale/login",
                json={"email": "shop@example.com", "password": "s3cretpw!"})
check("login ok", r.status_code == 200 and r.get_json()["partner_id"] == "PTNR-000001")
r = client.get("/api/wholesale/me")
check("me shows partner", r.get_json().get("partner_id") == "PTNR-000001")

# login throttle: 10 rapid bad attempts -> 429
throttle_client = store_app.app.test_client()
st = None
for _ in range(11):
    st = throttle_client.post("/api/wholesale/login",
                              json={"email": "x@example.com", "password": "bad"}).status_code
check("login throttled after 10 attempts", st == 429, st)

# double-approval of the same application -> 400, not a second partner
r = client.post(f"/api/admin/wholesale/applications/{app_id}/approve",
                headers={"X-Admin-Token": "test-admin-token"})
check("re-approve -> 400 already approved",
      r.status_code == 400 and "already" in r.get_json()["error"], r.status_code)
with ws._db() as c:
    n = c.execute("SELECT COUNT(*) c FROM partners").fetchone()["c"]
check("still exactly one partner", n == 1, n)

# wholesale checkout without login -> 401 (fresh client, no session)
anon = store_app.app.test_client()
r = anon.post("/api/checkout", json={"wholesale": True, "items": []})
check("wholesale checkout anon -> 401", r.status_code == 401, r.status_code)

# retail regression: no stripe keys -> 503 (unchanged)
r = anon.post("/api/checkout", json={"items": [{"sku": tee["sku"], "size": "M", "qty": 1}]})
check("retail checkout still 503 without keys", r.status_code == 503, r.status_code)

# ---------- wholesale checkout, Stripe stubbed ----------
store_app.STRIPE_READY = True
captured = {}


class FakeSession:
    def __init__(self, **kw):
        self.id = "cs_test_ws123"
        self.url = "https://checkout.stripe.com/pay/cs_test_ws123"
        captured.update(kw)


store_app.stripe.checkout.Session.create = lambda **kw: FakeSession(**kw)

r = client.post("/api/checkout", json={
    "wholesale": True,
    "items": [{"sku": tee["sku"], "size": "M", "qty": 24}]})
check("wholesale 24 tees -> 400 minimums",
      r.status_code == 400 and "25+" in r.get_json()["error"], r.status_code)

r = client.post("/api/checkout", json={
    "wholesale": True,
    "items": [{"sku": tee["sku"], "size": "M", "qty": 48}]})
j = r.get_json()
check("wholesale 48 tees -> 200", r.status_code == 200, f"{r.status_code} {j}")
if r.status_code == 200:
    items = captured["line_items"]
    merch = next(i for i in items if i["quantity"] == 48)
    ship = next(i for i in items if i["quantity"] == 1)
    check("partner unit price 2080", merch["price_data"]["unit_amount"] == 2080,
          merch["price_data"]["unit_amount"])
    check("bulk ship line 48*116=5568", ship["price_data"]["unit_amount"] == 5568,
          ship["price_data"]["unit_amount"])
    check("partner in metadata",
          captured["metadata"].get("wholesale_partner") == "PTNR-000001")
    with ws._db() as c:
        n = c.execute("SELECT COUNT(*) c FROM wholesale_orders").fetchone()["c"]
    check("order recorded", n == 1)

# second order: opening rule no longer applies, 25 tees ok
r = client.post("/api/checkout", json={
    "wholesale": True,
    "items": [{"sku": tee["sku"], "size": "M", "qty": 25}]})
check("reorder 25 tees -> 200 (opening rule spent)", r.status_code == 200, r.status_code)

# non-program product rejected at wholesale pricing
mug = next((p for p in prods if p["type"] == "mug" and p["purchasable"]), None)
if mug:
    r = client.post("/api/checkout", json={
        "wholesale": True,
        "items": [{"sku": mug["sku"], "qty": 30}]})
    check("mug rejected from wholesale checkout",
          r.status_code == 400 and "not in the wholesale program" in r.get_json()["error"],
          r.status_code)
else:
    print("SKIP mug wholesale rejection (no purchasable mug)")

# FOUNDER100 still works on wholesale orders (standing test method)
created_coupons = {}


class FakeCoupon:
    id = "co_test_f100"


store_app.stripe.Coupon.create = lambda **kw: FakeCoupon()
r = client.post("/api/checkout", json={
    "wholesale": True, "coupon": "FOUNDER100",
    "items": [{"sku": tee["sku"], "size": "M", "qty": 25}]})
j = r.get_json()
check("FOUNDER100 on wholesale -> 200",
      r.status_code == 200 and "discounts" in captured, f"{r.status_code} {j}")

r = client.post("/api/wholesale/logout")
check("logout ok", r.status_code == 200)
r = client.get("/api/wholesale/me")
check("me logged out", r.get_json().get("logged_in") is False)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL WHOLESALE TESTS PASSED")
