"""
PushRod storefront — catalog loader.

Reads the catalog source of truth (CSV + price list) and serves product data
as JSON. Data model is unified-catalog ready: every product carries a sku,
its prefix, and its owning brand per the ownership partition map, so the
4-door gateway can merge all four SKU lines (PR-*, RE-MC-*, RE-CT-*,
RE-MP-*) with zero collisions and each brand store filters to its own
prefixes via its brand config.
"""
import csv
import json
import os

# SKU prefix -> owning brand id. Unified catalog rule: prefixes never overlap,
# so ownership is unambiguous and the gateway can route by prefix alone.
# NOTE: these MUST match the real catalog CSV prefixes (verified 2026-09-27:
# 640 SKUs, zero collisions). RE-CT- = classic trucks, RE-MP- = modern performance.
# 7B- = seven-brand badge line (owned by PushRod merch, designs in pushrod-merch).
OWNERSHIP = {
    "PR-": "pushrod",
    "7B-": "pushrod",
    "RE-MC-": "restorationessentials",
    "RE-CT-": "restorationessentials",
    "RE-MP-": "restorationessentials",
    # RestorationEssentials guide PDFs (checkout migration Lane 1). Rows
    # live in data/re-catalog.csv and load UNLISTED (dark) until the lane
    # gate — see the `listed` handling in load_catalog.
    "RE-GD-": "restorationessentials",
    "SF-": "skillforge",
    "IH-": "ironhead",
}

# SKU prefix -> merch-library key, for resolving design artwork per line.
IMAGE_LIBS = {
    "PR-": "pushrod",
    "7B-": "pushrod",
    "RE-MC-": "muscle",
    "RE-CT-": "truck",
    "RE-MP-": "modern",
    "RE-GD-": "muscle",
    "SF-": "skillforge",
    "IH-": "ironhead",
}

# CSV `listed` values that take a product dark (loaded but never listed or
# sellable). Anything else — including a missing column — means listed,
# exactly as every pre-2026-10 catalog behaved.
UNLISTED_VALUES = {"0", "false", "no", "dark", "unlisted"}

# Product types that need a size choice at purchase time.
SIZED_TYPES = {"tee", "sweatshirt"}
APPAREL_SIZES = ["S", "M", "L", "XL", "2XL", "3XL"]


def mapping_keys_for(sku, ptype):
    """Required printful_mapping.json keys for one product.

    Sized goods (tee/sweatshirt) need one key per size ("SKU:SIZE");
    everything else needs its bare SKU key. Mirrors
    fulfillment.fulfill.mapping_key (kept inline so catalog.py stays
    dependency-free).
    """
    if (ptype or "").strip().lower() in SIZED_TYPES:
        return [f"{sku}:{s}" for s in APPAREL_SIZES]
    return [sku]


def mapping_complete(sku, ptype, mapping):
    """True when every required mapping key exists for the product.

    This is the honest-purchasability gate (Bill 2026-09-30): a product with
    a price but no Printful mapping must never be sold, or the customer pays
    for something we cannot ship (checkout 409 trap).

    Key presence alone is not enough: the mapped values must be non-null.
    A sku whose entry exists but has no catalog_variant_id or print_file_url
    is NOT fulfillable and must not be sold.
    """
    m = mapping or {}

    def _usable(entry):
        return (bool(entry) and entry.get("catalog_variant_id") is not None
                and entry.get("catalog_variant_id") != ""
                and bool(entry.get("print_file_url")))

    return all(_usable(m.get(k)) for k in mapping_keys_for(sku, ptype))


def sku_prefix(sku):
    for prefix in sorted(OWNERSHIP, key=len, reverse=True):
        if sku.startswith(prefix):
            return prefix
    return None


def load_prices(prices_json_path):
    """Returns {sku: {'amount': float, 'status': 'draft'|'confirmed'}}.
    Only prices present in the library's price list are returned — never
    invented. draft_msrp values are flagged draft."""
    if not prices_json_path or not os.path.exists(prices_json_path):
        return {}
    with open(prices_json_path) as f:
        data = json.load(f)
    prices = {}
    for p in data.get("products", []):
        sku = p.get("sku")
        if p.get("draft_msrp") is not None:
            prices[sku] = {"amount": float(p["draft_msrp"]), "status": "draft"}
        elif p.get("msrp") is not None:
            prices[sku] = {"amount": float(p["msrp"]), "status": "confirmed"}
    return prices


