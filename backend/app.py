"""
PushRod storefront backend — self-contained store app.

Serves the storefront frontend (../frontend, static files) and the JSON API
the frontend talks to:

  GET  /api/products            brand-filtered catalog as JSON
  GET  /api/products/<sku>      one product
  POST /api/checkout            {items:[{sku,size,qty}], coupon?} -> Stripe Checkout
                                 coupon "FOUNDER100" brings the total to $0.50 (test mode only)
  GET  /api/fulfill?session_id= verify paid session -> create Printful order
  POST /api/stripe/webhook      production: checkout.session.completed -> fulfill
  GET  /api/printful/mapping-status  SKU coverage of printful_mapping.json
  GET  /download/<token>        signed, expiring download for a digital SKU
                                 (token minted at fulfillment — see
                                 fulfillment/digital.py)
  POST /api/sr/checkout         SportRoots subscription checkout (DARK:
                                 SR_BILLING_ENABLED=1; see subscriptions.py)
  POST /api/sr/portal           SportRoots Customer Portal session (DARK)
  GET  /api/sr/entitlement      signed subscriber check for Polsia (DARK)

Order flow: customer buys on our site -> Stripe (test or live per STRIPE_MODE) ->
backend creates the Printful order via API -> Printful prints and ships.

Brand switching is a config change: BRAND env var selects
brands/<id>.yaml (name, theme, sku_prefixes). Same codebase, N brands.
"""
import json
import logging
import os

import stripe
import yaml
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from catalog import load_unified_catalog, catalog_stats, APPAREL_SIZES, mapping_keys_for, mapping_complete
from fulfillment.fulfill import (
    fulfill_paid_order, load_mapping, mapping_key, build_order_items,
    split_cart_lines,
)
from fulfillment import digital as digital_mod
from fulfillment import storage as storage_mod
from fulfillment.printful_client import PrintfulConfigError, PrintfulAPIError
import wholesale as wholesale_mod
import auctions as auctions_mod
import subscriptions as sr_mod

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
# If the bundled copy is absent (the build-time Catbox fetch in
# render-build.sh failed while that host was unreachable), fall back to
# the persistent-disk cache (/var/data/img-cache) so a deploy can never
# take the merch artwork down with it.
_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))


def _img_lib_dir(lib):
    bundled = os.path.join(_DATA_DIR, "img", lib)
    if os.path.isdir(bundled):
        return bundled
    if os.path.isdir("/var/data"):
        cached = os.path.join("/var/data", "img-cache", lib)
        if os.path.isdir(cached):
            return cached
    return bundled


IMAGE_LIB_DIRS = {lib: _img_lib_dir(lib)
                  for lib in ("pushrod", "muscle", "modern", "truck")}

ORDER_PREFIX = store_cfg.get("order_prefix", "pushrod")
STRIPE_WEBHOOK_SECRET = os.environ.get(
    store_cfg.get("stripe_webhook_secret_env", "STRIPE_WEBHOOK_SECRET"), "")

# ---------- Stripe: test/live mode switch ----------
# STRIPE_MODE=live selects the LIVE keypair (Bill 2026-10-01: "turn it all the
# way up" — the store takes real orders). Live mode refuses to boot unless
# STRIPE_LIVE_SECRET_KEY is a real live key. Customer protection does not
# depend on this switch: /api/checkout 409-rejects any line whose Printful
# mapping is incomplete (same rule as the purchasability gate), so a customer
# can never pay for something we cannot ship.
STRIPE_MODE = os.environ.get("STRIPE_MODE", "test").strip().lower()
if STRIPE_MODE == "live":
    stripe_key = os.environ.get("STRIPE_LIVE_SECRET_KEY", "")
    PUBLISHABLE_KEY = os.environ.get("STRIPE_LIVE_PUBLISHABLE_KEY", "")
    if not stripe_key.startswith("sk_live_"):
        raise RuntimeError(
            "REFUSED: STRIPE_MODE=live but STRIPE_LIVE_SECRET_KEY is missing "
            "or not a live key (must start with sk_live_).")
