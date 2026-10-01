"""
PushRod storefront backend — self-contained store app.

Serves the storefront frontend (../frontend, static files) and the JSON API
the frontend talks to:

  GET  /api/products            brand-filtered catalog as JSON
  GET  /api/products/<sku>      one product
  POST /api/checkout            {items:[{sku,size,qty}], coupon?} -> Stripe Checkout (TEST ONLY)
                                 coupon "FOUNDER100" brings the total to $0.50 (test method)
  GET  /api/fulfill?session_id= verify paid session -> create Printful order
  POST /api/stripe/webhook      production: checkout.session.completed -> fulfill
  GET  /api/printful/mapping-status  SKU coverage of printful_mapping.json

Order flow: customer buys on our site -> Stripe (test mode in v1) ->
backend creates the Printful order via API -> Printful prints and ships.

Brand switching is a config change: BRAND env var selects
brands/<id>.yaml (name, theme, sku_prefixes). Same codebase, N brands.
"""
import json
import logging
import os

import stripe
import yaml
from flask import Flask, jsonify, request, send_from_directory

from catalog import load_unified_catalog, catalog_stats, APPAREL_SIZES, mapping_keys_for, mapping_complete
from fulfillment.fulfill import (
    fulfill_paid_order, load_mapping, mapping_key, build_order_items,
)
from fulfillment.printful_client import PrintfulConfigError, PrintfulAPIError
import wholesale as wholesale_mod

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pushrod")

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
FRONTEND = os.path.join(ROOT, "frontend")

BRAND_ID = os.environ.get("BRAND", "pushrod")


