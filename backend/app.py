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
import re
from html import escape as _html_escape

import stripe
import yaml
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from catalog import load_unified_catalog, catalog_stats, APPAREL_SIZES, mapping_keys_for, mapping_complete, OWNERSHIP
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
import cookbook as cookbook_mod

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

# Per-brand display names for Stripe Checkout line items (2026-10-07 fix):
# this process serves the unified catalog behind every brand face, so a
# line item's seller name must come from the PRODUCT's owning brand
# (catalog.OWNERSHIP, carried on each product as p["owner"]) — never from
# this process's own BRAND_ID. Before this fix every line rendered as
# "<serving process brand> — <title>", so on the SkillForge process all
# RestorationEssentials/IronHead/Stitchfolk/EverReady products checked
# out as "SkillForge AI — …" (2026-10-06 buy-button audit: 730/1,370
# products misattributed). The account-level merchant display name is
# account configuration and is deliberately not touched here.
BRAND_DISPLAY_NAMES = {}
for _owner_id in sorted(set(OWNERSHIP.values())):
    try:
        BRAND_DISPLAY_NAMES[_owner_id] = \
            load_brand(_owner_id)["brand"]["name"]
    except Exception as exc:  # a bad brand file must never take the store down
        log.warning("brand display name %s not loaded: %s", _owner_id, exc)
BRAND_DISPLAY_NAMES.setdefault(brand["brand"]["id"], brand["brand"]["name"])


def _checkout_brand_name(line):
    """Checkout display name of the brand that owns a validated cart line."""
    return BRAND_DISPLAY_NAMES.get(line.get("owner")) \
        or brand["brand"]["name"]

# Host-mapped storefront faces (2026-10-04): restoreessentials.com is
# attached to this service, but its customers must see the Restoration
# Essentials brand over RE-owned products only — never this process's own
# brand over the whole unified catalog. A face changes ONLY /api/brand
# and /api/products; the catalog load, checkout, and fulfillment stay
# process-wide and identical on every host. A face config that fails to
# load is skipped with a warning — it must never take the store down.
HOST_FACES = {}
for _host, _brand_id in (("restoreessentials.com", "restorationessentials"),
                         ("www.restoreessentials.com", "restorationessentials"),
                         ("everreadyfamily.com", "everready"),
                         ("www.everreadyfamily.com", "everready"),
                         ("everreadyfamily.co", "everready"),
                         ("www.everreadyfamily.co", "everready"),
                         ("everready-family.com", "everready"),
                         ("www.everready-family.com", "everready"),
                         ("stitchfolkpatterns.com", "stitchfolk"),
                         ("www.stitchfolkpatterns.com", "stitchfolk"),
                         ("sportroots.coach", "sportroots"),
                         ("www.sportroots.coach", "sportroots"),
                         ("sportrootsdrills.com", "sportroots"),
                         ("www.sportrootsdrills.com", "sportroots"),
                         ("ironheadguides.com", "ironhead"),
                         ("www.ironheadguides.com", "ironhead"),
                         ("skillforge.co", "skillforge"),
                         ("www.skillforge.co", "skillforge"),
                         ("skillforgeai.co", "skillforge"),
                         ("www.skillforgeai.co", "skillforge"),
                         ("skillforgeaihub.com", "skillforge"),
                         ("www.skillforgeaihub.com", "skillforge")):
    try:
        HOST_FACES[_host] = load_brand(_brand_id)
    except Exception as exc:
        log.warning("host face %s (%s) not loaded: %s", _host, _brand_id, exc)

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
    if not stripe_key.startswith(("sk_live_", "rk_live_")):
        raise RuntimeError(
            "REFUSED: STRIPE_MODE=live but STRIPE_LIVE_SECRET_KEY is missing "
            "or not a live key (must start with sk_live_ or rk_live_).")
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