else:
    stripe_key = os.environ.get(store_cfg["stripe_secret_key_env"], "")
    PUBLISHABLE_KEY = os.environ.get(
        store_cfg.get("stripe_publishable_key_env", "STRIPE_TEST_PUBLISHABLE_KEY"), "")
    if stripe_key and not stripe_key.startswith("sk_test_"):
        raise RuntimeError(
            "REFUSED: test-mode Stripe key does not start with sk_test_. "
            "Set STRIPE_MODE=live with STRIPE_LIVE_SECRET_KEY for live keys.")
stripe.api_key = stripe_key or None
STRIPE_READY = bool(stripe_key)
log.info("stripe_mode=%s stripe_ready=%s", STRIPE_MODE, STRIPE_READY)

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


# Stripe Tax product tax codes (docs.stripe.com/tax/tax-codes). Digital lines
# are downloadable PDFs sold with permanent rights -> Digital Books code.
# Physical lines stay tangible goods; the wholesale freight line is Shipping.
# The account's preset (Electronically Supplied Services, txcd_10000000) only
# applies as a fallback for products with no code set directly.
DIGITAL_TAX_CODE = "txcd_10302000"    # Digital Books - downloaded - permanent rights
PHYSICAL_TAX_CODE = "txcd_99999999"   # General - Tangible Goods
SHIPPING_TAX_CODE = "txcd_92010001"   # Shipping


def _sget(obj, key, default=None):
    """Read `key` from a plain dict OR a stripe-python 15.x StripeObject.

    On StripeObject, .get() raises AttributeError and attribute access on a
    missing key raises AttributeError (KeyError chained inside) — both crash
    Flask into an HTML 500. `in` + item access is the safe pattern.
    Never raises: returns `default` when the key is absent or unreadable.
    """
    if obj is None:
        return default
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        if key in obj:
            return obj[key]
    except Exception:
        pass
    return default


app = Flask(__name__, static_folder=os.path.join(FRONTEND, "static"))

# ---------- wholesale partner program (Bill 2026-09-30: "build it out and wire it up") ----------
wholesale_mod.init(
    app,
    get_product=lambda sku: BY_SKU if sku == "__all__" else BY_SKU.get(sku),
    stripe_ready=STRIPE_READY,
    stripe_acct=_stripe_acct,
    publishable_key=PUBLISHABLE_KEY,
    root_dir=ROOT,
)

# ---------- auction engine (RE/IH; opt-in per brand yaml / AUCTIONS_ENABLED) ----------
auctions_mod.init(app, brand_cfg=brand, root_dir=ROOT)


# ---------- storefront pages (static frontend) ----------
@app.get("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.get("/product/<sku>")
def product_page(sku):
    p = BY_SKU.get(sku)
    # Dark-staged rows (listed=0) have no public product page.
    if not p or not p.get("listed", True):
        return "Not found", 404
    return send_from_directory(FRONTEND, "product.html")


@app.get("/checkout/success")
def success_page():
    return send_from_directory(FRONTEND, "success.html")


@app.get("/checkout/cancel")
def cancel_page():
    return send_from_directory(FRONTEND, "cancel.html")


# ---------- SEO discovery (sitemap / robots) ----------
@app.get("/sitemap.xml")
def sitemap_xml():
    # Base URL from the request host (never hardcoded) so every host this
    # service serves gets URLs on its own domain. RestorationEssentials-owned
    # products only: restoreessentials.com is the RE storefront face, and
    # other brands' SKUs belong on their own domains.
    base = request.host_url.rstrip("/")
    entries = [base + "/"]
    for p in PRODUCTS:
        if (p.get("listed", True) and p["purchasable"]
                and p["owner"] == "restorationessentials"):
            entries.append(f"{base}/product/{p['sku']}")
    urls = "\n".join(f"  <url><loc>{u}</loc></url>" for u in entries)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{urls}\n</urlset>\n")
    return Response(xml, mimetype="application/xml")


@app.get("/robots.txt")
def robots_txt():
    base = request.host_url.rstrip("/")
    return Response(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
                    mimetype="text/plain")


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
        "stripe_mode": STRIPE_MODE,
    })


@app.get("/api/products")
def api_products():
    # wholesale_eligible is additive metadata for the storefront's partner
    # pricing display; retail guests ignore it. Dark-staged rows (listed=0)
    # never appear on public surfaces.
    return jsonify([
        {**p, "wholesale_eligible": wholesale_mod.is_wholesale_eligible(p)}
        for p in PRODUCTS if p.get("listed", True)
    ])


