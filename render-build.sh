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
BRAND=gateway python3 -c "
import sys, os
sys.path.insert(0, 'backend')
os.environ['BRAND'] = 'gateway'
import app
assert len(app.PRODUCTS) == 642, f'expected 642 products, got {len(app.PRODUCTS)}'
assert all(p['purchasable'] for p in app.PRODUCTS), 'unpriced SKUs present'
print('catalog OK:', len(app.PRODUCTS), 'products, all priced')
"
