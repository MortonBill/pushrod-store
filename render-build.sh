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
    curl -sL -o "$lib.zip" "$URL"
    unzip -q "$lib.zip"
    rm "$lib.zip"
  fi
done
cd ../..

# Sanity: catalog loads, all SKUs priced.
# Honest purchasability (Bill 2026-09-30): 43 SKUs (metal signs, banners,
# keychains, flags, decal sets) have no Printful equivalent and are
# intentionally NOT purchasable — the storefront renders them unavailable
# and checkout blocks them. The build must NOT require them to be
# purchasable; it requires every SKU to be priced and the catalog complete.
BRAND=gateway python3 -c "
import sys, os
sys.path.insert(0, 'backend')
os.environ['BRAND'] = 'gateway'
import app
assert len(app.PRODUCTS) == 642, f'expected 642 products, got {len(app.PRODUCTS)}'
assert all(p['price'] is not None for p in app.PRODUCTS), 'unpriced SKUs present'
n_purch = sum(1 for p in app.PRODUCTS if p['purchasable'])
print(f'catalog OK: {len(app.PRODUCTS)} products, all priced, {n_purch} purchasable')
"