# ---------- host-face content pages ----------
# Static content served only on its own host face; every other host
# keeps the exact pre-face behavior (these paths 404 there). The buy
# buttons on these pages point at the shared /product/<SKU> routes.
# One registry serves both faces: a path shared by two faces (e.g.
# /faq) is registered once and dispatched to the requesting host's
# face — behavior on each face is unchanged.
FACE_PAGES = {
    "everready": {
        "dir": "everready",
        "home": "home.html",
        "pages": {
            "/products": "products.html",
            "/faq": "faq.html",
            "/founder": "founder.html",
            "/checklist": "checklist.html",
            "/guides/executors-first-30-days": "guide-executors-first-30-days.html",
            "/guides/five-conversations-before-you-need-them": "guide-five-conversations.html",
            "/guides/paperwork-after-someone-dies": "guide-paperwork-after-someone-dies.html",
            "/products/life-story-interview": "product-life-story.html",
            "/products/family-recipe-cookbook": "product-family-recipe-cookbook.html",
            "/products/family-command-center": "product-family-command-center.html",
            "/products/executors-kit": "product-executors-kit.html",
            "/contact": "contact.html",
        },
    },
    "stitchfolk": {
        "dir": "stitchfolk",
        "home": "home.html",
        "pages": {
            "/patterns": "patterns.html",
            "/free-pattern": "free-pattern.html",
            "/crochet": "crochet.html",
            "/knitting": "knitting.html",
            "/needlepoint": "needlepoint.html",
            "/learn": "learn.html",
            "/learn/cast-on": "learn-cast-on.html",
            "/learn/picking-needles": "learn-picking-needles.html",
            "/learn/reading-knitting-abbreviations": "learn-reading-knitting-abbreviations.html",
            "/learn/which-pattern": "learn-which-pattern.html",
            "/about": "about.html",
            "/faq": "faq.html",
            "/policies": "policies.html",
            "/policies/privacy": "policy-privacy.html",
            "/policies/terms": "policy-terms.html",
            "/policies/digital-downloads-refunds": "policy-digital-downloads-refunds.html",
            "/policies/cookies": "policy-cookies.html",
            "/policies/affiliate-disclosure": "policy-affiliate-disclosure.html",
            "/policies/contributor-terms": "policy-contributor-terms.html",
            "/policies/copyright-takedown": "policy-copyright-takedown.html",
            "/contact": "contact.html",
            "/testimonials": "testimonials.html",
            "/gallery": "gallery.html",
            "/new-arrivals": "new-arrivals.html",
            "/designer-patterns": "designer-patterns.html",
        },
    },
    "sportroots": {
        "dir": "sportroots",
        "home": "home.html",
        "pages": {
            "/sports": "sports.html",
            "/drills": "drills.html",
            "/drills/baseball": "drills-baseball.html",
            "/drills/basketball": "drills-basketball.html",
            "/drills/cheerleading": "drills-cheerleading.html",
            "/drills/football": "drills-football.html",
            "/drills/golf": "drills-golf.html",
            "/drills/gymnastics": "drills-gymnastics.html",
            "/drills/hockey": "drills-hockey.html",
            "/drills/lacrosse": "drills-lacrosse.html",
            "/drills/martial-arts": "drills-martial-arts.html",
            "/drills/skiing": "drills-skiing.html",
            "/drills/soccer": "drills-soccer.html",
            "/drills/softball": "drills-softball.html",
            "/drills/swimming": "drills-swimming.html",
            "/drills/tennis": "drills-tennis.html",
            "/drills/track": "drills-track.html",
            "/drills/volleyball": "drills-volleyball.html",
            "/drills/weight-training": "drills-weight-training.html",
            "/drills/wrestling": "drills-wrestling.html",
            "/coaches": "coaches.html",
            "/book": "book.html",
            "/pricing": "pricing.html",
            "/faq": "faq.html",
        },
    },
    "ironhead": {
        "dir": "ironhead",
        "home": "home.html",
        "pages": {
            "/guides": "guides.html",
            "/guides/harley-sportster": "shelf-sportster.html",
            "/guides/harley-shovelhead": "shelf-shovelhead.html",
            "/guides/harley-panhead": "shelf-panhead.html",
            "/guides/indian-chief": "shelf-indian-chief.html",
            "/guides/indian-scout": "shelf-indian-scout.html",
            "/guides/triumph-bonneville": "shelf-bonneville.html",
            "/guides/triumph-preunit": "shelf-preunit.html",
            "/guides/bmw-slash7": "shelf-bmw.html",
            "/guides/bsa": "shelf-bsa.html",
            "/guides/honda": "shelf-honda.html",
            "/guides/yamaha-xs650": "shelf-yamaha.html",
            "/guides/restoration-bundles": "shelf-bundles.html",
            "/pricing": "pricing.html",
            "/faq": "faq.html",
            "/about": "about.html",
            "/testimonials": "testimonials.html",
            "/blog": "blog.html",
            "/blog/ironhead-sportster-buying-guide": "blog-sportster.html",
            "/blog/ironhead-bmw-airhead-buying-guide": "blog-bmw-airhead.html",
            "/blog/ironhead-honda-cb750-buying-guide": "blog-honda-cb750.html",
            "/legal": "legal.html",
            "/legal/disclosure-summary": "legal-disclosure-summary.html",
            "/legal/terms": "legal-terms.html",
            "/legal/auction-rules": "legal-auction-rules.html",
            "/legal/seller-agreement": "legal-seller-agreement.html",
            "/legal/buyer-disclosures": "legal-buyer-disclosures.html",
            "/legal/refunds-cancellations": "legal-refunds-cancellations.html",
            "/legal/dispute-resolution": "legal-dispute-resolution.html",
            "/legal/guides-disclaimer": "legal-guides-disclaimer.html",
            "/contact": "contact.html",
            "/privacy": "privacy.html",
            "/workshop": "workshop.html",
            "/community": "community.html",
        },
    },
    "skillforge": {
        "dir": "skillforge",
        "home": "home.html",
        "pages": {
            "/playbooks": "playbooks.html",
            "/about": "about.html",
            "/faq": "faq.html",
            "/contact": "contact.html",
        },
    },
    # RestorationEssentials face (2026-10-07 — Bill: restoreessentials
    # .com must land on the GUIDE landing page, not the shared PushRod
    # merch shop shell + merch shelf the host used to fall back to):
    # the face home is guides-first (hero + guide showcase fed by the
    # face-filtered /api/products), the auctions blueprint carries
    # /auctions, and the PushRod merch stays a secondary cross-sell
    # section on the home that links out to pushrodshop.com — the same
    # links-only treatment the guide pages already get via
    # _inject_re_guide_shelf.
    "restorationessentials": {
        "dir": "restorationessentials",
        "home": "home.html",
        "pages": {
            "/guides": "guides.html",
            "/about": "about.html",
            "/contact": "contact.html",
        },
    },
}