def load_catalog(csv_path, prices_json_path=None, sku_prefixes=None, mapping=None):
    """Load products. sku_prefixes filters to the brand's prefixes
    (None = all prefixes, used by the future 4-door gateway).
    mapping is the printful_mapping.json "mappings" dict (or None): a
    product is purchasable ONLY when it has a price AND every required
    mapping key exists — see mapping_complete()."""
    prices = load_prices(prices_json_path)
    products = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            sku = row["sku"].strip()
            prefix = sku_prefix(sku)
            if prefix is None:
                continue  # unknown prefix: not part of the unified catalog
            if sku_prefixes and prefix not in sku_prefixes:
                continue
            price = prices.get(sku)
            design_file = row["design_file"].strip()
            ptype = row["type"].strip()
            # Dark staging (2026-10-02, checkout migration Lane 1): a row
            # may carry listed=0 to load into the catalog WITHOUT being
            # listed or sellable on any surface — the catalog is staged
            # ahead of its fulfillment (object-storage upload, service
            # bring-up, test-purchase gate) and flips visible lane by
            # lane. Unlisted forces not-purchasable regardless of price or
            # deliverable, so a dark row can never be bought by accident.
            listed = (row.get("listed") or "").strip().lower() \
                not in UNLISTED_VALUES
            # Fulfillment seam (2026-10-02, SkillForge digital pilot): a
            # catalog row MAY carry fulfillment_type (print|digital) and
            # digital_file (the deliverable's filename, served from the
            # digital-files dir — see fulfillment/digital.py). Both columns
            # are optional: rows without them are print goods fulfilled via
            # Printful, exactly as before. Aliases tolerated while the
            # digital catalog CSVs are still being written.
            ftype = (row.get("fulfillment_type") or row.get("fulfillment")
                     or "print").strip().lower()
            if ftype not in ("print", "digital"):
                ftype = "print"
            digital_file = (row.get("digital_file") or row.get("digital_path")
                            or row.get("download_file") or "").strip()
            if not digital_file and (row.get("delivery_file") or "").strip():
                # Catalog CSVs written by the content lane name the
                # deliverable column delivery_file and may carry a local
                # absolute path; the store serves bare filenames from the
                # digital-files dir, so normalize to the basename.
                digital_file = os.path.basename(row["delivery_file"].strip())
            if ftype == "digital":
                # Digital purchasability mirrors the print rule — the
                # honest-purchasability gate (Bill 2026-09-30) applied to a
                # file instead of a Printful mapping: price + a deliverable
                # file, or it can never be sold.
                purchasable = price is not None and bool(digital_file)
            else:
                purchasable = price is not None and mapping_complete(
                    sku, ptype, mapping)
            # Dark rows (listed=0) are never sellable, price/file or not.
            purchasable = purchasable and listed
            products.append({
                "sku": sku,
                "prefix": prefix,
                "owner": OWNERSHIP[prefix],
                "type": ptype,
                "fulfillment_type": ftype,
                "digital_file": digital_file,
                "title": row["title"].strip(),
                "description": row["description"].strip(),
                "base_color": row["base_color"].strip(),
                "design_file": design_file,
                # Unambiguous artwork URL: /img/<library>/<design_file>.
                # Libraries live in different dirs (pushrod-merch vs the three
                # re-merch lines), so the library key is part of the URL.
                "image_url": f"/img/{IMAGE_LIBS[prefix]}/{design_file}",
                "price": price,  # None -> "Price TBD", not purchasable
                # Honest purchasability (Bill 2026-09-30): price alone is not
                # enough — print needs every required Printful mapping key,
                # digital needs its deliverable file (see above).
                "purchasable": purchasable,
                # Dark-staging flag (see above): False = loaded but never
                # listed on public surfaces and never sellable.
                "listed": listed,
                "needs_size": ptype in SIZED_TYPES,
            })
    # Bill 2026-09-30: the 7B- seven-brand line sorts LAST in the catalog so
    # daily shoppers see the brand lines first; 7B- stays purchasable.
    products.sort(key=lambda p: (p["prefix"] == "7B-", p["sku"]))
    return products


def load_unified_catalog(sources, sku_prefixes=None, mapping=None):
    """Merge multiple (csv_path, prices_json_path) catalog sources into one
    unified product list, sorted by SKU with the 7B- seven-brand line last
    (Bill 2026-09-30). Asserts ZERO duplicate SKUs across
    sources — the unified-catalog rule (Bill 2026-09-26: one big catalog that
    sorts by sku when the gateway selection is accessed; ownership partitions
    by prefix). sku_prefixes filters to a brand's prefixes (None/empty = all,
    used by the 4-door gateway). mapping is the printful_mapping.json
    "mappings" dict; see load_catalog for the purchasability rule."""
    products = []
    seen = {}
    for csv_path, prices_json_path in sources:
        for p in load_catalog(csv_path, prices_json_path, sku_prefixes=sku_prefixes,
                              mapping=mapping):
            sku = p["sku"]
            if sku in seen:
                raise ValueError(
                    f"SKU COLLISION in unified catalog: {sku} in both "
                    f"{seen[sku]} and {csv_path} — resolve before serving")
            seen[sku] = csv_path
            products.append(p)
    products.sort(key=lambda p: (p["prefix"] == "7B-", p["sku"]))
    return products


def catalog_stats(products):
    from collections import Counter
    return {
        "total": len(products),
        "by_type": dict(Counter(p["type"] for p in products)),
        "purchasable": sum(1 for p in products if p["purchasable"]),
        # price_tbd = genuinely unpriced; fulfillment_pending = priced but
        # the Printful mapping is incomplete (honest-purchasability gate).
        "price_tbd": sum(1 for p in products if p["price"] is None),
        "fulfillment_pending": sum(
            1 for p in products if p["price"] is not None and not p["purchasable"]),
        # Dark-staged rows (listed=0): loaded, counted here, never listed.
        "unlisted": sum(1 for p in products if not p.get("listed", True)),
        "by_owner": dict(Counter(p["owner"] for p in products)),
    }
