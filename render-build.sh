#!/bin/bash
# Render build script: install deps, fetch product images, verify catalog.
set -e

pip install -r requirements.txt

# Product images (246MB total) live on Catbox; pull them into data/img/ at build.
mkdir -p data/img
cd data/img
for lib in pushrod muscle modern truck; do
  if [ ! -d "$lib" ]; then
    echo "Fetching $lib images..."
    case $lib in
      pushrod) URL="https://files.catbox.moe/cgku78.zip" ;;
      muscle)  URL="https://files.catbox.moe/9l3smn.zip" ;;
      modern)  URL="https://files.catbox.moe/1rjgv2.zip" ;;
      truck)   URL="https://files.catbox.moe/byluli.zip" ;;
    esac
    # Best-effort: if the image host is unreachable the build must still
    # succeed — the app falls back to the /var/data/img-cache disk cache
    # for artwork at runtime (backend/app.py _img_lib_dir). (2026-10-03:
    # files.catbox.moe unreachable from Render killed every deploy with
    # curl exit status 7 under `set -e`.)
    if curl -sL --retry 3 --retry-delay 5 --connect-timeout 20 \
         -o "$lib.zip" "$URL" && unzip -q "$lib.zip"; then
      rm "$lib.zip"
    else
      echo "WARN: $lib images unavailable this build; runtime disk-cache fallback applies"
      rm -f "$lib.zip"
    fi
  fi
done

# 2026-10-05 overlay: storefront art for the pre-1953 truck designs
# (RE-CT-H041 / RE-CT-T061 / RE-CT-H042 / RE-CT-T062) layered over the
# base truck library above. Contents verified byte-identical to the
# locally built overlay zip (sha256 c88d53f59de753b3220c132ed5a9831c952f38fa637da0b7379f7f6093219ba7).
curl -fsSL --retry 3 -o truck-overlay.zip "https://files.catbox.moe/yr9s1y.zip" && unzip -qo truck-overlay.zip && rm -f truck-overlay.zip
cd ../..

# Sanity: catalog loads, all SKUs priced.
# Honest purchasability (Bill 2026-09-30): 43 SKUs (metal signs, banners,
# keychains, flags, decal sets) have no Printful equivalent and are
# intentionally NOT purchasable — the storefront renders them unavailable
# and checkout blocks them. The build must NOT require them to be
# purchasable; it requires every SKU to be priced and the catalog complete.
# Count gates are FLOORS, not exact matches (root-caused 2026-10-09):
# commit df886f4 bumped this assert 642->643 when adding RE-GD-1970-
# OLDSMOBILE-442, but RE-GD-* guide rows load only under the skillforge /
# restorationessentials brand configs — BRAND=gateway never reads
# data/re-catalog.csv, so the gateway count stayed 642 and 12 straight
# deploys died here. Exact-count asserts strand deploys every time a wave
# adds a catalog row; floor asserts still catch the failure this gate
# exists for (a catalog CSV reverted/missing -> count collapse). Wave
# SKUs are asserted present, listed, purchasable, and correctly priced
# explicitly instead. Floors verified locally at the fix commit:
# gateway 642, skillforge 967 — raise a floor only with a loader count
# run at the same head (never guess).
BRAND=gateway python3 -c "
import sys, os
sys.path.insert(0, 'backend')
os.environ['BRAND'] = 'gateway'
import app
assert len(app.PRODUCTS) >= 642, f'catalog SHRANK below floor: expected >=642 gateway products, got {len(app.PRODUCTS)}'
assert all(p['price'] is not None for p in app.PRODUCTS), 'unpriced SKUs present'
n_purch = sum(1 for p in app.PRODUCTS if p['purchasable'])
print(f'gateway catalog OK: {len(app.PRODUCTS)} products, all priced, {n_purch} purchasable')
"
BRAND=skillforge python3 -c "
import sys, os
sys.path.insert(0, 'backend')
os.environ['BRAND'] = 'skillforge'
import app
assert len(app.PRODUCTS) >= 967, f'catalog SHRANK below floor: expected >=967 digital products, got {len(app.PRODUCTS)}'
assert all(p['price'] is not None for p in app.PRODUCTS), 'unpriced SKUs present'
_by_sku = {p['sku']: p for p in app.PRODUCTS}
for _sku in ('RE-GD-1970-OLDSMOBILE-442',
             'IH-NORTON-COMMANDO',
             'IH-NORTON-COMMANDO-BUYERS-GUIDE'):
    _p = _by_sku.get(_sku)
    assert _p is not None, f'wave SKU missing from catalog: {_sku}'
    assert _p['listed'] and _p['purchasable'], f'wave SKU not live: {_sku} listed={_p[\"listed\"]} purchasable={_p[\"purchasable\"]}'
    assert _p['price']['amount'] == 29.95, f'wave SKU mispriced: {_sku} {_p[\"price\"]}'
n_purch = sum(1 for p in app.PRODUCTS if p['purchasable'])
print(f'skillforge catalog OK: {len(app.PRODUCTS)} products, all priced, {n_purch} purchasable, wave SKUs live')
"
