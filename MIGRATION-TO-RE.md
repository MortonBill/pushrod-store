# Migration path: cloning the storefront for RestorationEssentials (brand #2)

Built 2026-09-27 on Bill's direct order ("build the store, add the gateway,
cart, checkout process, api integration, testing and then loading with all
640 items with unique sku's. test again and then mirror to RE"). The RE
mirror is live as config: `brands/restorationessentials.yaml` —
`BRAND=restorationessentials PORT=8092`. This doc is now the runbook for
operating both brand stores, not a future plan.

## 1. Brand config — one new YAML file

Create `brands/restorationessentials.yaml` modeled on
`brands/pushrod.yaml`:

- `brand.id: restorationessentials`, `brand.name` = the RE store name,
  `brand.tagline` = RE's line.
- `brand.sku_prefixes`: `["RE-MC-", "RE-CT-", "RE-MP-"]` — the loader
  filters the unified catalog to exactly these. PushRod's `PR-*` products
  never appear in the RE store.
- `theme.*`: RE's palette (suggest deep shop-red + cream on near-black,
  distinct from PUSHROD's amber) and fonts. `store.js` applies the theme as
  CSS variables at boot — no template changes needed.
- `catalog.csv`: path to the unified catalog CSV (all four SKU lines, zero
  collisions — prefixes guarantee it).
- `store.stripe_secret_key_env`: `RE_STRIPE_TEST_SECRET_KEY` (separate
  Stripe account/test keys per brand — see §3).
- `store.printful_api_key_env`: `RE_PRINTFUL_API_TOKEN` (separate token so
  RE fulfillment and accounting stay isolated from PUSHROD).

Launch it: `BRAND=restorationessentials PORT=8092 ./.venv/bin/python backend/app.py`.
Two brands, two ports, one codebase.

## 2. Catalog slice

`backend/catalog.py` already filters by `sku_prefixes`. Feed it the unified
catalog (640 SKUs: PR-* 160 + RE's 480) and each store shows only its own
prefixes. The `owner` field on every product comes from the `OWNERSHIP`
partition map — add new prefixes there if the catalog ever grows a fifth line.

RE pricing: RE's price list plugs into the same `prices_json` slot. Same
honesty rule — SKUs with no confirmed price render "Price TBD" and are not
purchasable. Never invent RE prices either.

## 3. Separate Stripe config

- One Stripe account (or test-mode key set) per brand: `RE_STRIPE_TEST_SECRET_KEY`,
  `RE_STRIPE_WEBHOOK_SECRET`. The `sk_test_` refusal guard applies to RE too.
- Webhook endpoint per brand store (`/api/stripe/webhook` on each port) so
  `checkout.session.completed` fulfills against the right brand's mapping.
- `external_id` for RE orders: `re-<stripe-session-id>` (edit the prefix in
  `fulfillment/fulfill.py` or, better, read it from the brand yaml —
  small change, flagged here so it isn't forgotten).

## 4. Separate Printful mapping

Copy `fulfillment/printful_mapping.example.json` to
`fulfillment/re_printful_mapping.json`, set `PRINTFUL_MAPPING` to it when
running the RE brand, and map RE's SKUs to their Printful variants. RE's
product mix (restoration guides are digital — likely NOT Printful) may need
a second fulfillment path (digital delivery); the `fulfill.py` glue is the
seam — add a `fulfillment_type` per SKU (`print` vs `digital`) there.

## 5. The 4-door gateway (sits in front)

The gateway is live: `brands/gateway.yaml`, `BRAND=gateway PORT=8093`.
It loads the **unified** catalog (all 640 SKUs, sorted by SKU) and presents
four doors — PUSHROD Garage Gear (PR-*), Muscle Car (RE-MC-*), RestoMod
(RE-MP-*), Truck (RE-CT-*) — each a pre-filtered view of the one catalog.
Unlike the original sketch, the gateway carries the full cart + Stripe
(TEST-mode) checkout itself, so a shopper can buy across lines in one order;
the per-brand stores remain for brand-pure browsing. Door config lives in
the brand yaml (`brand.doors`); `store.js` renders the doors only when the
brand defines them.

## 6. Checklist — RE mirror (completed 2026-09-27)

- [x] `brands/restorationessentials.yaml` written (RE palette: shop gold + barn red on near-black)
- [x] Unified 640-SKU catalog in place, zero prefix collisions verified (test_unified.py)
- [x] RE price list confirmed — all 480 RE SKUs carry confirmed msrp, zero "Price TBD"
- [x] Live smoke: BRAND=restorationessentials on :8092 serves 480/480 purchasable
- [x] Gateway routing verified: 4 doors filter the 640 by prefix; every prefix lands correctly
- [ ] `RE_STRIPE_TEST_SECRET_KEY` set; test purchase + refund completed (keys not yet issued — checkout correctly 503s without them)
- [ ] `RE_PRINTFUL_API_TOKEN` generated (same dashboard steps as README) — needs a browser task with the saved Printful login
- [ ] `re_printful_mapping.json` covers every purchasable RE SKU (`PRINTFUL_MAPPING` env selects it; checkout refuses unmapped lines so nothing unshippable can be paid for)
- [ ] Digital-fulfillment path if RE ever sells non-Printful goods (all 640 current SKUs are Printful-type physical goods)
