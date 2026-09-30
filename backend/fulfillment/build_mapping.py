#!/usr/bin/env python3
"""Build the Printful fulfillment mapping scaffold: backend/fulfillment/printful_mapping.json.

Reads the four catalog CSVs and emits one mapping entry per fulfillable variant:
  - tee / sweatshirt: 6 keys per SKU ("SKU:SIZE")
  - hat / mug / decal / patch / embroidered patch: 1 key per SKU ("SKU")

Entries carry null catalog_variant_id / print_file_url until fill_mapping.py runs
against the live Printful API (credential was invalid as of 2026-09-30). The _fill
block on each entry holds everything the fill script needs: blank product, color
candidates, size, and the local print-file path.

Non-mappable types (metal sign, sign, keychain, banner, vinyl banner, flag,
decal set, decal sheet) are recorded in _meta.unmapped with reasons; they get no keys.

Usage: python3 backend/fulfillment/build_mapping.py   (run from repo root)
"""
import json, os, sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "backend"))
from catalog import load_unified_catalog, IMAGE_LIBS  # noqa: E402

PRINT_FILES = os.path.expanduser("~/workspace/pushrod-print-files")
OUT = os.path.join(REPO, "backend", "fulfillment", "printful_mapping.json")

SIZES = ["S", "M", "L", "XL", "2XL", "3XL"]

# Blank product choices (verified against Printful's public catalog 2026-09-30)
BLANKS = {
    "tee": {
        "product": "Comfort Colors 1717",
        "technique": "dtg",
        "placement": "front",
        "note": "matches catalog 'Comfort Colors-style' merch spec; DTG front print",
    },
    "sweatshirt": {
        "product": "Gildan 18000 Heavy Blend Crewneck",
        "technique": "dtg",
        "placement": "front",
        "note": "heavy-blend crewneck; DTG front print",
    },
    "hat": {
        "product": "Yupoong 6606 Retro Trucker",
        "technique": "embroidery",
        "placement": "front",
        "note": "matches catalog 'embroidered trucker' descriptions; embroidered front panel",
    },
    "mug": {
        "product": "11oz Black Mug",
        "technique": None,  # resolved by fill_mapping.py from the catalog product's default technique
        "technique_candidates": ["sublimation"],
        "placement": "front",  # verified against API placements by fill_mapping.py
        "note": "matte black ceramic 11oz",
    },
    "decal": {
        "product": "Kiss-Cut Stickers",
        "technique": None,  # resolved by fill_mapping.py
        "placement": "front",
        "note": "weatherproof vinyl; color_candidates pick white vs transparent by finish",
    },
    "patch": {
        "product": "Embroidered Patches",
        "technique": "embroidery",
        "placement": "front",
        "note": "square 3x3 default; rectangle 3.5x2.25 for wide designs (chosen by art aspect)",
    },
    "embroidered patch": {
        "product": "Embroidered Patches",
        "technique": "embroidery",
        "placement": "front",
        "note": "square 3x3 default; rectangle 3.5x2.25 for wide designs (chosen by art aspect)",
    },
}

COLOR_CANDIDATES = {
    "tee": {
        "black": ["Black"], "charcoal": ["Charcoal"], "navy": ["Navy"],
        "olive": ["Olive"], "forest": ["Forest Green"], "brown": ["Brown", "Espresso"],
        "burgundy": ["Burgundy"], "maroon": ["Maroon"], "cream": ["Cream"],
        "tan": ["Sandstone", "Tan"],
    },
    "sweatshirt": {
        "black": ["Black"], "charcoal": ["Charcoal"], "navy": ["Navy"],
        "olive": ["Military Green"], "forest": ["Forest Green"],
        "brown": ["Dark Chocolate", "Brown"], "burgundy": ["Maroon"],
        "maroon": ["Maroon"], "cream": ["Sand", "White"], "tan": ["Sand"],
    },
    "hat": {
        "Black": ["Black"],
        "Charcoal": ["Dark Heather Grey", "Heather Grey", "Black"],
        "Brown": ["Brown/Khaki"],
        "Camo": ["Camo"],
        "Dark Brown": ["Brown/Khaki"],
        "Forest": ["Black", "Dark Heather Grey"],   # 6606 has no green; needs review
        "Navy": ["Navy"],
        "Olive": ["Khaki", "Black"],                # uncertain; needs review
    },
    "mug": {"black": ["Black"]},
}

UNMAPPABLE = {
    "metal sign": "no sane Printful equivalent (rigid metal signage)",
    "sign": "no sane Printful equivalent",
    "keychain": "no sane Printful equivalent",
    "banner": "no sane Printful equivalent",
    "vinyl banner": "no sane Printful equivalent",
    "flag": "no sane Printful equivalent",
    "decal set": "multi-piece set; no Printful equivalent",
    "decal sheet": "multi-design sheet; no Printful equivalent",
}

PROFILE_OF = {
    "tee": "apparel", "sweatshirt": "apparel", "hat": "hat", "mug": "mug",
    "decal": "sticker", "patch": "patch", "embroidered patch": "patch",
}

HAT_COLOR_REVIEW = {"Forest", "Olive"}  # no confident 6606 match; flag for manual review


def design_base(row):
    d = (row.get("design_file") or "").strip()
    d = d.rsplit("/", 1)[-1]
    return d[:-5] if d.lower().endswith(".webp") else d


