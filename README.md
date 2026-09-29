# PUSHROD Storefront — v1

Our own standalone store. We own the code, the cart, and checkout — no
platform in the middle. Printful is fulfillment-only via its API.

```
customer buys on OUR site
      │  (frontend: pure static HTML/CSS/JS → JSON API)
      ▼
backend API (Flask) ──► Stripe Checkout (TEST mode in v1)
      │  payment confirmed
      ▼
fulfillment module ──► Printful API v2 → prints & ships
```

## Layout

```
pushrod-store/
  brands/pushrod.yaml          brand config: name, theme, SKU prefixes, ownership map
  backend/
    app.py                     Flask: serves frontend + JSON API (/api/*)
    catalog.py                 catalog loader — unified 640-SKU data model
    stripe checkout            inside app.py (TEST-mode guarded)
    fulfillment/
      printful_client.py       REAL Printful v2 API client (orders, catalog, shipping)
      fulfill.py               paid cart → Printful order glue
      printful_mapping.example.json   our SKU → Printful variant map template
    test_integration.py        9 end-to-end tests (Stripe stubbed, Printful dry-run)
  frontend/
    index.html / product.html / success.html / cancel.html
    static/css/store.css  static/js/store.js
  .env.example  README.md  MIGRATION-TO-RE.md
```

## Run locally

```bash
cd ~/workspace/pushrod-store
./.venv/bin/python backend/app.py        # http://127.0.0.1:8091
```

Copy `.env.example` to `.env` and fill in keys as they become available.
Without a Stripe test key the store browses and carts fine; checkout returns
503 with instructions. Without `PRINTFUL_API_TOKEN`, fulfillment raises a
clear config error naming the exact dashboard steps.

Run the integration tests:

```bash
./.venv/bin/python backend/test_integration.py
```

## Stripe

v1 runs **TEST mode only**. `app.py` refuses any `STRIPE_TEST_SECRET_KEY`
that does not start with `sk_test_` — a live key crashes the app at startup
rather than charging real money by accident.

- Test checkout: set `STRIPE_TEST_SECRET_KEY=sk_test_...`, add to cart,
  check out with Stripe's test card `4242 4242 4242 4242`.
- After payment, `/checkout/success?session_id=...` calls `/api/fulfill`,
  which re-verifies `payment_status=paid` with Stripe before creating the
  Printful order.
- Production path: point a Stripe webhook at `/api/stripe/webhook`
  (`checkout.session.completed`) and set `STRIPE_WEBHOOK_SECRET`. The v1
  `/api/fulfill` redirect flow is for local testing only.

### Flipping Stripe to live (when the business says so)

1. In `backend/app.py`, replace the `sk_test_` guard with an explicit
   `STORE_LIVE=1` env gate (fail closed: live keys refused unless the flag
   is set).
2. Set `STRIPE_TEST_SECRET_KEY` → live `sk_live_...` (rename the env var to
   `STRIPE_SECRET_KEY` at the same time), `STRIPE_WEBHOOK_SECRET` from the
   live webhook endpoint.
3. Switch fulfillment from `/api/fulfill` to the webhook as the single
   order-creation path; keep the idempotency (`external_id =
   pushrod-<session-id>`) so retries never double-print.
4. Run one live $1 test product purchase and refund it before opening.

## Printful — API key generation

No key is stored in this repo. To generate it (a browser task does this
with the saved printful.com login from the Secure Vault):

1. Sign in at printful.com → **Stores** → select the store → **Settings**.
2. Open the **API** section → generate a **private token**.
3. Copy the token into the environment: `export PRINTFUL_API_TOKEN=...`
   (or add it to `.env`). Never commit it.

Then build the SKU mapping: copy
`backend/fulfillment/printful_mapping.example.json` to
`backend/fulfillment/printful_mapping.json` and fill `mappings` — for each
SKU+size, the Printful `catalog_variant_id` (browse with
`GET /v2/catalog-products` / `/catalog-variants` via `printful_client.py`)
and a public `print_file_url` for the artwork. Check coverage any time at
`GET /api/printful/mapping-status`. Checkout **refuses** to take payment
for any line with no mapping, so a customer can never pay for something we
cannot ship.

## Embed / deploy — the store lives IN our site

The store is one self-contained unit: static frontend + JSON API + the
fulfillment module. Three ways to put it on a brand site:

1. **Reverse proxy (recommended):** the site's web server proxies `/store/*`
   to this app and `/store/api/*` to its API. One deploy, brand switched by
   the `BRAND` env var.
2. **Iframe embed:** serve the app on `store.ourbrand.com` and embed
   `/` in the brand site's shop page. Zero template merging.
3. **Static export:** copy `frontend/` into the site's static build and
   point `store.js` at the API base URL; the Flask app then runs API-only.

The catalog images in v1 are served from the local merch library
(`/img/...` → `~/workspace/your_files/pushrod-merch/`); point
`catalog.image_base` at the CDN/site asset path when deploying.

## Pricing honesty

Only prices present in the library's `wholesale-catalog.json` are sellable.
All four price lists are CONFIRMED 2026-09-26 (Bill verified against Printful):
tees $26, hats $24, pullovers $44, mugs $15, signs $19, decals $4, banners $14,
patches $6, keychains $10, flags $25. Every one of the 640 SKUs carries a
confirmed price — nothing renders "Price TBD". SKUs with no price (should one
ever appear) are not purchasable. Server-side validation re-prices every
checkout — client prices are never trusted.
