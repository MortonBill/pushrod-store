"""SportRoots billing (Lane 4) test: dark-by-default + the full dark-
enabled loop with Stripe stubbed.

Two halves, run as separate processes because app.py reads env at import:

  SR_TEST_MODE=off  SR_BILLING_ENABLED absent  -> routes 404, webhook
                      passes subscription events through untouched.
  SR_TEST_MODE=on   SR_BILLING_ENABLED=1       -> checkout/portal/
                      entitlement routes work against stubbed Stripe and
                      the webhook records/revokes entitlements.

Run: python3 test_subscriptions.py   (from backend/, runs both halves)
Stripe is stubbed (no network/keys); Printful runs PRINTFUL_DRY_RUN=1.
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PRINTFUL_DRY_RUN"] = "1"
os.environ.setdefault("BRAND", "skillforge")
os.environ["SKILLFORGE_STRIPE_TEST_SECRET_KEY"] = "sk_test_fake_for_tests"
os.environ["SKILLFORGE_STRIPE_WEBHOOK_SECRET"] = "whsec_test"

MODE = os.environ.get("SR_TEST_MODE", "off")
if MODE == "on":
    os.environ["SR_BILLING_ENABLED"] = "1"
    os.environ["SR_PRICE_MONTHLY"] = "price_monthly_test"
    os.environ["SR_PRICE_LIFETIME"] = "price_lifetime_test"
    os.environ["SR_ENTITLEMENT_SECRET"] = "test-secret"
    _store_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "sr_entitlements_test.json")
    if os.path.exists(_store_path):
        os.remove(_store_path)
    os.environ["SR_ENTITLEMENTS_PATH"] = _store_path

import app as store_app  # noqa: E402
import subscriptions as sr_mod  # noqa: E402

client = store_app.app.test_client()
failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {extra}" if extra else ""))
    if not cond:
        failures.append(name)


if MODE == "off":
    check("flag off: module reports disabled", not sr_mod.is_enabled())
    r = client.post("/api/sr/checkout", json={"plan": "monthly",
                                              "email": "a@b.c"})
    check("flag off: /api/sr/checkout is 404", r.status_code == 404,
          str(r.status_code))
    r = client.post("/api/sr/portal", json={"email": "a@b.c"})
    check("flag off: /api/sr/portal is 404", r.status_code == 404)
    r = client.get("/api/sr/entitlement?email=a@b.c&sig=x")
    check("flag off: /api/sr/entitlement is 404", r.status_code == 404)
    # Webhook: a subscription event must pass through with a plain ack and
    # record NOTHING (no entitlement store file created).
    event = {"type": "customer.subscription.updated",
             "data": {"object": {"id": "sub_1", "customer": "cus_1",
                                 "status": "active",
                                 "items": {"data": [{"price": {"id": "price_x"}}]}}}}
    store_app.stripe.Webhook.construct_event = lambda p, s, sec: event
    r = client.post("/api/stripe/webhook", data=b"{}", headers={
        "Content-Type": "application/json", "Stripe-Signature": "t=1,v1=x"})
    check("flag off: webhook acks subscription event", r.status_code == 200
          and r.get_json() == {"received": True}, r.get_data(as_text=True))
    check("flag off: no entitlement store created",
          not os.path.exists(sr_mod.default_store_path()))
    # Existing surface untouched.
    r = client.get("/api/products")
    check("flag off: /api/products still serves", r.status_code == 200)
    # /pricing stays honest opening-soon while billing is dark: no
    # purchase UI is served, no live-block bytes leak.
    r = client.get("/pricing", headers={"Host": "sportrootsdrills.com"})
    body = r.get_data(as_text=True)
    check("flag off: /pricing serves on SR host", r.status_code == 200,
          str(r.status_code))
    check("flag off: /pricing reads opening-soon", "OPENING SOON" in body)
    check("flag off: /pricing has no purchase CTAs",
          "data-sr-subscribe" not in body and "sr-portal-btn" not in body)
    check("flag off: /pricing leaks no live-block markers",
          "SR-LIVE-BEGIN" not in body and "SR-DARK-BEGIN" not in body)

else:
    check("flag on: module reports enabled", sr_mod.is_enabled())
    store_app.STRIPE_READY = True
    sr_mod._stripe_ready = True

    created = {}

    class FakeObj(dict):
        __getattr__ = dict.get

    def fake_session_create(**kw):
        created.clear()
        created.update(kw)
        s = FakeObj(id="cs_test_sr1", url="https://checkout.stripe.com/pay/cs_test_sr1")
        return s

    store_app.stripe.checkout.Session.create = fake_session_create
    store_app.stripe.Subscription = type("S", (), {
        "retrieve": staticmethod(lambda sid, **kw: FakeObj(
            id=sid, customer="cus_1", status="trialing",
            current_period_end=1799999999,
            items={"data": [{"price": {"id": "price_monthly_test"}}]}))})
    store_app.stripe.Customer = type("C", (), {
        "retrieve": staticmethod(lambda cid, **kw: FakeObj(
            id=cid, email="fam@example.com"))})
    store_app.stripe.billing_portal.Session.create = lambda **kw: FakeObj(
        url="https://billing.stripe.com/portal/test")

    # --- checkout: monthly trial shape ---
    r = client.post("/api/sr/checkout", json={"plan": "monthly",
                                              "email": "fam@example.com"})
    body = r.get_json()
    check("checkout monthly 200", r.status_code == 200, r.get_data(as_text=True))
    check("checkout monthly mode=subscription", created.get("mode") == "subscription")
    check("checkout monthly price", created["line_items"][0]["price"] == "price_monthly_test")
    check("checkout monthly trial 30d",
          created["subscription_data"]["trial_period_days"] == 30)
    check("checkout monthly no card up front",
          created.get("payment_method_collection") == "if_required")
    check("checkout monthly metadata kind",
          created["metadata"].get("kind") == "sr_sub")
    check("checkout monthly no automatic_tax in test mode",
          "automatic_tax" not in created)
    check("checkout monthly no Connect header", "stripe_account" not in created)

    # --- checkout: unconfigured plan + bad input ---
    r = client.post("/api/sr/checkout", json={"plan": "club",
                                              "email": "fam@example.com"})
    check("checkout club unset price -> 503", r.status_code == 503,
          r.get_data(as_text=True))
    r = client.post("/api/sr/checkout", json={"plan": "nope", "email": "x@y.z"})
    check("checkout unknown plan -> 400", r.status_code == 400)
    r = client.post("/api/sr/checkout", json={"plan": "monthly", "email": "bad"})
    check("checkout bad email -> 400", r.status_code == 400)

    # --- checkout: lifetime is payment mode ---
    r = client.post("/api/sr/checkout", json={"plan": "lifetime",
                                              "email": "fam@example.com"})
    check("checkout lifetime mode=payment", created.get("mode") == "payment")
    check("checkout lifetime no subscription_data",
          "subscription_data" not in created)

    # --- webhook: subscription checkout completion records entitlement ---
    def post_event(event):
        store_app.stripe.Webhook.construct_event = lambda p, s, sec: event
        return client.post("/api/stripe/webhook", data=b"{}", headers={
            "Content-Type": "application/json", "Stripe-Signature": "t=1,v1=x"})

    r = post_event({"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_test_sr1", "mode": "subscription",
        "customer": "cus_1", "subscription": "sub_1",
        "customer_details": {"email": "fam@example.com"},
        "metadata": {"kind": "sr_sub", "plan": "monthly"}}}})
    check("webhook sr checkout 200", r.status_code == 200, r.get_data(as_text=True))
    sig = hmac.new(b"test-secret", b"fam@example.com", hashlib.sha256).hexdigest()
    r = client.get(f"/api/sr/entitlement?email=fam@example.com&sig={sig}")
    body = r.get_json()
    check("entitlement trialing = entitled",
          body["entitled"] is True and body["status"] == "trialing", str(body))
    check("entitlement plan resolved from price", body["plan"] == "monthly")
    r = client.get("/api/sr/entitlement?email=fam@example.com&sig=deadbeef")
    check("entitlement bad sig -> 403", r.status_code == 403)

    # --- webhook: subscription deleted revokes ---
    r = post_event({"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "canceled",
        "items": {"data": [{"price": {"id": "price_monthly_test"}}]}}}})
    r = client.get(f"/api/sr/entitlement?email=fam@example.com&sig={sig}")
    body = r.get_json()
    check("entitlement canceled = not entitled",
          body["entitled"] is False and body["status"] == "canceled", str(body))

    # --- webhook: invoice.paid re-activates ---
    r = post_event({"type": "invoice.paid", "data": {"object": {
        "customer": "cus_1", "subscription": "sub_1",
        "customer_email": "fam@example.com"}}})
    r = client.get(f"/api/sr/entitlement?email=fam@example.com&sig={sig}")
    check("invoice.paid reactivates", r.get_json()["entitled"] is True)

    # --- charge.refunded revokes (rides the existing branch) ---
    r = post_event({"type": "charge.refunded", "data": {"object": {
        "id": "ch_1", "customer": "cus_1", "amount_refunded": 1299,
        "metadata": {}}}})
    check("webhook refund 200", r.status_code == 200, r.get_data(as_text=True))
    r = client.get(f"/api/sr/entitlement?email=fam@example.com&sig={sig}")
    body = r.get_json()
    check("refund revokes entitlement",
          body["entitled"] is False and body["status"] == "refunded", str(body))

    # --- portal: known vs unknown email ---
    r = client.post("/api/sr/portal", json={"email": "fam@example.com"})
    check("portal known email", r.status_code == 200
          and "billing.stripe.com" in r.get_json()["portal_url"],
          r.get_data(as_text=True))
    r = client.post("/api/sr/portal", json={"email": "ghost@example.com"})
    check("portal unknown email -> 404", r.status_code == 404)

    # --- portal: restricted-key Permission denied fails closed ---
    # 2026-10-09 go-live proof (Step B): the live restricted key the app
    # loads cannot create Customer Portal sessions — Stripe answered
    # Permission denied and the raw exception text (which embeds the
    # key fingerprint fragment) was served to the buyer. The route must
    # fail closed instead: HTTP 502, the exact approved product copy,
    # and no leak of the raw exception text or the key fragment anywhere
    # in the response body.
    _raw_exc = ("Permission denied. The provided key 'rk_live_...FSNK' "
                "does not have the required permissions for this "
                "endpoint on account acct_test.")

    _exc = store_app.stripe.error.PermissionError(_raw_exc)

    def _denied_portal(**kw):
        raise _exc

    _orig_portal = store_app.stripe.billing_portal.Session.create
    store_app.stripe.billing_portal.Session.create = _denied_portal
    try:
        r = client.post("/api/sr/portal", json={"email": "fam@example.com"})
        _body = r.get_data(as_text=True) or ""
        _json = r.get_json() or {}
        _err = _json.get("error") or ""
        _approved = (
            "Manage billing is blocked: Stripe refused access with "
            "the current key (Permission denied; billing portal write "
            "required). Nothing changed and no new key was invented. "
            "The Stripe customer portal will open here once the "
            "payment key is authorized for billing.")
        check("portal denied: 502", r.status_code == 502,
              str(r.status_code))
        check("portal denied: approved copy verbatim",
              _err == (
                  "<p style=\"max-width:34em;margin:2em auto;font:15px/1.5 "
                  "-apple-system,system-ui,sans-serif;color:#333\">"
                  "Manage billing is blocked: Stripe refused access with "
                  "the current key (Permission denied; billing portal "
                  "write required). Nothing changed and no new key was "
                  "invented. The Stripe customer portal will open here "
                  "once the payment key is authorized for billing.</p>")
              and _approved in _err.replace("&quot;", '"'), _err[:80])
        check("portal denied: no raw exception leak",
              "rk_live" not in _body and "FSNK" not in _body
              and "The provided key" not in _body, _body[:120])
    finally:
        store_app.stripe.billing_portal.Session.create = _orig_portal

    # --- regression (2026-10-09 go-live proof): multi-worker store ---
    # gunicorn runs --workers 2 (render.yaml): the webhook write lands in
    # one worker's EntitlementStore, the buyer's /api/sr/portal lookup in
    # the other's. The store used to load once at init and never re-read,
    # so worker B answered "no SportRoots subscription found" forever.
    _workers_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "sr_entitlements_workers_test.json")
    if os.path.exists(_workers_path):
        os.remove(_workers_path)
    worker_a = sr_mod.EntitlementStore(_workers_path)
    worker_b = sr_mod.EntitlementStore(_workers_path)
    worker_a.upsert("two@example.com", customer_id="cus_2",
                    subscription_id="sub_2", status="trialing",
                    plan="monthly")
    seen = worker_b.get_by_email("two@example.com")
    check("regression: second worker sees first worker's write",
          bool(seen) and seen.get("customer_id") == "cus_2", str(seen))
    if os.path.exists(_workers_path):
        os.remove(_workers_path)

    # --- regression (2026-10-09 go-live proof): buyer return path ---
    # success.html calls GET /api/fulfill?session_id=... for EVERY
    # checkout, subscriptions included. The old handler ran the one-time
    # fulfillment on the session's empty cart: fulfill_paid_order
    # returned None and result.get() raised outside the caught set, so
    # the buyer got an HTML 500 ("Unexpected token '<' ... is not valid
    # JSON") and NO synchronous entitlement was ever recorded. The SR
    # branch must record from the returned session and answer JSON, so
    # Manage/portal works the moment the buyer lands.
    store_app.stripe.checkout.Session.retrieve = staticmethod(
        lambda sid, **kw: FakeObj(
            id=sid, payment_status="paid", mode="subscription",
            status="complete", customer="cus_1", subscription="sub_1",
            customer_details={"email": "sync@example.com"},
            metadata={"kind": "sr_sub", "plan": "monthly"}))
    try:
        r = client.get("/api/fulfill?session_id=cs_test_sr_return")
        ok_json = (r.status_code == 200
                   and r.content_type.startswith("application/json")
                   and bool((r.get_json() or {}).get("sportroots")))
        fulfill_extra = f"{r.status_code} {r.content_type}"
    except Exception as e:  # old code: uncaught AttributeError
        ok_json, fulfill_extra = False, f"raised {e!r}"
    check("regression: /api/fulfill SR session answers JSON, records sync",
          ok_json, fulfill_extra)
    r = client.post("/api/sr/portal", json={"email": "sync@example.com"})
    check("regression: portal finds buyer right after success return",
          r.status_code == 200, r.get_data(as_text=True))

    # --- /pricing serves live purchase CTAs when billing is enabled ---
    r = client.get("/pricing", headers={"Host": "sportrootsdrills.com"})
    body = r.get_data(as_text=True)
    check("flag on: /pricing serves on SR host", r.status_code == 200,
          str(r.status_code))
    for plan in ("monthly", "annual", "club", "lifetime"):
        check(f"flag on: /pricing subscribe CTA for {plan}",
              f'data-sr-subscribe="{plan}"' in body)
    check("flag on: /pricing has portal/manage entry",
          "sr-portal-btn" in body)
    check("flag on: /pricing posts to the SR API",
          '"/api/sr/checkout"' in body and '"/api/sr/portal"' in body)
    check("flag on: /pricing has no dark copy",
          "OPENING SOON" not in body and "planned price" not in body)
    check("flag on: /pricing leaks no dark-block markers",
          "SR-DARK-BEGIN" not in body and "SR-LIVE-BEGIN" not in body)

    # --- one-time checkout untouched by all of this ---
    r = client.get("/api/products")
    check("flag on: /api/products still serves", r.status_code == 200)
    if os.path.exists(_store_path):
        os.remove(_store_path)

print(f"\n{len(failures)} failure(s) in mode={MODE}")
sys.exit(1 if failures else 0)
