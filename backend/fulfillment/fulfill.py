"""
Order fulfillment glue: paid cart -> Printful order (print) and/or
emailed download links (digital).

Called AFTER Stripe confirms payment (webhook checkout.session.completed in
production; the /api/fulfill endpoint in v1 local testing). Print lines are
converted into Printful order items via printful_mapping.json and submitted
as a CONFIRMED order so Printful prints and ships without further action.
Digital lines (catalog fulfillment_type=digital) go through
fulfillment.digital: one email with signed, expiring download links.

Every fulfilled print order gets our external_id: "pushrod-<stripe-session-id>"
so it is idempotent and traceable in both systems. Digital delivery is
idempotent via the digital ledger (see fulfillment/digital.py).
"""
import json
import logging
import os

from .printful_client import PrintfulClient, PrintfulConfigError
from . import digital as digital_mod

log = logging.getLogger("pushrod.fulfill")


def load_mapping(mapping_path):
    if not os.path.exists(mapping_path):
        return {}
    with open(mapping_path) as f:
        data = json.load(f)
    return data.get("mappings", {})


def mapping_key(sku, size):
    return f"{sku}:{size}" if size else sku


def build_order_items(cart_lines, products_by_sku, mapping):
    """cart_lines: [{sku, size|None, qty}]. Raises ValueError listing any
    SKU+size with no Printful mapping so the failure is explicit, not silent.

    A mapping entry with a null/empty catalog_variant_id or print_file_url
    (placeholder awaiting the API fill) counts as unmapped — it can never
    produce a shippable order."""
    items, unmapped = [], []
    for line in cart_lines:
        key = mapping_key(line["sku"], line.get("size"))
        m = mapping.get(key)
        if not m:
            unmapped.append(key)
            continue
        if not m.get("catalog_variant_id") or not m.get("print_file_url"):
            unmapped.append(f"{key} (mapping incomplete — no variant/file yet)")
            continue
        items.append({
            "catalog_variant_id": m["catalog_variant_id"],
            "quantity": line["qty"],
            "placement": m.get("placement", "front"),
            "technique": m.get("technique", "dtg"),
            "file_url": m["print_file_url"],
        })
    if unmapped:
        raise ValueError(
            "No Printful mapping for: " + ", ".join(unmapped) +
            ". Add them to printful_mapping.json (see printful_mapping.example.json)."
        )
    return items


def split_cart_lines(cart_lines, products_by_sku):
    """Partition cart lines into (print_lines, digital_lines) by each
    product's fulfillment_type (catalog seam — see catalog.load_catalog).

    Unknown SKUs count as PRINT: the Printful path then raises its explicit
    "no mapping" error exactly as before, instead of a digital line
    vanishing into an email nobody can fulfill."""
    print_lines, digital_lines = [], []
    for line in cart_lines:
        product = products_by_sku.get(line["sku"]) or {}
        if (product.get("fulfillment_type") or "print").strip().lower() == "digital":
            digital_lines.append(line)
        else:
            print_lines.append(line)
    return print_lines, digital_lines


def fulfill_paid_order(stripe_session_id, customer_email, shipping_address,
                       cart_lines, products_by_sku, mapping_path,
                       order_prefix="pushrod", download_base_url=None,
                       store_name="", digital_ledger_path=None):
    """Fulfill a paid Stripe session: Printful order for print lines,
    emailed download links for digital lines (mixed carts do both).

    Returns the Printful order payload when the cart is print-only (the
    original contract); when digital lines are present returns
    {"printful": <order|None>, "digital": <summary>} instead."""
    print_lines, digital_lines = split_cart_lines(cart_lines, products_by_sku)

    digital_result = None
    if digital_lines:
        if not download_base_url:
            raise ValueError(
                "digital lines in cart but no download_base_url passed — "
                "the caller must supply the public base URL so download "
                "links can be built")
        ledger = (digital_mod.DigitalLedger(digital_ledger_path)
                  if digital_ledger_path else None)
        digital_result = digital_mod.fulfill_digital_lines(
            stripe_session_id, customer_email, digital_lines,
            products_by_sku, download_base_url, store_name=store_name,
            ledger=ledger)

    order = None
    if print_lines:
        mapping = load_mapping(mapping_path)
        order_items = build_order_items(print_lines, products_by_sku, mapping)
        recipient = {
            "name": shipping_address.get("name", ""),
            "address1": shipping_address.get("line1", ""),
            "city": shipping_address.get("city", ""),
            "state_code": shipping_address.get("state", ""),
            "country_code": shipping_address.get("country", "US"),
            "zip": shipping_address.get("postal_code", ""),
            "email": customer_email,
        }
        if shipping_address.get("line2"):
            recipient["address2"] = shipping_address["line2"]
        if shipping_address.get("phone"):
            recipient["phone"] = shipping_address["phone"]

        client = PrintfulClient()  # raises PrintfulConfigError without a token
        external_id = f"{order_prefix}-{stripe_session_id}"[:32]
        order = client.create_order(
            recipient=recipient,
            order_items=order_items,
            external_id=external_id,
            confirm=True,  # draft -> submitted: Printful prints and ships
        )
        log.info("Printful order created for %s: %s", stripe_session_id,
                 order.get("id"))

    if digital_result is not None:
        return {"printful": order, "digital": digital_result}
    return order