ER_PAGES = FACE_PAGES["everready"]["pages"]
ST_PAGES = FACE_PAGES["stitchfolk"]["pages"]
SR_PAGES = FACE_PAGES["sportroots"]["pages"]
IH_PAGES = FACE_PAGES["ironhead"]["pages"]
SF_PAGES = FACE_PAGES["skillforge"]["pages"]
SKILLFORGE_FACE_HOSTS = {"skillforge.co", "www.skillforge.co",
                         "skillforgeai.co", "www.skillforgeai.co",
                         "skillforgeaihub.com", "www.skillforgeaihub.com"}


def _skillforge_self_canonical(resp):
    """Point a SkillForge face page's canonical at the serving host.

    The same static face serves the candidate domains (skillforge.co
    and skillforgeai.co, inert and not owned; and skillforgeaihub.com,
    picked by Bill 2026-10-05 -- "Hub" covers playbooks, forms, and
    whatever comes next -- inert until it registers and attaches);
    the files carry a skillforge.co canonical placeholder and this
    rewrite makes each host canonical to itself, per the SEO pattern.
    Only SkillForge face responses pass through here.
    """
    host = _request_host_key()
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    html_text = re.sub(
        r'(<link rel="canonical" href="https://)'
        r'(?:skillforgeaihub\.com|skillforge\.co|skillforgeai\.co)',
        lambda m: m.group(1) + host, html_text, count=1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


def _everready_self_canonical(resp):
    """Point an EverReady face page's canonical at the serving host.

    The same static face serves everreadyfamily.co and everready-family.com
    (the hyphenated .com is the domain Bill is registering; the .co stays
    routed); the files carry an everreadyfamily.co canonical placeholder
    and this rewrite makes each host canonical to itself, mirroring the
    SkillForge dual-host pattern above. Only EverReady face responses
    pass through here.
    """
    host = _request_host_key()
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    html_text = re.sub(
        r'(<link rel="canonical" href="https://)'
        r'(?:everreadyfamily\.com|everready-family\.com|everreadyfamily\.co)'
        r'(?=[/"])',
        lambda m: m.group(1) + host, html_text, count=1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


def _face_page(path):
    cfg, _products = _face()
    spec = FACE_PAGES.get(cfg["brand"]["id"])
    if not spec or path not in spec["pages"]:
        return "Not found", 404
    if cfg["brand"]["id"] == "skillforge":
        # SkillForge is also this process's own brand: without the host
        # gate its face pages would leak onto the default onrender host
        # and change long-standing routes there. Face hosts only.
        if _request_host_key() not in SKILLFORGE_FACE_HOSTS:
            return "Not found", 404
        resp = send_from_directory(os.path.join(FRONTEND, spec["dir"]),
                                   spec["pages"][path])
        return _skillforge_self_canonical(resp)
    if cfg["brand"]["id"] == "everready":
        # One static face, two routed hosts (everreadyfamily.co and
        # everready-family.com): each host canonical to itself.
        resp = send_from_directory(os.path.join(FRONTEND, spec["dir"]),
                                   spec["pages"][path])
        return _everready_self_canonical(resp)
    return send_from_directory(os.path.join(FRONTEND, spec["dir"]),
                               spec["pages"][path])


for _path in sorted({p for _s in FACE_PAGES.values() for p in _s["pages"]}):
    app.add_url_rule(_path, endpoint="face" + _path.replace("/", "_"),
                     view_func=lambda p=_path: _face_page(p))


# ---------- brand sitemap builders ----------
# Which sitemap file each face brand's host declares. The shared
# /sitemap.xml dispatch and robots.txt below read this same map, so a
# face host's declared sitemap is always the one that lists its own
# brand's pages (the /sitemap-<brand>.xml routes keep serving the same
# bodies: a GSC property submitted against either URL stays valid).
BRAND_SITEMAP_FILES = {
    "everready": "sitemap-er.xml",
    "stitchfolk": "sitemap-st.xml",
    "sportroots": "sitemap-sr.xml",
    "ironhead": "sitemap-ih.xml",
    "skillforge": "sitemap-sf.xml",
}


def _brand_sitemap_paths(bid, products):
    """Sitemap path list for one face brand, or None when the brand has
    no face sitemap (restorationessentials, gateway, pushrod: the
    default /sitemap.xml below already lists exactly their products).
    Pure builder: the face-only host guards stay in the routes."""
    if bid == "everready":
        return ["/"] + sorted(ER_PAGES) + ["/product/ER-FCC-001",
                                           "/product/ER-EK-001",
                                           "/product/ER-FBK-001",
                                           "/product/ER-FRB-001",
                                           "/product/ER-DAI-001",
                                           "/product/ER-LSIK-001",
                                           "/product/ER-FRC-001"]
    if bid == "stitchfolk":
        return ["/"] + sorted(ST_PAGES) + [
            f"/product/{p['sku']}" for p in products
            if p.get("listed", True) and p["purchasable"]]
    if bid == "sportroots":
        return ["/"] + sorted(SR_PAGES)
    if bid == "ironhead":
        return ["/"] + sorted(IH_PAGES) + [
            f"/product/{p['sku']}" for p in products
            if p.get("listed", True) and p["purchasable"]]
    if bid == "skillforge":
        return ["/"] + sorted(SF_PAGES) + [
            f"/product/{p['sku']}" for p in products
            if p.get("listed", True) and p["purchasable"]]
    return None


def _sitemap_response(paths):
    base = request.host_url.rstrip("/")
    urls = "\n".join(f"  <url><loc>{base}{u}</loc></url>" for u in paths)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{urls}\n</urlset>\n")
    return Response(xml, mimetype="application/xml")


# ---------- EverReady sitemap (face only) ----------
@app.get("/sitemap-er.xml")
def sitemap_er_xml():
    # The EverReady content pages get their own sitemap here for
    # whoever ends up submitting the EverReady domain; on the EverReady
    # host the shared /sitemap.xml serves this same body (see the
    # dispatch below).
    cfg, _products = _face()
    if cfg["brand"]["id"] != "everready":
        return "Not found", 404
    return _sitemap_response(_brand_sitemap_paths("everready", _products))


# ---------- Stitchfolk sitemap (face only) ----------
@app.get("/sitemap-st.xml")
def sitemap_st_xml():
    # Same pattern as /sitemap-er.xml: the Stitchfolk content pages and
    # live pattern pages get their own sitemap for whoever submits the
    # Stitchfolk domain; on the Stitchfolk host the shared /sitemap.xml
    # serves this same body (see the dispatch below).
    cfg, products = _face()
    if cfg["brand"]["id"] != "stitchfolk":
        return "Not found", 404
    return _sitemap_response(_brand_sitemap_paths("stitchfolk", products))


# ---------- SportRoots sitemap (face only) ----------
@app.get("/sitemap-sr.xml")
def sitemap_sr_xml():
    # Same pattern as /sitemap-er.xml and /sitemap-st.xml: the
    # SportRoots content pages (home, sports index, the drill library
    # and its per-sport views, coaches, booking-request, pricing, faq)
    # get their own sitemap for whoever submits the SportRoots domain;
    # on the SportRoots host the shared /sitemap.xml serves this same
    # body (see the dispatch below). SR sells subscriptions, not SKUs:
    # no /product/<SKU> URLs here.
    cfg, _products = _face()
    if cfg["brand"]["id"] != "sportroots":
        return "Not found", 404
    return _sitemap_response(_brand_sitemap_paths("sportroots", _products))


# ---------- IronHead sitemap (face only) ----------
@app.get("/sitemap-ih.xml")
def sitemap_ih_xml():
    # Same pattern as /sitemap-er.xml, /sitemap-st.xml and
    # /sitemap-sr.xml: the IronHead content pages and live guide pages
    # get their own sitemap for whoever submits the IronHead domain;
    # on the IronHead host the shared /sitemap.xml serves this same
    # body (see the dispatch below).
    cfg, products = _face()
    if cfg["brand"]["id"] != "ironhead":
        return "Not found", 404
    return _sitemap_response(_brand_sitemap_paths("ironhead", products))


# ---------- SkillForge sitemap (face only) ----------
@app.get("/sitemap-sf.xml")
def sitemap_sf_xml():
    # Same pattern as /sitemap-er.xml, /sitemap-st.xml, /sitemap-sr.xml
    # and /sitemap-ih.xml: the SkillForge content pages and live
    # playbook pages get their own sitemap for whoever submits the
    # SkillForge domain. This is the first sitemap anywhere that lists
    # SF SKUs (the GSC plan's finding 4). SkillForge is also the
    # process brand, so the host gate below is what keeps this
    # face-only: the default onrender host 404s here like every other
    # face sitemap. On the face hosts the shared /sitemap.xml serves
    # this same body (see the dispatch below).
    cfg, products = _face()
    if (cfg["brand"]["id"] != "skillforge"
            or _request_host_key() not in SKILLFORGE_FACE_HOSTS):
        return "Not found", 404
    return _sitemap_response(_brand_sitemap_paths("skillforge", products))

# ---------- face-host SEO head (server-side) ----------
# The shared storefront shells (frontend/index.html, frontend/product.html)
# carry PUSHROD titles, no meta description, and no canonical in their raw
# bytes — JS rebrands after load, so a non-rendering crawler reads the
# wrong brand on a face domain (GSC finding: restoreessentials.com raw
# HTML served <title>PUSHROD™</title>). On host-mapped face hosts only,
# rewrite the served shell's head: the face brand's title, a real meta
# description, and a self-host canonical for the exact URL requested.
# Responses on every other host are returned untouched (byte-identical
# to the pre-face behavior), and the face content pages (FACE_PAGES)
# carry their canonicals baked into their static files.
FACE_HOME_HEAD = {
    "restorationessentials": (
        "Restoration Essentials | Restoration guides for muscle cars & classic trucks",
        "Restoration Essentials — Restoration guides for muscle cars "
        "& classic trucks. 385 guides for 1947–1973 American classics."),
}
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)


def _request_host_key():
    return (request.host or "").split(":")[0].strip().lower()


def _face_head(resp, title=None, description=None, canonical=None):
    """Rewrite a shared-shell response's <head> for a face host (above).
    The canonical defaults to this request's own URL on the serving
    host (pass canonical= to pin another host's URL, e.g. the PushRod
    product pages below); title/description are applied only when
    supplied. Caller guarantees the request host is in HOST_FACES."""
    canonical = canonical or f"https://{_request_host_key()}{request.path}"
    resp.direct_passthrough = False  # send_from_directory streams; buffer it
    html_text = resp.get_data(as_text=True)
    if title:
        html_text = _TITLE_RE.sub(
            lambda _m: f"<title>{_html_escape(title)}</title>",
            html_text, count=1)
    if description and 'name="description"' not in html_text:
        html_text = html_text.replace(
            "</title>",
            '</title>\n<meta name="description" '
            f'content="{_html_escape(description, quote=True)}">', 1)
    if 'rel="canonical"' not in html_text:
        html_text = html_text.replace(
            "</head>", f'<link rel="canonical" href="{canonical}">\n</head>', 1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)  # body changed; the file's ETag is stale
    return resp


# PushRod storefront hosts: pushrodshop.com (+ www) is the store's own
# public domain -- the one GSC indexes for the unified catalog. It is
# not a host face (the shared shell already speaks PushRod there); only
# its product pages get the per-product head rewrite below.
PUSHROD_STORE_HOSTS = {"pushrodshop.com", "www.pushrodshop.com"}


def _inject_home_h1(resp, name):
    """Seed the shared store shell with a server-rendered <h1>.

    frontend/index.html ships no <h1> in its raw bytes, so a
    non-rendering crawler reading the store home sees no heading at
    all. The <h1> is a sibling of #doors/#grid inside <main>: store.js
    fills only those two elements on hydration and never touches this
    one, so the page keeps exactly one <h1> before and after boot.
    """
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    if "<h1" in html_text or '<div class="doors" id="doors">' not in html_text:
        return resp
    html_text = html_text.replace(
        '<div class="doors" id="doors">',
        f'<h1 class="pagetitle">{_html_escape(name)}</h1>\n  '
        '<div class="doors" id="doors">', 1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


def _inject_product_h1(resp, title):
    """Seed #pdetail with a server-rendered <h1> (the product name).

    frontend/product.html ships an empty #pdetail, so the raw bytes
    carry no <h1> at all. store.js renderDetail() replaces #pdetail's
    innerHTML wholesale on hydration, so the server <h1> is replaced
    (never duplicated) after boot; it is the one a non-JS crawler sees.
    """
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    anchor = '<div class="detail" id="pdetail"></div>'
    if anchor in html_text:
        html_text = html_text.replace(
            anchor,
            f'<div class="detail" id="pdetail">'
            f'<h1>{_html_escape(title)}</h1></div>', 1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


def _face_product_chrome(resp, cfg):
    """Server-render the face brand's chrome on a product page.

    frontend/product.html is the shared shell: its header small-print
    ("GARAGE GEAR"), back link ("← Back to the garage"), and footer
    ("— garage-built gear.") are PUSHROD's. store.js swaps only the
    .brandname text after load, so the raw bytes (crawler / first
    paint) read PUSHROD on every face domain. On face hosts, rewrite
    those three strings to the face brand's name/tagline so the
    server-rendered page is the host brand on its own. Other hosts
    (incl. pushrodshop.com, where PUSHROD chrome is correct) are
    returned untouched. Caller guarantees the request host is in
    HOST_FACES.
    """
    b = cfg["brand"]
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    # The name spans too (header + footer): store.js would swap them
    # to the same value after load, but the raw bytes must not claim
    # PUSHROD on another brand's domain even before JS runs.
    html_text = html_text.replace(
        '<span class="brandname">PUSHROD</span>',
        f'<span class="brandname">{_html_escape(b["name"])}</span>')
    html_text = html_text.replace(
        "<small>GARAGE GEAR</small>",
        f"<small>{_html_escape(b['tagline'])}</small>", 1)
    html_text = html_text.replace(
        "← Back to the garage",
        f"← Back to {_html_escape(b['name'])}", 1)
    html_text = html_text.replace(
        " — garage-built gear.",
        f" — {_html_escape(b['tagline'])}.", 1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


# ---------- merch cross-sell shelves (RestorationEssentials face) ----------
# Curated lead designs from the PushRod merch catalog, linked (never
# sold) from the RestorationEssentials face: pushrodshop.com is the
# merch store and takes every merch checkout; the RE face only points
# at the exact product pages. The cards are static on purpose: the
# guide-store service serving the RE face does not load the merch
# catalog at all, so there is nothing local to render from. Every SKU,
# price, and image below was verified live against
# pushrodshop.com/api/products at build time (2026-10-05); each card
# links to a product page that 200s there today. If a design is ever
# retired on PushRod, drop its row here in the same change.
MERCH_SHOP_BASE = "https://pushrodshop.com"

# (sku, display name, kind, price, image path on pushrodshop.com)
RE_SHELF_MUSCLE = [
    ("RE-MC-H001", "Big Block Badge hat", "Hat", "$24.00", "/img/muscle/hats/re-mc-h001-big-block-badge.webp"),
    ("RE-MC-H008", "454 hat", "Hat", "$24.00", "/img/muscle/hats/re-mc-h008-454.webp"),
    ("RE-MC-H005", "Four Speed hat", "Hat", "$24.00", "/img/muscle/hats/re-mc-h005-four-speed.webp"),
    ("RE-MC-H002", "1320 hat", "Hat", "$24.00", "/img/muscle/hats/re-mc-h002-1320.webp"),
    ("RE-MC-T016", "Big Block 454 tee", "Tee", "$26.00", "/img/muscle/tees/re-mc-t016-big-block-454.webp"),
    ("RE-MC-T017", "440 tee", "Tee", "$26.00", "/img/muscle/tees/re-mc-t017-440.webp"),
    ("RE-MC-T018", "428 tee", "Tee", "$26.00", "/img/muscle/tees/re-mc-t018-428.webp"),
    ("RE-MC-T037", "Four Speed tee", "Tee", "$26.00", "/img/muscle/tees/re-mc-t037-four-speed.webp"),
    ("RE-MC-T003", "Quarter Mile tee", "Tee", "$26.00", "/img/muscle/tees/re-mc-t003-quarter-mile.webp"),
    ("RE-MC-T036", "Slicks tee", "Tee", "$26.00", "/img/muscle/tees/re-mc-t036-slicks.webp"),
    ("RE-MC-S016", "Big Block 454 pullover", "Pullover", "$44.00", "/img/muscle/tees/re-mc-t016-big-block-454.webp"),
    ("RE-MC-M001", "Big Block Coffee Mug", "Mug", "$15.00", "/img/muscle/misc/re-mc-m001-big-block-coffee-mug.webp"),
]
RE_SHELF_TRUCK = [
    ("RE-CT-H001", "Patina & Pride hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h001-patina-pride.webp"),
    ("RE-CT-H007", "Shop Truck hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h007-shop-truck.webp"),
    ("RE-CT-H026", "Half-Ton Hero hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h026-half-ton-hero.webp"),
    ("RE-CT-H013", "Long Bed Legend hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h013-long-bed-legend.webp"),
    ("RE-CT-T001", "Patina & Pride tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t001-patina-pride.webp"),
    ("RE-CT-T007", "Shop Truck tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t007-shop-truck.webp"),
    ("RE-CT-T030", "Half-Ton Hero tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t030-half-ton-hero.webp"),
    ("RE-CT-T013", "Long Bed Legend tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t013-long-bed-legend.webp"),
    ("RE-CT-T022", "Rusted But Running tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t022-rusted-but-running.webp"),
    ("RE-CT-T008", "Barn Find tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t008-barn-find.webp"),
    ("RE-CT-S001", "Patina & Pride pullover", "Pullover", "$44.00", "/img/truck/tees/re-ct-t001-patina-pride.webp"),
    ("RE-CT-M004", "Shop Truck Mug", "Mug", "$15.00", "/img/truck/misc/re-ct-m004-shop-truck-mug.webp"),
]
# The smaller strip carried on each truck guide page.
RE_SHELF_TRUCK_STRIP = [
    ("RE-CT-H001", "Patina & Pride hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h001-patina-pride.webp"),
    ("RE-CT-H007", "Shop Truck hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h007-shop-truck.webp"),
    ("RE-CT-H026", "Half-Ton Hero hat", "Hat", "$24.00", "/img/truck/hats/re-ct-h026-half-ton-hero.webp"),
    ("RE-CT-T001", "Patina & Pride tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t001-patina-pride.webp"),
    ("RE-CT-T030", "Half-Ton Hero tee", "Tee", "$26.00", "/img/truck/tees/re-ct-t030-half-ton-hero.webp"),
    ("RE-CT-M004", "Shop Truck Mug", "Mug", "$15.00", "/img/truck/misc/re-ct-m004-shop-truck-mug.webp"),
]

_MERCH_SHELF_STYLE = (
    "<style>.merchshelf{margin:2.5rem 0 1rem}"
    ".merchshelf h3{margin:1.6rem 0 .8rem}"
    ".merchshelf a.card{text-decoration:none;color:var(--text)}</style>")


def _merch_shelf_card(spec):
    sku, name, kind, price, image_path = spec
    return (f'<a class="card" href="{MERCH_SHOP_BASE}/product/{sku}">'
            f'<img src="{MERCH_SHOP_BASE}{image_path}" '
            f'alt="{_html_escape(name, quote=True)}" loading="lazy">'
            f'<span class="body"><h3>{_html_escape(name)}</h3>'
            f'<span class="meta">{_html_escape(kind)}</span>'
            f'<span class="price">{price}</span></span></a>')


def _merch_shelf_group(heading, items):
    cards = "".join(_merch_shelf_card(spec) for spec in items)
    if not cards:
        return ""
    return (f"<h3>{_html_escape(heading)}</h3>"
            f'<div class="grid">{cards}</div>')


def _merch_shelf_see_all():
    return (f'<p style="margin-top:1.2rem"><a href="{MERCH_SHOP_BASE}">'
            "See the whole shelf at PushrodShop →</a></p>")


def _inject_re_home_shelf(resp):
    """RestorationEssentials home: the 24-design merch shelf, server-
    rendered into the shared shell after the guide showcase (the
    #doors/#grid block) and before the footer — RE face host only."""
    groups = (_merch_shelf_group("From the muscle-car shelf", RE_SHELF_MUSCLE)
              + _merch_shelf_group("From the truck shelf", RE_SHELF_TRUCK))
    if not groups:
        return resp
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    if "merchshelf" in html_text or "</main>" not in html_text:
        return resp
    shelf = (_MERCH_SHELF_STYLE
             + '<section class="merchshelf"><h2>Gear for the garage</h2>'
             + "<p>Guides get the car back together. This is what you "
               "wear while you do it — printed when you order it, "
               "shipped from PushrodShop.</p>"
             + groups + _merch_shelf_see_all() + "</section>")
    html_text = html_text.replace("</main>", shelf + "\n</main>", 1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


def _re_guide_shelf_kind(sku):
    """Which shelf strip an RE guide page carries: the truck guides
    (C/K, Squarebody, K5 Blazer/Jimmy, Bronco, F-Series, El Camino)
    get the truck strip; every other RE guide gets the muscle strip."""
    if not sku.startswith("RE-GD-"):
        return None
    if ("-TRUCK-" in sku or "K5-BLAZER" in sku or "BRONCO" in sku
            or "EL-CAMINO" in sku):
        return "truck"
    return "muscle"


def _inject_re_guide_shelf(resp, sku):
    """Guide pages on the RE face carry the matching merch strip
    (truck guides -> truck shelf, other guides -> muscle-car shelf),
    server-rendered below the guide detail. Links only — checkout
    stays on pushrodshop.com."""
    kind = _re_guide_shelf_kind(sku)
    if kind is None:
        return resp
    if kind == "truck":
        shelf = _merch_shelf_group("From the truck shelf",
                                   RE_SHELF_TRUCK_STRIP)
    else:
        shelf = _merch_shelf_group("From the muscle-car shelf",
                                   RE_SHELF_MUSCLE)
    if not shelf:
        return resp
    resp.direct_passthrough = False
    html_text = resp.get_data(as_text=True)
    if "merchshelf" in html_text or "</main>" not in html_text:
        return resp
    block = (_MERCH_SHELF_STYLE + '<section class="merchshelf">'
             + shelf + _merch_shelf_see_all() + "</section>")
    html_text = html_text.replace("</main>", block + "\n</main>", 1)
    resp.set_data(html_text)
    resp.content_length = len(resp.get_data())
    resp.headers.pop("ETag", None)
    return resp


# ---------- storefront pages (static frontend) ----------
@app.get("/")
def index():
    # Host faces (EverReady, Stitchfolk): the root serves that
    # storefront's home instead of the shared product-grid shell.
    # Every other host keeps the grid.
    cfg, _products = _face()
    spec = FACE_PAGES.get(cfg["brand"]["id"])
    if spec:
        # SkillForge is the process brand too: its face home serves only
        # on the candidate face hosts, never on the default host.
        if (cfg["brand"]["id"] == "skillforge"
                and _request_host_key() not in SKILLFORGE_FACE_HOSTS):
            spec = None
        else:
            resp = send_from_directory(os.path.join(FRONTEND, spec["dir"]),
                                       spec["home"])
            if cfg["brand"]["id"] == "skillforge":
                resp = _skillforge_self_canonical(resp)
            elif cfg["brand"]["id"] == "everready":
                resp = _everready_self_canonical(resp)
            return resp
    resp = send_from_directory(FRONTEND, "index.html")
    if _request_host_key() in HOST_FACES:
        # Face host on the shared shell (RestorationEssentials): the raw
        # bytes must carry the face brand, not PUSHROD (see _face_head).
        b = cfg["brand"]
        title, desc = FACE_HOME_HEAD.get(
            b["id"], (f"{b['name']} | {b['tagline']}",
                      f"{b['name']} — {b['tagline']}."))
        resp = _face_head(resp, title=title, description=desc)
    # The shell ships no <h1>; seed one (the site name on this host).
    resp = _inject_home_h1(resp, cfg["brand"]["name"])
    if cfg["brand"]["id"] == "restorationessentials":
        # Merch cross-sell shelf (links to pushrodshop.com only).
        resp = _inject_re_home_shelf(resp)
    return resp


@app.get("/product/<sku>")
def product_page(sku):
    p = BY_SKU.get(sku)
    # Dark-staged rows (listed=0) have no public product page.
    if not p or not p.get("listed", True):
        return "Not found", 404
    if _request_host_key() in HOST_FACES:
        # A brand face sells only its own products (same rule as
        # /api/products): a foreign brand's SKU must not render under
        # this host's brand — 404, never a misbranded page. Non-face
        # hosts (incl. pushrodshop.com, which sells the unified merch
        # catalog by design) are unchanged.
        face_cfg, _face_products = _face()
        if p.get("owner") != face_cfg["brand"]["id"]:
            return "Not found", 404
    resp = send_from_directory(FRONTEND, "product.html")
    if _request_host_key() in HOST_FACES:
        # Face host (RestorationEssentials/EverReady/Stitchfolk): the raw
        # bytes carry the product and the face brand, not the shared
        # PUSHROD shell (see _face_head). Other hosts unchanged.
        cfg, _products = _face()
        b = cfg["brand"]
        resp = _face_head(
            resp,
            title=f"{p['title']} | {b['name']}",
            description=(p.get("description") or "").strip()
            or f"{p['title']} — {b['name']}.")
        # Header/footer chrome too: the shared shell's small-print,
        # back link, and footer are PUSHROD's until JS swaps the name.
        resp = _face_product_chrome(resp, cfg)
    elif _request_host_key() in PUSHROD_STORE_HOSTS:
        # PushRod storefront (pushrodshop.com): the raw bytes carried
        # the bare PUSHROD title with no meta description and no
        # canonical for every SKU in the catalog (GSC finding). Give
        # each product page its own title, the product's description,
        # and a self-canonical on pushrodshop.com.
        resp = _face_head(
            resp,
            title=f"{p['title']} | PushRod",
            description=(p.get("description") or "").strip()
            or f"{p['title']} — PushRod.",
            canonical=f"https://pushrodshop.com/product/{sku}")
    # The shell ships an empty #pdetail; seed it with the product <h1>.
    resp = _inject_product_h1(resp, p["title"])
    if _request_host_key() in HOST_FACES:
        # RE guide pages carry the matching merch shelf strip.
        face_cfg, _face_products = _face()
        if face_cfg["brand"]["id"] == "restorationessentials":
            resp = _inject_re_guide_shelf(resp, sku)
    return resp


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
    # service serves gets URLs on its own domain. On a face host this
    # serves the face brand's own sitemap (the same body its
    # /sitemap-<brand>.xml route serves): stitchfolkpatterns.com
    # previously listed 383 RestorationEssentials guide URLs here and
    # zero Stitchfolk patterns (GSC finding). Hosts with no face
    # (pushrodshop.com, the default host) and the RestorationEssentials
    # face keep the pre-existing RestorationEssentials-owned listing:
    # restoreessentials.com is the RE storefront face, and other
    # brands' SKUs belong on their own domains.
    cfg, products = _face()
    paths = _brand_sitemap_paths(cfg["brand"]["id"], products)
    if cfg["brand"]["id"] == "skillforge" and \
            _request_host_key() not in SKILLFORGE_FACE_HOSTS:
        paths = None
    if paths is not None:
        return _sitemap_response(paths)
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
    # Face hosts declare their own brand's sitemap file (the /sitemap.xml
    # dispatch serves the same body there too); every other host keeps
    # the shared /sitemap.xml.
    cfg, _products = _face()
    fname = BRAND_SITEMAP_FILES.get(cfg["brand"]["id"])
    if cfg["brand"]["id"] == "skillforge" and \
            _request_host_key() not in SKILLFORGE_FACE_HOSTS:
        fname = None
    if fname:
        return Response(
            f"User-agent: *\nAllow: /\nSitemap: {base}/{fname}\n",
            mimetype="text/plain")
    return Response(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
                    mimetype="text/plain")


@app.get("/google7140981d206ed08c.html")
def google_site_verification():
    # Google Search Console HTML verification file, served host-agnostic
    # like /robots.txt above: stitchfolkpatterns.com and pushrodshop.com
    # are both served by this backend, and Search Console issued this same
    # filename/content for both properties, so one route puts the token at
    # every host's root (2026-10-05).
    return Response("google-site-verification: google7140981d206ed08c.html",
                    mimetype="text/html")


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
def _face():
    """(brand config, products) for this request's storefront face.

    Hosts in HOST_FACES get that brand's identity and only that brand's
    products; every other host gets the process brand and the full loaded
    catalog — byte-for-byte the pre-face behavior. Checkout and
    fulfillment never consult the face (process-wide on purpose), so a
    shopper who landed on the RE domain checks out identically to one
    who landed on the process domain."""
    cfg = HOST_FACES.get((request.host or "").split(":")[0].strip().lower())
    if cfg is None:
        return brand, PRODUCTS
    return cfg, [p for p in PRODUCTS if p["owner"] == cfg["brand"]["id"]]


@app.get("/api/brand")
def api_brand():
    cfg, products = _face()
    b = cfg["brand"]
    return jsonify({
        "id": b["id"], "name": b["name"], "tagline": b["tagline"],
        "doors": b.get("doors", []),
        "theme": cfg["theme"], "stats": catalog_stats(products),
        "sizes": APPAREL_SIZES,
        "stripe_ready": STRIPE_READY,
        "stripe_mode": STRIPE_MODE,
    })


@app.get("/api/products")
def api_products():
    # wholesale_eligible is additive metadata for the storefront's partner
    # pricing display; retail guests ignore it. Dark-staged rows (listed=0)
    # never appear on public surfaces. On a host-mapped brand face the
    # grid is that brand's products only (see _face); other hosts unchanged.
    _cfg, products = _face()
    return jsonify([
        {**p, "wholesale_eligible": wholesale_mod.is_wholesale_eligible(p)}
        for p in products if p.get("listed", True)
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
                      "qty": qty, "unit_cents": unit_cents,
                      "owner": p["owner"]})
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
            "product_data": {"name": f"{_checkout_brand_name(l)} — {l['title']}"
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
    else:
        # Show the promotion-code field on Stripe Checkout (money-path
        # probe fix, 2026-10-05): without this flag Stripe renders no
        # promo entry point at all, so a code like FOUNDER100 can never
        # be typed. Mutually exclusive with `discounts` (Stripe rejects
        # a session carrying both), hence the else. A code only takes
        # effect if a matching coupon + promotion code exists in the
        # session-creating account; the flag alone grants no discount.
        create_kwargs["allow_promotion_codes"] = True
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


# ---------- Family Recipe Cookbook lane (EverReady ER-FRC-001, 2026-10-06) ----------
# Per-contributor card-photo intake -> never-guess vision transcription
# ([?] flags, contributor confirm-and-lock) -> 8.5x11 photo-left/text-
# right book assembly (backend/cookbook.py). Token-gated throughout.
# Registered defensively: a cookbook fault must never take the store
# down (same rule as the host faces above).
try:
    cookbook_mod.init(app, public_base_url=_public_base_url)
except Exception:  # noqa: BLE001
    log.exception("cookbook lane failed to register — store continues")


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