def product_lib(row):
    """Merch-library key for this row (pushrod/modern/muscle/truck)."""
    for prefix, lib in IMAGE_LIBS.items():
        if row["sku"].startswith(prefix):
            return lib
    return ""


def decal_candidates(row):
    desc = (row.get("description") or "").lower()
    if "clear" in desc:
        return ["Transparent", "White"]
    return ["White"]


def patch_shape(lib, base):
    """Square 3x3 default; rectangle 3.5x2.25 when the art is wide."""
    p = os.path.join(PRINT_FILES, lib, "patch", base + ".png")
    if os.path.exists(p):
        try:
            from PIL import Image
            w, h = Image.open(p).size
            if h and w / h > 1.25:
                return 'Rectangle 3.5" x 2.25"'
        except Exception:
            pass
    return 'Square 3" x 3"'


def main():
    data = os.path.join(REPO, "data")
    sources = [
        (os.path.join(data, f"{lib}-catalog.csv"), os.path.join(data, f"{lib}-prices.json"))
        for lib in ("pushrod", "modern", "muscle", "truck")
    ]
    catalog = load_unified_catalog(sources)
    mapping = {}
    unmapped = []
    stats = {"keys": 0, "skus": 0}

    for row in catalog:
        sku = row["sku"]
        ptype = (row.get("type") or "").strip()
        lib = product_lib(row)
        base = design_base(row)
        if not sku or not base:
            continue

        if ptype in UNMAPPABLE:
            unmapped.append({"sku": sku, "type": ptype, "reason": UNMAPPABLE[ptype]})
            continue
        if ptype not in BLANKS:
            unmapped.append({"sku": sku, "type": ptype, "reason": "unknown type; no blank chosen"})
            continue

        blank = BLANKS[ptype]
        profile = PROFILE_OF[ptype]
        print_local = os.path.join(PRINT_FILES, lib, profile, base + ".png")
        stats["skus"] += 1

        def entry(extra_fill=None):
            fill = {
                "blank_product": blank["product"],
                "print_file_local": print_local,
                "print_file_profile": profile,
                "print_file_exists": os.path.exists(print_local),
            }
            if blank.get("technique_candidates"):
                fill["technique_candidates"] = blank["technique_candidates"]
            if extra_fill:
                fill.update(extra_fill)
            return {
                "catalog_variant_id": None,
                "placement": blank["placement"],
                "print_file_url": None,
                "technique": blank["technique"],
                "_fill": fill,
            }

        color_raw = (row.get("base_color") or "").strip()

        if ptype in ("tee", "sweatshirt"):
            cmap = COLOR_CANDIDATES[ptype]
            cands = cmap.get(color_raw.lower(), [color_raw] if color_raw else [])
            for size in SIZES:
                key = f"{sku}:{size}"
                mapping[key] = entry({
                    "color": color_raw,
                    "color_candidates": cands,
                    "size": size,
                })
                stats["keys"] += 1
        elif ptype == "hat":
            cands = COLOR_CANDIDATES["hat"].get(color_raw, [color_raw] if color_raw else [])
            e = entry({"color": color_raw, "color_candidates": cands})
            if color_raw in HAT_COLOR_REVIEW:
                e["_fill"]["color_review"] = (
                    f"No confident Yupoong 6606 match for catalog color '{color_raw}'; "
                    "fill script picks the first available candidate and flags it here."
                )
            mapping[sku] = e
            stats["keys"] += 1
        elif ptype == "mug":
            mapping[sku] = entry({
                "color": "Black",
                "color_candidates": COLOR_CANDIDATES["mug"]["black"],
            })
            stats["keys"] += 1
        elif ptype == "decal":
            mapping[sku] = entry({"color_candidates": decal_candidates(row)})
            stats["keys"] += 1
        elif ptype in ("patch", "embroidered patch"):
            shape = patch_shape(lib, base)
            mapping[sku] = entry({"patch_shape": shape})
            stats["keys"] += 1

    # de-duplicate unmapped (one row per SKU)
    seen, unmapped_unique = set(), []
    for u in unmapped:
        if u["sku"] not in seen:
            seen.add(u["sku"])
            unmapped_unique.append(u)

    mapping["_meta"] = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "generator": "backend/fulfillment/build_mapping.py",
        "key_count": stats["keys"],
        "sku_count": stats["skus"],
        "unmapped_count": len(unmapped_unique),
        "blanks": BLANKS,
        "auth_status": (
            "Printful API credential (custom.printful) returned 401 'access token "
            "invalid' on 2026-09-30. catalog_variant_id and print_file_url are null "
            "until fill_mapping.py is run with a working token."
        ),
        "fill_instructions": (
            "Run backend/fulfillment/fill_mapping.py with a working Printful token. "
            "It resolves each _fill.blank_product via GET /v2/catalog-products, picks "
            "variants by color_candidates + size, pushes each local PNG to a public "
            "URL, registers it via POST /v2/files (File Library API), and writes the "
            "returned IDs/URLs into the mapping. DRY RUN only: never creates orders."
        ),
        "unmapped": unmapped_unique,
    }

    out = {"_meta": mapping.pop("_meta"), "mappings": mapping}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {OUT}: {stats['keys']} keys, {stats['skus']} SKUs, "
          f"{len(unmapped_unique)} unmapped SKUs")


if __name__ == "__main__":
    main()
