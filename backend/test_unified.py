"""Unified-catalog test: 640 SKUs loaded, zero collisions, every item
purchasable, gateway + brand slices correct, cart/checkout/fulfill green.

Bill's order 2026-09-27: build the store, add the gateway, cart, checkout,
API integration, testing, load all 640 with unique SKUs, test again, mirror
to RE. This file is the "test again" pass on the loaded 640.

Run: ./../.venv/bin/python backend/test_unified.py   (from pushrod-store/)
Stripe is stubbed (no network/keys); Printful runs PRINTFUL_DRY_RUN=1.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PRINTFUL_DRY_RUN"] = "1"
os.environ.setdefault("BRAND", "gateway")  # unified catalog: all 640

import app as store_app
from catalog import APPAREL_SIZES, load_unified_catalog, sku_prefix
from fulfillment.fulfill import build_order_items, mapping_key

# ---- stub Stripe ----
created = {}


class FakeSession:
    def __init__(self, **kw):
        self.id = "cs_test_unified1"
        self.url = "https://checkout.stripe.com/pay/cs_test_unified1"
        self.payment_status = "paid"
        self.metadata = kw.get("metadata", {})
        self.customer_details = type("o", (), {"email": "buyer@example.com"})()
        self.shipping_details = type("o", (), {
            "name": "Test Buyer",
            "address": {"line1": "1 Garage Way", "line2": "", "city": "Des Moines",
                        "state": "IA", "country": "US", "postal_code": "50310"},
        })()


store_app.stripe.checkout.Session.create = lambda **kw: created.setdefault(
    "cs_test_unified1", FakeSession(**kw))
store_app.stripe.checkout.Session.retrieve = lambda sid: created[sid]
store_app.STRIPE_READY = True

# ---- seed a Printful mapping for one SKU per line (sized + unsized) ----
SAMPLES = [
    ("PR-T001", "L"), ("PR-H001", None),
    ("RE-MC-T001", "M"), ("RE-MC-H001", None),
    ("RE-MP-T001", "XL"), ("RE-MP-M001", None),
    ("RE-CT-T001", "2XL"), ("RE-CT-M001", None),
]
mapping = {"mappings": {
    mapping_key(sku, size): {
        "catalog_variant_id": 9000 + i, "placement": "front",
        "print_file_url": f"https://example.com/{sku}.png", "technique": "dtg"}
    for i, (sku, size) in enumerate(SAMPLES)
}}
mapping_path = "/tmp/test_unified_mapping.json"
with open(mapping_path, "w") as f:
    json.dump(mapping, f)
store_app.MAPPING_PATH = mapping_path

client = store_app.app.test_client()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


# 1. unified catalog: 640, zero collisions, sorted by SKU
prods = client.get("/api/products").get_json()
check("640 products served", len(prods) == 640, f"got {len(prods)}")
skus = [p["sku"] for p in prods]
check("zero SKU collisions", len(set(skus)) == len(skus))
check("sorted by SKU", skus == sorted(skus))

# 2. prefix partition: 160 per line, owners correct
from collections import Counter
by_prefix = Counter(p["prefix"] for p in prods)
check("160 per prefix", dict(by_prefix) == {
    "PR-": 160, "RE-MC-": 160, "RE-CT-": 160, "RE-MP-": 160}, str(dict(by_prefix)))
check("owners correct",
      all(p["owner"] == ("pushrod" if p["prefix"] == "PR-" else "restorationessentials")
          for p in prods))

# 3. every item purchasable: confirmed price, no "Price TBD", no draft-only
unpurch = [p["sku"] for p in prods if not p["purchasable"]]
check("all 640 purchasable", not unpurch, str(unpurch[:5]))
drafts = [p["sku"] for p in prods if p["price"]["status"] != "confirmed"]
check("all prices confirmed (none draft)", not drafts, str(drafts[:5]))

# 4. trademark: no IronHead wordmark anywhere in the catalog
bad = [p["sku"] for p in prods
       if "ironhead" in (p["title"] + " " + p["description"]).lower()]
check("no IronHead wordmark in catalog", not bad, str(bad[:5]))

# 5. gateway brand API: doors present, 4 doors, stats sane
b = client.get("/api/brand").get_json()
check("gateway id", b["id"] == "gateway")
check("4 doors", len(b.get("doors", [])) == 4, str([d["label"] for d in b.get("doors", [])]))
check("door prefixes cover catalog",
      {d["prefix"] for d in b["doors"]} == {"PR-", "RE-MC-", "RE-CT-", "RE-MP-"})
check("stats total 640", b["stats"]["total"] == 640)
check("stats price_tbd 0", b["stats"]["price_tbd"] == 0)

# 6. images: one design file per line resolves via /img/<lib>/
for sku, _ in SAMPLES:
    p = next(x for x in prods if x["sku"] == sku)
    r = client.get(p["image_url"])
    check(f"image 200 {sku}", r.status_code == 200, f"{p['image_url']} -> {r.status_code}")

# 7. cart validation across all four lines (sized + unsized)
r = client.post("/api/checkout", json={"items": [
    {"sku": s, "size": z, "qty": 1} for s, z in SAMPLES]})
j = r.get_json()
check("mixed 8-line cart -> checkout session",
      r.status_code == 200 and "checkout_url" in j, str(r.status_code))

# 8. unmapped SKU blocked pre-payment (not in seeded mapping)
r = client.post("/api/checkout", json={"items": [{"sku": "RE-CT-T002", "size": "L", "qty": 1}]})
check("unmapped RE SKU blocked pre-payment", r.status_code == 409)

# 9. fulfill the paid session -> Printful dry-run order (gateway prefix)
r = client.get("/api/fulfill?session_id=cs_test_unified1")
j = r.get_json()
check("fulfill dry-run ok", r.status_code == 200 and j.get("dry_run") is True,
      str(r.status_code) + " " + str(j)[:100])

# 10. mapping-status endpoint sane
r = client.get("/api/printful/mapping-status").get_json()
check("mapping-status reports", r["mapped"] == len(SAMPLES),
      f"mapped={r['mapped']}")

# 11. brand slices (catalog layer): pushrod 160 / RE 480
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
push = load_unified_catalog(
    [(c["csv"], c.get("prices_json")) for c in
     __import__("yaml").safe_load(open(os.path.join(ROOT, "brands/pushrod.yaml")))["catalog"]["catalogs"]],
    sku_prefixes=["PR-"])
re_conf = __import__("yaml").safe_load(open(os.path.join(ROOT, "brands/restorationessentials.yaml")))
re = load_unified_catalog(
    [(c["csv"], c.get("prices_json")) for c in re_conf["catalog"]["catalogs"]],
    sku_prefixes=re_conf["brand"]["sku_prefixes"])
check("pushrod slice 160", len(push) == 160, str(len(push)))
check("RE slice 480", len(re) == 480, str(len(re)))
check("RE slice has no PR-", all(p["prefix"] != "PR-" for p in re))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
