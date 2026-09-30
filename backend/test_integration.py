"""End-to-end integration test: cart validation -> Stripe session (stubbed) ->
fulfill -> Printful order (dry-run). Run with the store venv."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PRINTFUL_DRY_RUN"] = "1"

import app as store_app
from catalog import APPAREL_SIZES, load_unified_catalog
from collections import Counter

# Stub Stripe: no network, no keys
created = {}


class FakeSession:
    def __init__(self, **kw):
        self.id = "cs_test_fake123"
        self.url = "https://checkout.stripe.com/pay/cs_test_fake123"
        self.payment_status = "paid"
        self.metadata = kw.get("metadata", {})
        self.customer_details = type("o", (), {"email": "buyer@example.com"})()
        self.shipping_details = type("o", (), {
            "name": "Test Buyer",
            "address": {"line1": "1 Garage Way", "line2": "", "city": "Des Moines",
                        "state": "IA", "country": "US", "postal_code": "50310"},
        })()


def fake_create(**kw):
    s = FakeSession(**kw)
    created[s.id] = s
    return s


def fake_retrieve(sid):
    assert sid in created, "unknown session"
    return created[sid]


store_app.stripe.checkout.Session.create = fake_create
store_app.stripe.checkout.Session.retrieve = fake_retrieve
store_app.STRIPE_READY = True

# Seed a Printful mapping for one SKU so fulfillment can proceed
mapping_path = "/tmp/test_mapping.json"
with open(mapping_path, "w") as f:
    json.dump({"mappings": {
        "PR-T001:L": {"catalog_variant_id": 4011, "placement": "front",
                      "print_file_url": "https://example.com/pr-t001.png", "technique": "dtg"},
    }}, f)
store_app.MAPPING_PATH = mapping_path

client = store_app.app.test_client()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


# 1. cart validation: good cart
r = client.post("/api/checkout", json={"items": [{"sku": "PR-T001", "size": "L", "qty": 2}]})
j = r.get_json()
check("checkout creates session", r.status_code == 200 and "checkout_url" in j, str(r.status_code))

# 2. unpriced SKU rejected (simulate by stripping the price in-memory —
# the real catalog is now fully priced, so the guard is tested with a fixture)
_saved = dict(store_app.BY_SKU["PR-H001"])
store_app.BY_SKU["PR-H001"]["price"] = None
store_app.BY_SKU["PR-H001"]["purchasable"] = False
r = client.post("/api/checkout", json={"items": [{"sku": "PR-H001", "qty": 1}]})
check("unpriced sku rejected", r.status_code == 400, r.get_json().get("error", "")[:60])
store_app.BY_SKU["PR-H001"].update(_saved)

# 3. missing size rejected
r = client.post("/api/checkout", json={"items": [{"sku": "PR-T001", "qty": 1}]})
check("missing size rejected", r.status_code == 400, r.get_json().get("error", "")[:60])

# 4. bad size rejected
r = client.post("/api/checkout", json={"items": [{"sku": "PR-T001", "size": "XXL", "qty": 1}]})
check("bad size rejected", r.status_code == 400)

# 5. unmapped SKU+size blocked before payment
r = client.post("/api/checkout", json={"items": [{"sku": "PR-T002", "size": "M", "qty": 1}]})
check("unmapped variant blocked pre-payment", r.status_code == 409, r.get_json().get("error", "")[:70])

# 6. unknown SKU rejected
r = client.post("/api/checkout", json={"items": [{"sku": "PR-X999", "qty": 1}]})
check("unknown sku rejected", r.status_code == 400)

# 7. fulfill a paid session -> Printful dry-run order
r = client.get("/api/fulfill?session_id=cs_test_fake123")
j = r.get_json()
check("fulfill creates printful order", r.status_code == 200 and "printful_order_id" in j,
      str(r.status_code) + " " + str(j)[:120])

# 8. gateway-ready: brand filter excludes nothing for pushrod, prefix model intact
prods = client.get("/api/products").get_json()
check("156 products served", len(prods) == 156)
check("all PR- owned by pushrod", all(p["owner"] == "pushrod" for p in prods))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
