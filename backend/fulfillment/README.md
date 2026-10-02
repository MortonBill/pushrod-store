# Fulfillment — print (Printful) + digital (signed downloads)

Two fulfillment paths, split per cart line by the product's
`fulfillment_type` (catalog seam, set in the catalog CSV):

| fulfillment_type | Path | Trigger |
|---|---|---|
| `print` (default) | Printful order via `fulfill.py` | `checkout.session.completed` |
| `digital` | Emailed signed download link via `digital.py` | `checkout.session.completed` |

Mixed carts do both. The split lives in `fulfill.split_cart_lines()`;
unknown SKUs count as print so they hit the existing loud "no Printful
mapping" error instead of disappearing.

## The catalog seam

A catalog CSV row may carry two extra columns (both optional; rows without
them are print goods, unchanged):

- `fulfillment_type` — `print` | `digital` (aliases: `fulfillment`).
  Anything unrecognized falls back to `print`.
- `digital_file` — filename of the deliverable (aliases: `digital_path`,
  `download_file`). Only the basename is ever used; the file itself lives
  in the digital-files directory (see env below).

Purchasability mirrors the honest-purchasability rule for print: a digital
SKU is sellable only with a confirmed price AND a `digital_file`. A priced
digital SKU with no file renders unavailable — a customer can never pay
for a download we don't have.

## Digital delivery chain

On `checkout.session.completed` (`digital.fulfill_digital_lines`):

1. **Signed link** — one per digital SKU: `<PUBLIC_BASE_URL>/download/<token>`.
   The token is HMAC-SHA256 over `{sku, buyer email, expiry}`, verified by
   the `/download` endpoint, which streams the file as an attachment.
   Tokens are never logged.
2. **Email** — one Brevo email per order listing every link, from
   bill@aitoolsfortoday.com. Without `BREVO_API_KEY` the sender is in
   DRY-RUN: the email is logged in full and ledgered as dry-run, never sent.
3. **Kit tag** — buyer tagged via the Kit API when configured; otherwise
   recorded `queued` in the ledger. Tagging never blocks delivery.

**Idempotency:** fulfilled session ids persist in the digital ledger
(JSON, atomic writes). A duplicate `checkout.session.completed` for the
same session returns `already_fulfilled` and sends NO second email.

## Environment variables (names only — set values in Render)

| Var | Required for | Notes |
|---|---|---|
| `DIGITAL_DOWNLOAD_SECRET` | minting/verifying links | Hard fail without it (webhook 500s, Stripe retries). Set once; rotating invalidates outstanding links. |
| `DIGITAL_LINK_TTL_DAYS` | link expiry | Default 7. |
| `DIGITAL_FILES_DIR` | file storage | Default `data/digital` under the project root. Only basenames from the catalog are served. |
| `DIGITAL_FULFILLMENTS_PATH` | idempotency ledger | Default `digital_fulfillments.json` beside `digital.py`. |
| `PUBLIC_BASE_URL` | link host | Optional; falls back to the request host. Set when behind a proxy/custom domain. |
| `BREVO_API_KEY` | delivery email | Missing => dry-run (logged, ledgered, not sent). |
| `BREVO_SENDER_EMAIL` / `BREVO_SENDER_NAME` | email identity | Defaults bill@aitoolsfortoday.com / "AI Tools for Today". |
| `KIT_API_KEY` + `KIT_BUYER_TAG_ID` | buyer tagging | Either missing => tag queued in the ledger, delivery unaffected. |

## Refund runbook (honest version)

- **Refunds are issued by a human in the Stripe dashboard** (Bill's login):
  Payments → the charge → Refund. Code never auto-refunds; a test purchase
  is refunded in the same session it was made.
- On `charge.refunded` the webhook **logs the event and flags the session**
  in the digital ledger (`refunded: true`) when the checkout session id is
  on the charge metadata. That is bookkeeping, not enforcement.
- **A delivered download cannot be recalled.** There is no file recall —
  anyone who saved the PDF keeps it. Do not tell a customer otherwise.
  What actually limits exposure:
  - links expire (`DIGITAL_LINK_TTL_DAYS`, default 7 days) and are bound
    to the buyer's email in the signed token;
  - re-issuing a link is a manual support act (mint a fresh token for the
    same sku+email), never automatic after a refund;
  - if a refunded buyer keeps using a still-live link until expiry, that
    is the accepted cost of digital goods — same as every PDF store.
- Print goods: a refund does NOT cancel a Printful order by itself. If the
  order hasn't printed, cancel it in Printful the same hour; if it has,
  the refund is a write-off. Check the Printful order status before
  refunding print items.