@app.get("/api/products/<sku>")
def api_product(sku):
    p = BY_SKU.get(sku)
    if not p or not p.get("listed", True):
        return jsonify({"error": "not found"}), 404
    return jsonify(p)


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
    """20%-off partner pricing; rejects anything outside the wholesale program
    (only unpurchasable products — no Printful fulfillment — are excluded)."""
    if not wholesale_mod.is_wholesale_eligible(p):
        return None, (f"{p['sku']} is not in the wholesale program "
                      f"(no Printful fulfillment available for it)")
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
        # Server-side minimums: 10+ units per base product, every order;
        # opening order additionally 50+ units or $500+ merchandise.
        wl_lines = [{"type": BY_SKU[l["sku"]]["type"], "qty": l["qty"],
                     "unit_cents": l["unit_cents"]} for l in lines]
        min_errors = wholesale_mod.check_wholesale_minimums(
            wl_lines, is_first_order=not partner["has_ordered"])
        if min_errors:
            return jsonify({"error": "; ".join(min_errors)}), 400
        ship_cents = wholesale_mod.bulk_shipping_cents(wl_lines)
    # Fulfillment pre-check: every PRINT line must have COMPLETE Printful
    # mapping (non-null values, not just keys) — the same rule as the
    # purchasability gate (catalog.mapping_complete). A customer must never
    # pay for something we cannot ship. build_order_items() keeps its strict
    # completeness check for actual fulfillment in fulfill_paid_order(), so
    # a paid order with unfilled entries fails loudly there instead of
    # shipping nothing silently. Digital lines skip this gate: their
    # purchasability already required a deliverable file at catalog load.
    mapping = load_mapping(MAPPING_PATH)
    unmapped = []
    for l in lines:
        if _is_digital(l["sku"]):
            continue
        if not mapping_complete(l["sku"], BY_SKU[l["sku"]]["type"], mapping):
            unmapped.append(l["sku"])
    if unmapped:
        return jsonify({"error": "No Printful mapping for: " + ", ".join(unmapped)}), 409

    # FOUNDER100 (Bill's standing test method): an API-level coupon that
    # brings the order total to exactly $0.50 for the test charge. Passed as
    # {coupon:"FOUNDER100"}; never advertised in the storefront UI. DISABLED
    # in live mode — it must never discount a real-money order.
    coupon = ((data or {}).get("coupon") or "").strip().upper()
    discounts = None
    if coupon == "FOUNDER100":
        if STRIPE_MODE == "live":
            return jsonify({"error": "FOUNDER100 is a test-mode coupon."}), 400
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
                                     + (f" ({l['size']})" if l["size"] else ""),
                             "tax_code": DIGITAL_TAX_CODE if _is_digital(l["sku"])
                             else PHYSICAL_TAX_CODE},
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
                "product_data": {"name": "Bulk shipping (estimate) — partner rate",
                                 "tax_code": SHIPPING_TAX_CODE},
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
        success_url=base + "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=base + "/checkout/cancel",
        **_stripe_acct(),
    )
    # Shipping is collected only when the cart has print goods — a
    # digital-only buyer has nothing to ship and must not be forced
    # through an address form (fulfillment is the emailed download link).
    if any(not _is_digital(l["sku"]) for l in lines):
        create_kwargs["shipping_address_collection"] = \
            {"allowed_countries": ["US"]}
    if discounts:
        create_kwargs["discounts"] = discounts
    # Stripe Tax: calculate/collect automatically (live mode only — in test
    # mode the flag stays off so test checkout can never fail on Tax
    # activation state; the per-line tax_code above is harmless either way).
    # tax_behavior is left unset: prices resolve through the account default
    # (USD resolves exclusive — tax added on top of the sticker price).
    if STRIPE_MODE == "live":
        create_kwargs["automatic_tax"] = {"enabled": True}
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
    # stripe-python 15.x: session fields are StripeObjects, not dicts —
    # .get() raises AttributeError and missing-key attribute access raises
    # AttributeError (2026-10-01: crashed /api/fulfill into an HTML 500 on a
    # live paid order). _sget() is the only safe read pattern here.
    meta = _sget(session, "metadata", {}) or {}
    cart_raw = _sget(meta, "cart", "[]") or "[]"
    try:
        cart = json.loads(cart_raw)
    except (ValueError, TypeError):
        return jsonify({"error": "unreadable cart metadata on session"}), 400
    ship = _sget(session, "shipping_details", {}) or {}
    if not _sget(ship, "address"):
        # Modern Checkout (Link) puts shipping under collected_information.
        ci = _sget(session, "collected_information", {}) or {}
        ship = _sget(ci, "shipping_details", {}) or ship
    cust = _sget(session, "customer_details", {}) or {}
    addr = _sget(ship, "address", {}) or {}
    shipping_address = {
        "name": _sget(ship, "name", "") or "",
        "line1": _sget(addr, "line1", "") or "",
        "line2": _sget(addr, "line2", "") or "",
        "city": _sget(addr, "city", "") or "",
        "state": _sget(addr, "state", "") or "",
        "country": _sget(addr, "country", "US") or "US",
        "postal_code": _sget(addr, "postal_code", "") or "",
    }
    if not shipping_address["line1"] or not shipping_address["city"]:
        print_lines, _digital_lines = split_cart_lines(cart, BY_SKU)
        if print_lines:
            log.warning("fulfill: session %s paid but has no shipping address",
                        session.id)
            return jsonify({"error": "no shipping address on this checkout "
                                     "session — contact support with your order "
                                     "email"}), 422
        # Digital-only order: nothing ships, so no address is required —
        # delivery is the emailed download link.
    try:
        result = fulfill_paid_order(
            stripe_session_id=session.id,
            customer_email=_sget(cust, "email", "") or "",
            shipping_address=shipping_address,
            cart_lines=cart, products_by_sku=BY_SKU, mapping_path=MAPPING_PATH,
            order_prefix=ORDER_PREFIX,
            download_base_url=_public_base_url(),
            store_name=brand["brand"]["name"],
        )
    except (PrintfulConfigError, digital_mod.DigitalConfigError) as e:
        return jsonify({"error": str(e)}), 503
    except (PrintfulAPIError, ValueError, digital_mod.DigitalSendError) as e:
        return jsonify({"error": str(e)}), 502
    if isinstance(result, dict) and "digital" in result:
        po = result.get("printful") or {}
        return jsonify({"printful_order_id": po.get("id"),
                        "status": po.get("status"),
                        "dry_run": po.get("dry_run", False),
                        "digital": result.get("digital")})
    if isinstance(result, dict) and "everready_drive" in result:
        po = result.get("printful") or {}
        return jsonify({"printful_order_id": po.get("id"),
                        "status": po.get("status"),
                        "dry_run": po.get("dry_run", False),
                        "everready_drive": result.get("everready_drive")})
    return jsonify({"printful_order_id": result.get("id"), "status": result.get("status"),
                    "dry_run": result.get("dry_run", False)})


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
    if _sget(event, "type") == "checkout.session.completed":
        session = _sget(event, "data", {}) or {}
        session = _sget(session, "object", {}) or {}
        meta = _sget(session, "metadata", {}) or {}
        if _sget(meta, "kind") == "sr_sub":
            # SportRoots subscription checkout (subscriptions.py Lane 4):
            # record the entitlement; there is no cart to fulfill. No-op
            # unless SR_BILLING_ENABLED is on (handle_event checks).
            try:
                sr = sr_mod.handle_event(event, _sget)
            except Exception:  # noqa: BLE001 — webhook must not crash; alert instead
                log.exception("sportroots entitlement record failed for %s",
                              _sget(session, "id", "?"))
                return jsonify({"error": "sportroots entitlement failed"}), 500
            return jsonify({"received": True, "sportroots": sr})
        if _sget(meta, "kind") == "auction_pay":
            # Auction winner pay page (auctions.py Slice 3): the handler
            # marks invoice + lot PAID idempotently. Signature already
            # verified above; no cart fulfillment runs for these.
            try:
                result = auctions_mod.handle_checkout_completed(session)
            except auctions_mod.AuctionError as e:
                log.warning("auction payment handling failed: %s", e)
                return jsonify({"error": str(e)}), 400
            return jsonify({"received": True, "auction": result})
        try:
            cart = json.loads(_sget(meta, "cart", "[]") or "[]")
        except (ValueError, TypeError):
            return jsonify({"error": "unreadable cart metadata"}), 400
        ship_obj = _sget(session, "shipping_details", {}) or {}
        if not _sget(ship_obj, "address"):
            ci = _sget(session, "collected_information", {}) or {}
            ship_obj = _sget(ci, "shipping_details", {}) or ship_obj
        addr = _sget(ship_obj, "address", {}) or {}
        try:
            fulfill_paid_order(
                stripe_session_id=_sget(session, "id", ""),
                customer_email=_sget(_sget(session, "customer_details", {}) or {}, "email", "") or "",
                shipping_address={
                    "name": _sget(ship_obj, "name", "") or "",
                    "line1": _sget(addr, "line1", "") or "", "line2": _sget(addr, "line2", "") or "",
                    "city": _sget(addr, "city", "") or "", "state": _sget(addr, "state", "") or "",
                    "country": _sget(addr, "country", "US") or "US",
                    "postal_code": _sget(addr, "postal_code", "") or "",
                },
                cart_lines=cart, products_by_sku=BY_SKU, mapping_path=MAPPING_PATH,
                order_prefix=ORDER_PREFIX,
                download_base_url=_public_base_url(),
                store_name=brand["brand"]["name"],
            )
        except Exception as e:  # noqa: BLE001 — webhook must not crash; alert instead
            log.exception("fulfillment failed for %s", _sget(session, "id", "?"))
            return jsonify({"error": str(e)}), 500
    if _sget(event, "type") in ("invoice.paid",
                                "customer.subscription.updated",
                                "customer.subscription.deleted"):
        # SportRoots billing lifecycle: keep the entitlement store
        # (subscriptions.py) current. handle_event returns None when
        # SR_BILLING_ENABLED is off — the webhook then just acks.
        try:
            sr = sr_mod.handle_event(event, _sget)
        except Exception:  # noqa: BLE001 — webhook must not crash; alert instead
            log.exception("sportroots billing event failed: %s",
                          _sget(event, "type", "?"))
            return jsonify({"error": "sportroots billing failed"}), 500
        if sr is not None:
            return jsonify({"received": True, "sportroots": sr})
    if _sget(event, "type") == "charge.refunded":
        # Refund runbook (fulfillment/README.md): refunds are issued by a
        # human in the Stripe dashboard. Here we only LOG and flag the
        # digital ledger — a delivered download cannot be recalled, and no
        # code path pretends otherwise. The checkout session id rides on
        # charge metadata when present; without it we log the charge id so
        # the refund can be matched by hand.
        charge = _sget(_sget(event, "data", {}) or {}, "object", {}) or {}
        meta = _sget(charge, "metadata", {}) or {}
        sid = _sget(meta, "checkout_session_id", "") or ""
        flagged = False
        if sid:
            try:
                flagged = digital_mod.DigitalLedger().mark_refunded(sid)
            except Exception:  # noqa: BLE001 — logging must not crash
                log.exception("refund ledger flag failed for %s", sid)
        log.warning(
            "charge.refunded: charge=%s amount_refunded=%s session=%s "
            "ledger_flagged=%s — any download links already delivered stay "
            "valid until expiry (cannot be recalled)",
            _sget(charge, "id", "?"), _sget(charge, "amount_refunded", "?"),
            sid or "(not on charge metadata)", flagged)
        # SportRoots: a refunded subscription charge revokes entitlement
        # (subscriptions.py). No-op when SR_BILLING_ENABLED is off.
        if sr_mod.is_enabled():
            try:
                sr_mod.handle_charge_refunded(charge, _sget)
            except Exception:  # noqa: BLE001 — logging must not crash
                log.exception("sportroots refund handling failed")
    return jsonify({"received": True})


