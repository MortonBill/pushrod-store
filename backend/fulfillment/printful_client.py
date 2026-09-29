"""
PushRod storefront — Printful fulfillment module (REAL API, v2).

The store lives IN our sites. Printful is fulfillment-only:
  customer buys on our site -> backend creates the Printful order via API
  -> Printful prints and ships.

Auth: private API token from the Printful dashboard (Settings -> API),
read from the PRINTFUL_API_TOKEN env var. Never hardcoded, never logged.

API: https://developers.printful.com/docs/v2-beta/
  Base: https://api.printful.com/v2
  Auth: Authorization: Bearer <token>
  Orders: POST /v2/orders (recipient + order_items in one request)
  Confirm: POST /v2/orders/{id}/confirmation  (draft -> submitted)
  Status: GET /v2/orders/{id}  |  lookup: GET /v2/orders?external_id=...
  Shipping quote: POST /v2/shipping-rates
  Catalog: GET /v2/catalog-products , GET /v2/catalog-products/{id}/catalog-variants

There is NO mock mode on the default path. Without a token every method
raises PrintfulConfigError with instructions. (A PRINTFUL_DRY_RUN=1 escape
hatch exists for offline development only and is loudly logged.)
"""
import json
import logging
import os
import urllib.request
import urllib.error

log = logging.getLogger("pushrod.printful")

API_BASE = "https://api.printful.com/v2"


class PrintfulConfigError(RuntimeError):
    pass


class PrintfulAPIError(RuntimeError):
    def __init__(self, status, problem):
        self.status = status
        self.problem = problem
        detail = problem.get("detail") if isinstance(problem, dict) else problem
        super().__init__(f"Printful API {status}: {detail}")


class PrintfulClient:
    def __init__(self, api_token=None, dry_run=False):
        self.api_token = api_token or os.environ.get("PRINTFUL_API_TOKEN")
        self.dry_run = dry_run or os.environ.get("PRINTFUL_DRY_RUN") == "1"
        if not self.api_token and not self.dry_run:
            raise PrintfulConfigError(
                "PRINTFUL_API_TOKEN is not set. Generate a private API token in the "
                "Printful dashboard: sign in at printful.com -> Stores -> your store "
                "-> Settings -> API -> generate token, then export PRINTFUL_API_TOKEN."
            )
        if self.dry_run:
            log.warning("PRINTFUL DRY-RUN: no real API calls will be made.")

    # ---- low-level ----
    def _request(self, method, path, payload=None):
        if self.dry_run:
            log.warning("DRY-RUN %s %s payload=%s", method, path,
                        json.dumps(payload)[:500] if payload else None)
            return {"dry_run": True, "method": method, "path": path}
        url = API_BASE + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            try:
                problem = json.loads(e.read().decode())
            except Exception:
                problem = {"detail": e.reason}
            raise PrintfulAPIError(e.code, problem)

    # ---- catalog (for building the SKU -> variant mapping) ----
    def list_catalog_products(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return self._request("GET", "/catalog-products" + ("?" + qs if qs else ""))

    def list_catalog_variants(self, catalog_product_id):
        return self._request("GET", f"/catalog-products/{catalog_product_id}/catalog-variants")

    # ---- orders ----
    def create_order(self, recipient, order_items, external_id=None, confirm=False):
        """Create a Printful order (draft unless confirm=True).
        recipient: {name, address1, address2?, city, state_code, country_code, zip, phone?, email?}
        order_items: [{catalog_variant_id, quantity, placement, technique, file_url}]
        """
        payload = {
            "recipient": recipient,
            "order_items": [
                {
                    "catalog_variant_id": item["catalog_variant_id"],
                    "source": "catalog",
                    "quantity": item["quantity"],
                    "placements": [{
                        "placement": item.get("placement", "front"),
                        "technique": item.get("technique", "dtg"),
                        "layers": [{"type": "file", "url": item["file_url"]}],
                    }],
                }
                for item in order_items
            ],
        }
        if external_id:
            payload["external_id"] = external_id
        order = self._request("POST", "/orders", payload)
        if confirm and not self.dry_run:
            order_id = order["id"]
            self._request("POST", f"/orders/{order_id}/confirmation")
            return self.get_order(order_id)
        return order

    def confirm_order(self, order_id):
        return self._request("POST", f"/orders/{order_id}/confirmation")

    def get_order(self, order_id):
        return self._request("GET", f"/orders/{order_id}")

    def get_order_by_external_id(self, external_id):
        return self._request("GET", f"/orders?external_id={external_id}")

    def cancel_order(self, order_id):
        return self._request("DELETE", f"/orders/{order_id}")

    def shipping_rates(self, recipient, order_items):
        payload = {
            "recipient": recipient,
            "order_items": [
                {"catalog_variant_id": i["catalog_variant_id"], "quantity": i["quantity"]}
                for i in order_items
            ],
        }
        return self._request("POST", "/shipping-rates", payload)
