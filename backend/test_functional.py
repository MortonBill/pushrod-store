"""Functional test for the PushRod storefront (Bill 2026-09-30).

Exercises the running Flask app end-to-end via the test client: storefront
pages, brand API, product catalog API (including the 7B-last ordering rule),
product pages, cart/checkout refusal without Stripe keys, static assets, and
the RE/IH cross-links. No Stripe keys or network needed — STRIPE_READY is
False in this environment, and checkout must refuse cleanly with 503.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Match production: Render runs with BRAND=gateway (all four catalog lines).
os.environ["BRAND"] = "gateway"

import app as store_app

client = store_app.app.test_client()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# 1. Homepage renders with logo slot + cross-links
r = client.get("/")
check("homepage 200", r.status_code == 200, r.status_code)
html = r.get_data(as_text=True)
check("homepage references logo img", "pushrod-logo.png" in html)
check("homepage links RestorationEssentials",
      "restorationessentials.polsia.app" in html)
check("homepage links IronHead", "ironhead.polsia.app" in html)

# 2. Brand API
r = client.get("/api/brand")
check("/api/brand 200", r.status_code == 200, r.status_code)
b = r.get_json()
check("brand payload has expected keys",
      all(k in b for k in ("id", "name", "tagline", "stats", "sizes",
                           "stripe_ready")),
      str(sorted(b.keys())))

# 3. Product catalog API
r = client.get("/api/products")
check("/api/products 200", r.status_code == 200, r.status_code)
ps = r.get_json()
check("products is a list", isinstance(ps, list))
check("product count sane (>600)", len(ps) > 600, len(ps))
skus = [p["sku"] for p in ps]
check("SKUs unique", len(set(skus)) == len(skus))
check("required fields on every product",
      all(all(k in p for k in ("sku", "prefix", "title", "price",
                               "image_url", "purchasable")) for p in ps))
seven = [p for p in ps if p["prefix"] == "7B-"]
check("7B- seven-brand line present", len(seven) > 0)
if seven:
    tail = ps[-len(seven):]
    check("7B- products sort last in catalog",
          all(p["prefix"] == "7B-" for p in tail)
          and not any(p["prefix"] == "7B-" for p in ps[:-len(seven)]),
          [p["sku"] for p in tail])

# 4. Single-product API
sku = ps[0]["sku"]
r = client.get(f"/api/products/{sku}")
check("single product 200", r.status_code == 200
      and r.get_json()["sku"] == sku, r.status_code)
r = client.get("/api/products/NOPE-NOT-REAL")
check("unknown sku 404", r.status_code == 404, r.status_code)

# 5. Product page
r = client.get(f"/product/{sku}")
check("product page 200", r.status_code == 200, r.status_code)
r = client.get("/product/NOPE-NOT-REAL")
check("product page unknown sku 404", r.status_code == 404, r.status_code)

# 6. Checkout refuses cleanly while Stripe keys are unset
r = client.post("/api/checkout", json={"items": [{"sku": sku, "qty": 1}]})
check("checkout 503 without Stripe keys", r.status_code == 503,
      r.status_code)

# 7. Static assets served
r = client.get("/static/css/store.css")
check("store.css served", r.status_code == 200, r.status_code)
r = client.get("/static/js/store.js")
check("store.js served", r.status_code == 200, r.status_code)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL FUNCTIONAL TESTS PASSED")