# ---------- digital delivery (SkillForge pilot, 2026-10-02) ----------
# Digital SKUs (catalog fulfillment_type=digital) are delivered by emailed
# signed links (fulfillment/digital.py) and served by /download below from
# DIGITAL_FILES_DIR (default data/digital under the project root). Link
# targets must be built on the PUBLIC host: PUBLIC_BASE_URL overrides the
# request host when the service sits behind a proxy/custom domain.

def _digital_files_dir():
    return os.environ.get("DIGITAL_FILES_DIR", os.path.join(ROOT, "data", "digital"))


def _public_base_url():
    return (os.environ.get("PUBLIC_BASE_URL") or request.host_url).rstrip("/")


# ---------- SportRoots subscription billing (Lane 4, 2026-10-03) ----------
# Recurring Billing + Customer Portal + webhook-kept entitlement store
# (backend/subscriptions.py). DARK unless SR_BILLING_ENABLED=1: with the
# flag off no /api/sr/* routes exist and the webhook below passes
# subscription events through untouched. sr_mod.init registers the
# routes; the webhook branches call sr_mod.handle_event / handle_charge_
# refunded, which no-op when disabled.
sr_mod.init(app, stripe_ready=STRIPE_READY, stripe_mode=STRIPE_MODE,
            stripe_acct=_stripe_acct, public_base_url=_public_base_url)


