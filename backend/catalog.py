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
}

# SKU prefix -> merch-library key, for resolving design artwork per line.
IMAGE_LIBS = {
    "PR-": "pushrod",
    "7B-": "pushrod",
    "RE-MC-": "muscle",
    "RE-CT-": "truck",
    "RE-MP-": "modern",
}

# Product types that need a size choice at purchase time.
SIZED_TYPES = {"tee", "sweatshirt"}
APPAREL_SIZES = ["S", "M", "L", "XL", "2XL", "3XL"]


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


def load_catalog(csv_path, prices_json_path=None, sku_prefixes=None):
    """Load products. sku_prefixes filters to the brand's prefixes
    (None = all prefixes, used by the future 4-door gateway)."""
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
            products.append({
                "sku": sku,
                "prefix": prefix,
                "owner": OWNERSHIP[prefix],
                "type": row["type"].strip(),
                "title": row["title"].strip(),
                "description": row["description"].strip(),
                "base_color": row["base_color"].strip(),
                "design_file": design_file,
                # Unambiguous artwork URL: /img/<library>/<design_file>.
                # Libraries live in different dirs (pushrod-merch vs the three
                # re-merch lines), so the library key is part of the URL.
                "image_url": f"/img/{IMAGE_LIBS[prefix]}/{design_file}",
                "price": price,  # None -> "Price TBD", not purchasable
                "purchasable": price is not None,
                "needs_size": row["type"].strip() in SIZED_TYPES,
            })
    products.sort(key=lambda p: p["sku"])
    return products


def load_unified_catalog(sources, sku_prefixes=None):
    """Merge multiple (csv_path, prices_json_path) catalog sources into one
    unified product list, sorted by SKU. Asserts ZERO duplicate SKUs across
    sources — the unified-catalog rule (Bill 2026-09-26: one big catalog that
    sorts by sku when the gateway selection is accessed; ownership partitions
    by prefix). sku_prefixes filters to a brand's prefixes (None/empty = all,
    used by the 4-door gateway)."""
    products = []
    seen = {}
    for csv_path, prices_json_path in sources:
        for p in load_catalog(csv_path, prices_json_path, sku_prefixes=sku_prefixes):
            sku = p["sku"]
            if sku in seen:
                raise ValueError(
                    f"SKU COLLISION in unified catalog: {sku} in both "
                    f"{seen[sku]} and {csv_path} — resolve before serving")
            seen[sku] = csv_path
            products.append(p)
    products.sort(key=lambda p: p["sku"])
    return products


def catalog_stats(products):
    from collections import Counter
    return {
        "total": len(products),
        "by_type": dict(Counter(p["type"] for p in products)),
        "purchasable": sum(1 for p in products if p["purchasable"]),
        "price_tbd": sum(1 for p in products if not p["purchasable"]),
        "by_owner": dict(Counter(p["owner"] for p in products)),
    }