def load_brand(brand_id):
    path = os.path.join(ROOT, "brands", f"{brand_id}.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def catalog_sources(catalog_cfg):
    """Brand yaml carries `catalogs:` (list of {csv, prices_json}).
    A single legacy `catalog:` block is accepted for backward compat.
    Relative paths resolve against the project ROOT (portable deploys)."""
    raw = catalog_cfg["catalogs"] if "catalogs" in catalog_cfg else [catalog_cfg]
    out = []
    for c in raw:
        entry = dict(c)
        for key in ("csv", "prices_json"):
            if entry.get(key) and not os.path.isabs(entry[key]):
                entry[key] = os.path.join(ROOT, entry[key])
        out.append(entry)
    return out


brand = load_brand(BRAND_ID)
theme = brand["theme"]
catalog_cfg = brand["catalog"]
store_cfg = brand["store"]

PRODUCTS = None  # assigned below after the mapping loads
BY_SKU = {}

MAPPING_PATH = os.environ.get(
    "PRINTFUL_MAPPING",
    os.path.join(BASE, "fulfillment", "printful_mapping.json"),
)

# Honest purchasability (Bill 2026-09-30): the Printful mapping loads ONCE at
# startup and gates the catalog — a product is purchasable only when it has a
# price AND every required mapping key exists (all 6 sizes for tee/sweatshirt,
# bare SKU otherwise). No mapping entry, no sale: the storefront renders such
# cards unavailable and checkout can never 409 on them.
MAPPING = load_mapping(MAPPING_PATH)

PRODUCTS = load_unified_catalog(
    [(c["csv"], c.get("prices_json")) for c in catalog_sources(catalog_cfg)],
    sku_prefixes=brand["brand"]["sku_prefixes"],
    mapping=MAPPING,
)
BY_SKU = {p["sku"]: p for p in PRODUCTS}
_unpurch = sum(1 for p in PRODUCTS if not p["purchasable"])
log.info("printful mapping: %d keys, %d/%d products purchasable",
         len(MAPPING), len(PRODUCTS) - _unpurch, len(PRODUCTS))

# Merch-library roots for /img/<lib>/... (keys match catalog.IMAGE_LIBS).
# Bundled under data/img/ for portable deploys; DATA_DIR env overrides.
_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
IMAGE_LIB_DIRS = {
    "pushrod": os.path.join(_DATA_DIR, "img", "pushrod"),
    "muscle": os.path.join(_DATA_DIR, "img", "muscle"),
    "modern": os.path.join(_DATA_DIR, "img", "modern"),
    "truck": os.path.join(_DATA_DIR, "img", "truck"),
}

ORDER_PREFIX = store_cfg.get("order_prefix", "pushrod")
STRIPE_WEBHOOK_SECRET = os.environ.get(
    store_cfg.get("stripe_webhook_secret_env", "STRIPE_WEBHOOK_SECRET"), "")

# ---------- Stripe: TEST MODE ONLY ----------
# LIVE FLIP REQUIREMENT (Bill's honesty rule): when the business approves
# live keys, the Printful mapping must be FULLY filled first — every entry
# needs catalog_variant_id + print_file_url via fill_mapping.py — because
# fulfill_paid_order() refuses to ship from placeholder entries. The
# checkout pre-check below only verifies mapping key existence.
stripe_key = os.environ.get(store_cfg["stripe_secret_key_env"], "")
if stripe_key and not stripe_key.startswith("sk_test_"):
    raise RuntimeError(
        "REFUSED: STRIPE_TEST_SECRET_KEY does not start with sk_test_. "
        "v1 runs in TEST mode only — live keys are never accepted here."
    )
stripe.api_key = stripe_key or None
STRIPE_READY = bool(stripe_key)

# Connect routing (Bill 2026-09-30): AI Tools for Today (acct_1TttobEq0rLRALyr)
# is a Connect Express account inside the Restoration Essentials master login —
# it has no separate dashboard login or API keys. The platform's test key acts
# on its behalf via the Stripe-Account header, so charges land in the AI Tools
# for Today bucket. Unset = charge the platform (master) account directly.
STRIPE_CONNECT_ACCOUNT_ID = os.environ.get(
    store_cfg.get("stripe_connect_account_id_env",
                  "STRIPE_CONNECT_ACCOUNT_ID"), "").strip() or None


def _stripe_acct():
    return {"stripe_account": STRIPE_CONNECT_ACCOUNT_ID} \
        if STRIPE_CONNECT_ACCOUNT_ID else {}

app = Flask(__name__, static_folder=os.path.join(FRONTEND, "static"))

# ---------- wholesale partner program (Bill 2026-09-30: "build it out and wire it up") ----------
wholesale_mod.init(
    app,
    get_product=lambda sku: BY_SKU if sku == "__all__" else BY_SKU.get(sku),
    stripe_ready=STRIPE_READY,
    stripe_acct=_stripe_acct,
    publishable_key=os.environ.get(
        store_cfg.get("stripe_publishable_key_env", "STRIPE_TEST_PUBLISHABLE_KEY"), ""),
    root_dir=ROOT,
)


# ---------- storefront pages (static frontend) ----------
@app.get("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.get("/product/<sku>")
def product_page(sku):
    if sku not in BY_SKU:
        return "Not found", 404
    return send_from_directory(FRONTEND, "product.html")


@app.get("/checkout/success")
def success_page():
    return send_from_directory(FRONTEND, "success.html")


@app.get("/checkout/cancel")
def cancel_page():
    return send_from_directory(FRONTEND, "cancel.html")


# ---------- wholesale pages ----------
@app.get("/wholesale")
def wholesale_home():
    return send_from_directory(FRONTEND, "wholesale.html")


@app.get("/wholesale/apply")
def wholesale_apply():
    return send_from_directory(FRONTEND, "apply.html")


@app.get("/wholesale/login")
def wholesale_login():
    return send_from_directory(FRONTEND, "login.html")


@app.get("/wholesale/agreement")
def wholesale_agreement():
    return send_from_directory(FRONTEND, "agreement.html")


@app.get("/wholesale/agreement-body")
def wholesale_agreement_body():
    # Single source of truth for the agreement text: both agreement.html and
    # apply.html fetch and inject this fragment.
    return send_from_directory(FRONTEND, "agreement-body.html")


@app.get("/wholesale/map")
def wholesale_map():
    return send_from_directory(FRONTEND, "map.html")


@app.get("/wholesale/admin")
def wholesale_admin():
    return send_from_directory(FRONTEND, "admin.html")


@app.get("/img/<lib>/<path:filename>")
def catalog_image(lib, filename):
    # Serves catalog artwork per merch library. In production these map to
    # the CDN/site asset path (see catalog.image_base in the brand yaml).
    lib_dir = IMAGE_LIB_DIRS.get(lib)
    if not lib_dir:
        return "Not found", 404
    return send_from_directory(lib_dir, filename)


# ---------- API ----------
@app.get("/api/brand")
def api_brand():
    b = brand["brand"]
    return jsonify({
        "id": b["id"], "name": b["name"], "tagline": b["tagline"],
        "doors": b.get("doors", []),
        "theme": theme, "stats": catalog_stats(PRODUCTS),
        "sizes": APPAREL_SIZES,
        "stripe_ready": STRIPE_READY,
    })


@app.get("/api/products")
def api_products():
    # wholesale_eligible is additive metadata for the storefront's partner
    # pricing display; retail guests ignore it.
    return jsonify([
        {**p, "wholesale_eligible": wholesale_mod.is_wholesale_eligible(p)}
        for p in PRODUCTS
    ])


@app.get("/api/products/<sku>")
def api_product(sku):
    p = BY_SKU.get(sku)
    return (jsonify(p), 200) if p else (jsonify({"error": "not found"}), 404)


def _validate_cart(items, price_fn=None):
    """Re-price and validate client cart server-side. Never trust client prices.

    price_fn(product) -> (unit_cents, error_or_None); defaults to retail.
    Wholesale checkout passes a function applying the 20%-off partner price
    and rejecting non-program products."""
    lines, total_cents, errors = [], 0, []
    for it in items or []:
        sku = it.get("sku")
        qty = max(1, min(99, int(it.get("qty", 1))))
        size = it.get("size")
        p = BY_SKU.get(sku)
        if not p:
            errors.append(f"unknown sku {sku}")
            continue
        if not p["purchasable"]:
            errors.append(f"{sku} has no confirmed price and cannot be sold")
            continue
        if p["needs_size"] and size not in APPAREL_SIZES:
            errors.append(f"{sku} requires a size ({'/'.join(APPAREL_SIZES)})")
            continue
        if price_fn is not None:
            unit_cents, perr = price_fn(p)
            if perr:
                errors.append(perr)
                continue
        else:
            unit_cents = int(round(p["price"]["amount"] * 100))
        lines.append({"sku": sku, "title": p["title"], "size": size,
                      "qty": qty, "unit_cents": unit_cents})
        total_cents += unit_cents * qty
    return lines, total_cents, errors


def _wholesale_price_fn(p):
    """20%-off partner pricing; rejects anything outside the 3 launch blanks."""
    if not wholesale_mod.is_wholesale_eligible(p):
        return None, (f"{p['sku']} is not in the wholesale program "
                      f"(launch: tees, hats, pullovers only)")
    retail_cents = int(round(p["price"]["amount"] * 100))
    return wholesale_mod.wholesale_unit_cents(retail_cents), None


@app.post("/api/checkout")
def api_checkout():
    data = request.get_json(force=True)
    # Wholesale checkout: requires an approved partner session. Retail guest
    # flow is untouched when the flag is absent.
    wholesale_requested = bool(data.get("wholesale"))
    partner = wholesale_mod.current_partner() if wholesale_requested else None
    if wholesale_requested and not partner:
        return jsonify({"error": "wholesale checkout requires an approved "
                                 "partner login"}), 401
    if not STRIPE_READY:
        return jsonify({"error": "Stripe test key not configured. "
                                 "Set STRIPE_TEST_SECRET_KEY (sk_test_...) to enable checkout."}), 503
    lines, total_cents, errors = _validate_cart(
        data.get("items"),
        price_fn=_wholesale_price_fn if partner else None)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    if not lines:
        return jsonify({"error": "cart is empty"}), 400
    ship_cents = 0
    if partner:
        # Server-side minimums: 25+ units per base product, every order;
        # opening order additionally 48+ units or $500+ merchandise.
        wl_lines = [{"type": BY_SKU[l["sku"]]["type"], "qty": l["qty"],
                     "unit_cents": l["unit_cents"]} for l in lines]
        min_errors = wholesale_mod.check_wholesale_minimums(
            wl_lines, is_first_order=not partner["has_ordered"])
        if min_errors:
            return jsonify({"error": "; ".join(min_errors)}), 400
        ship_cents = wholesale_mod.bulk_shipping_cents(wl_lines)
    # Fulfillment pre-check: every line must have COMPLETE Printful mapping
    # (non-null values, not just keys) — the same rule as the purchasability
    # gate (catalog.mapping_complete). A customer must never pay for something
    # we cannot ship. build_order_items() keeps its strict completeness check
    # for actual fulfillment in fulfill_paid_order(), so a paid order with
    # unfilled entries fails loudly there instead of shipping nothing silently.
    mapping = load_mapping(MAPPING_PATH)
    unmapped = []
    for l in lines:
        if not mapping_complete(l["sku"], BY_SKU[l["sku"]]["type"], mapping):
            unmapped.append(l["sku"])
    if unmapped:
        return jsonify({"error": "No Printful mapping for: " + ", ".join(unmapped)}), 409

    # FOUNDER100 (Bill's standing test method): an API-level coupon that
    # brings the order total to exactly $0.50 for the test charge. Passed as
    # {coupon:"FOUNDER100"}; never advertised in the storefront UI. TEST MODE
    # ONLY — v1 refuses live keys at startup, so this can never discount a
    # real-money order.
    coupon = ((data or {}).get("coupon") or "").strip().upper()
    discounts = None
    if coupon == "FOUNDER100":
        if total_cents <= 50:
            return jsonify({"error": "FOUNDER100 needs an order total over $0.50"}), 400
        try:
            fc = stripe.Coupon.create(
                amount_off=total_cents - 50,
                currency=store_cfg["currency"],
                duration="once",
                name="FOUNDER100 test (order -> $0.50)",
                **_stripe_acct(),
            )
        except stripe.error.StripeError as e:
            log.warning("stripe Coupon.create failed: %r", e)
            msg = getattr(e, "user_message", None) or str(e) or "coupon failed"
            return jsonify({"error": f"Stripe error: {msg}"}), 502
        discounts = [{"coupon": fc.id}]

    base = request.host_url.rstrip("/")
    line_items = [{
        "price_data": {
            "currency": store_cfg["currency"],
            "unit_amount": l["unit_cents"],
            "product_data": {"name": f"{brand['brand']['name']} — {l['title']}"
                                     + (f" ({l['size']})" if l["size"] else "")},
        },
        "quantity": l["qty"],
    } for l in lines]
    if partner and ship_cents > 0:
        # Bulk shipping at our cost, shown as an estimate line item —
        # freight is partner-paid and excluded from minimum calculations.
        line_items.append({
            "price_data": {
                "currency": store_cfg["currency"],
                "unit_amount": ship_cents,
                "product_data": {"name": "Bulk shipping (estimate) — partner rate"},
            },
            "quantity": 1,
        })
        total_cents += ship_cents
    metadata = {"cart": json.dumps(
        [{"sku": l["sku"], "size": l["size"], "qty": l["qty"]} for l in lines])}
    if partner:
        metadata["wholesale_partner"] = partner["partner_id"]
    create_kwargs = dict(
        mode="payment",
        line_items=line_items,
        metadata=metadata,
        shipping_address_collection={"allowed_countries": ["US"]},
        success_url=base + "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=base + "/checkout/cancel",
        **_stripe_acct(),
    )
    if discounts:
        create_kwargs["discounts"] = discounts
    try:
        session = stripe.checkout.Session.create(**create_kwargs)
    except stripe.error.StripeError as e:
        # Never a bare 500: surface the Stripe failure as JSON so the
        # storefront shows it inline (and logs carry the diagnosis).
        log.warning("stripe checkout Session.create failed: %r", e)
        msg = getattr(e, "user_message", None) or str(e) or "checkout failed"
        return jsonify({"error": f"Stripe error: {msg}"}), 502
    if partner:
        wholesale_mod.record_wholesale_order(partner["partner_id"], session.id,
                                             total_cents)
    return jsonify({"checkout_url": session.url, "session_id": session.id})


@app.get("/api/fulfill")
def api_fulfill():
    """v1 local flow: after Stripe success redirect, verify the session is paid
    and create the Printful order. Production uses the webhook below instead."""
    session_id = request.args.get("session_id", "")
    if not STRIPE_READY:
        return jsonify({"error": "Stripe not configured"}), 503
    try:
        session = stripe.checkout.Session.retrieve(session_id, **_stripe_acct())
    except Exception as e:
        return jsonify({"error": f"cannot retrieve session: {e}"}), 400
    if session.payment_status != "paid":
        return jsonify({"error": f"session not paid (status={session.payment_status})"}), 402
    cart = json.loads(session.metadata.get("cart", "[]"))
    ship = session.shipping_details or {}
    cust = session.customer_details or {}
    addr = (ship.get("address") if isinstance(ship, dict) else ship.address) or {}
    if not isinstance(addr, dict):
        addr = {}
    try:
        order = fulfill_paid_order(
            stripe_session_id=session.id,
            customer_email=(cust.get("email") if isinstance(cust, dict) else cust.email) or "",
            shipping_address={
                "name": (ship.get("name") if isinstance(ship, dict) else ship.name) or "",
                "line1": addr.get("line1", ""), "line2": addr.get("line2", ""),
                "city": addr.get("city", ""), "state": addr.get("state", ""),
                "country": addr.get("country", "US"),
                "postal_code": addr.get("postal_code", ""),
            },
            cart_lines=cart, products_by_sku=BY_SKU, mapping_path=MAPPING_PATH,
            order_prefix=ORDER_PREFIX,
        )
    except PrintfulConfigError as e:
        return jsonify({"error": str(e)}), 503
    except (PrintfulAPIError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"printful_order_id": order.get("id"), "status": order.get("status"),
                    "dry_run": order.get("dry_run", False)})


@app.post("/api/stripe/webhook")
def stripe_webhook():
    """Production fulfillment path: Stripe calls this on checkout.session.completed."""
    secret = STRIPE_WEBHOOK_SECRET
    if not secret:
        # Fail closed: an unsigned webhook must never trigger fulfillment.
        # Set STRIPE_WEBHOOK_SECRET on Render to enable this endpoint.
        return jsonify({"error": "webhook secret not configured"}), 503
    payload, sig = request.data, request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception as e:
        return jsonify({"error": f"bad signature: {e}"}), 400
    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        cart = json.loads(session.get("metadata", {}).get("cart", "[]"))
        addr = (session.get("shipping_details") or {}).get("address") or {}
        try:
            fulfill_paid_order(
                stripe_session_id=session["id"],
                customer_email=(session.get("customer_details") or {}).get("email", ""),
                shipping_address={
                    "name": (session.get("shipping_details") or {}).get("name", ""),
                    "line1": addr.get("line1", ""), "line2": addr.get("line2", ""),
                    "city": addr.get("city", ""), "state": addr.get("state", ""),
                    "country": addr.get("country", "US"),
                    "postal_code": addr.get("postal_code", ""),
                },
                cart_lines=cart, products_by_sku=BY_SKU, mapping_path=MAPPING_PATH,
                order_prefix=ORDER_PREFIX,
            )
        except Exception as e:  # noqa: BLE001 — webhook must not crash; alert instead
            log.exception("fulfillment failed for %s", session["id"])
            return jsonify({"error": str(e)}), 500
    return jsonify({"received": True})


@app.get("/api/printful/mapping-status")
def mapping_status():
    mapping = load_mapping(MAPPING_PATH)
    missing = []
    for p in PRODUCTS:
        if not p["purchasable"]:
            continue
        if not mapping_complete(p["sku"], p["type"], mapping):
            missing.append(p["sku"])
    return jsonify({"mapped": len(mapping), "unmapped_keys": missing})


if __name__ == "__main__":
    log.info("brand=%s products=%d purchasable=%d stripe_ready=%s stripe_connect=%s",
             BRAND_ID, len(PRODUCTS),
             sum(1 for p in PRODUCTS if p["purchasable"]), STRIPE_READY,
             STRIPE_CONNECT_ACCOUNT_ID or "platform")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8091)))