def _is_digital(sku):
    return ((BY_SKU.get(sku) or {}).get("fulfillment_type")
            or "print").strip().lower() == "digital"


@app.get("/download/<token>")
def download_file(token):
    """Serve a digital product file to whoever holds a valid signed token.

    The token (minted at fulfillment, emailed to the buyer) binds sku +
    buyer email + expiry. Never log the token itself."""
    try:
        signer = digital_mod.DownloadTokenSigner()
    except digital_mod.DigitalConfigError as e:
        return jsonify({"error": str(e)}), 503
    try:
        payload = signer.verify(token)
    except ValueError as e:
        reason = str(e)
        status = 410 if reason == "expired" else 403
        return jsonify({"error": f"download link {reason}"}), status
    sku = payload.get("sku", "")
    product = BY_SKU.get(sku)
    if (not product or not _is_digital(sku)
            or not product.get("digital_file")):
        return jsonify({"error": "not found"}), 404
    # Byte source behind the token check (fulfillment/storage.py): local
    # disk by default; private S3-compatible object storage when the
    # catalog outgrows git (DIGITAL_STORAGE_BACKEND=s3). Token behavior is
    # identical either way — only the byte fetch changes.
    try:
        backend = storage_mod.get_storage(_digital_files_dir())
    except storage_mod.StorageConfigError as e:
        return jsonify({"error": str(e)}), 503
    # Basename only — a catalog value can never traverse out of the files
    # dir or address an arbitrary storage key.
    filename = os.path.basename(product["digital_file"])
    if backend.is_local:
        files_dir = os.path.realpath(_digital_files_dir())
        # The resolved path must sit directly in the files dir.
        if os.path.dirname(os.path.realpath(
                os.path.join(files_dir, filename))) != files_dir \
                or not os.path.isfile(os.path.join(files_dir, filename)):
            log.warning("download: digital file missing for sku %s", sku)
            return jsonify({"error": "file not available — contact support"}), 404
        log.info("download: sku %s served", sku)
        return send_from_directory(files_dir, filename, as_attachment=True)
    try:
        chunks, size, content_type = backend.open(filename)
    except storage_mod.StorageNotFound:
        log.warning("download: digital object missing in storage for sku %s", sku)
        return jsonify({"error": "file not available — contact support"}), 404
    except storage_mod.StorageError as e:
        log.warning("download: storage fetch failed for sku %s: %s", sku, e)
        return jsonify({"error": "file temporarily unavailable — contact support"}), 502
    log.info("download: sku %s served (object storage)", sku)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if size is not None:
        headers["Content-Length"] = str(size)
    return Response(stream_with_context(chunks),
                    content_type=content_type, headers=headers)


@app.get("/api/printful/mapping-status")
def mapping_status():
    mapping = load_mapping(MAPPING_PATH)
    missing = []
    for p in PRODUCTS:
        if not p["purchasable"]:
            continue
        if (p.get("fulfillment_type") or "print") == "digital":
            continue  # digital SKUs need no Printful mapping
        if not mapping_complete(p["sku"], p["type"], mapping):
            missing.append(p["sku"])
    return jsonify({"mapped": len(mapping), "unmapped_keys": missing})


if __name__ == "__main__":
    log.info("brand=%s products=%d purchasable=%d stripe_ready=%s stripe_connect=%s",
             BRAND_ID, len(PRODUCTS),
             sum(1 for p in PRODUCTS if p["purchasable"]), STRIPE_READY,
             STRIPE_CONNECT_ACCOUNT_ID or "platform")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8091)))
