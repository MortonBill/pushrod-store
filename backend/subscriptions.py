"""
SportRoots subscription billing (checkout migration Lane 4, 2026-10-03).

SportRoots is the one lane that isn't "one-time payment -> file delivery":
it sells subscriptions (Stripe Billing) plus a one-time Lifetime tier.
This module adds that machinery to the shared store backend:

  POST /api/sr/checkout     {plan, email} -> Stripe Checkout session
                            (mode=subscription for monthly/annual/club,
                            mode=payment for lifetime)
  POST /api/sr/portal       {email} -> Stripe Customer Portal session
                            (cancel / pause / card update — self-serve,
                            replacing the broken Polsia cancel path for good)
  GET  /api/sr/entitlement  ?email=&sig= -> {entitled, status, plan, ...}
                            HMAC-signed read for the Polsia front-end's
                            "is this family subscribed?" login check.

Entitlement truth lives in a JSON store (SR_ENTITLEMENTS_PATH) that the
Stripe webhook keeps current: checkout.session.completed (kind=sr_sub),
invoice.paid, customer.subscription.updated/deleted, and charge.refunded
all upsert the buyer's record. The Polsia site never talks to Stripe —
it asks this service, which is the whole point of the cutover.

DARK BY DEFAULT: everything here is inert unless SR_BILLING_ENABLED=1.
Flag off => routes are never registered (Flask 404s them), webhook
events pass through untouched, and nothing about the live one-time
checkout changes. Prices are NOT in code: each plan reads its Stripe
Price id from env (SR_PRICE_MONTHLY / SR_PRICE_ANNUAL / SR_PRICE_CLUB /
SR_PRICE_LIFETIME) — creating those Billing products/prices is a
separate, separately-authorized step (see
~/workspace/your_files/business/sportroots-billing-staging-2026-10-03.md).

Trial semantics per the LOCKED SportRoots pricing packet (Bill
2026-10-03, sportroots-pricing-packet-reconciled-2026-10-03.md):
Plus Monthly $4.99/mo and Plus Annual $39/yr carry a 30-day free
start with NO card up front (payment_method_collection=if_required);
Club $99/yr has no trial (card required, charged immediately);
Lifetime $99 is a plain one-time Checkout payment.
"""
import hashlib
import hmac
import json
import logging
import os
import time

import stripe
from flask import jsonify, request

log = logging.getLogger("pushrod.sportroots")

# Statuses that mean "this family may watch". past_due is deliberately
# NOT entitled here: dunning (Smart Retries + portal card update) is
# Stripe's job, and the entitlement read reports the raw status so the
# front-end can show a grace-period message instead of a hard wall.
ENTITLED_STATUSES = {"trialing", "active"}

# plan -> (price env var, checkout mode, trial days). Amounts live in
# Stripe Prices, never in code (pricing policy: never invent prices).
PLANS = {
    "monthly": {"price_env": "SR_PRICE_MONTHLY", "mode": "subscription",
                "trial_days": 30},
    "annual": {"price_env": "SR_PRICE_ANNUAL", "mode": "subscription",
               "trial_days": 30},
    "club": {"price_env": "SR_PRICE_CLUB", "mode": "subscription",
             "trial_days": 0},
    "lifetime": {"price_env": "SR_PRICE_LIFETIME", "mode": "payment",
                 "trial_days": 0},
}

_enabled = False
_stripe_ready = False
_stripe_mode = "test"
_stripe_acct = lambda: {}  # noqa: E731 — replaced by init()
_public_base_url = lambda: ""  # noqa: E731 — replaced by init()
_store = None


def _flag_on():
    return os.environ.get("SR_BILLING_ENABLED", "").strip().lower() \
        in ("1", "true", "yes", "on")


def is_enabled():
    return _enabled


# ---------- entitlement store ----------

def default_store_path():
    override = os.environ.get("SR_ENTITLEMENTS_PATH", "")
    if override:
        return override
    # Deployed service: entitlements are billing truth and must survive
    # redeploys, so prefer the persistent disk when one is mounted
    # (Render mounts it at /var/data — same pattern as app._img_lib_dir).
    # Next to this module is the fallback for local runs/tests.
    if os.path.isdir("/var/data"):
        return "/var/data/sr_entitlements.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "sr_entitlements.json")


class EntitlementStore:
    """JSON record of SportRoots subscription entitlements.

    Shape: {"by_email": {email: record}, "by_customer": {cus_id: email}}.
    Same discipline as fulfillment.digital.DigitalLedger: atomic writes
    (tmp file + os.replace) so a crash mid-write can't corrupt the file.
    On the deployed service this path MUST sit on the persistent disk
    (SR_ENTITLEMENTS_PATH=/var/data/sr_entitlements.json) or entitlements
    vanish on every redeploy.
    """

    def __init__(self, path=None):
        self.path = path or default_store_path()
        self._data = {"by_email": {}, "by_customer": {}}
        self._load()

    def _load(self):
        """(Re)read the store file. The deployed service runs multiple
        gunicorn workers, each holding its own EntitlementStore: a
        webhook write in one worker MUST be visible to a portal or
        entitlement read in another, so reads/writes always start from
        what's on disk, never from a boot-time snapshot. (2026-10-09
        go-live proof: worker B answered "no SportRoots subscription
        found" forever because it never re-read worker A's write.)"""
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = {
                        "by_email": loaded.get("by_email", {}),
                        "by_customer": loaded.get("by_customer", {}),
                    }
            except (ValueError, OSError):
                log.warning("SR entitlement store unreadable at %s — "
                            "starting empty (subscribers will re-record "
                            "on their next billing event)", self.path)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=1)
        os.replace(tmp, self.path)

    def get_by_email(self, email):
        if not email:
            return None
        self._load()
        return self._data["by_email"].get(email.strip().lower())

    def get_by_customer(self, customer_id):
        self._load()
        email = self._data["by_customer"].get(customer_id or "")
        return self.get_by_email(email) if email else None

    def upsert(self, email, **fields):
        """Create/merge the record for `email`; keeps the customer index
        in sync. Returns the merged record."""
        email = (email or "").strip().lower()
        if not email:
            return None
        self._load()
        rec = dict(self._data["by_email"].get(email) or {"email": email})
        rec.update({k: v for k, v in fields.items() if v is not None})
        rec["email"] = email
        rec["updated_ts"] = int(time.time())
        # Entitlement is ALWAYS derived from status, never set by hand —
        # one rule, no drift between what webhooks wrote and what the
        # entitlement endpoint answers.
        rec["entitled"] = rec.get("status") in ENTITLED_STATUSES
        self._data["by_email"][email] = rec
        if rec.get("customer_id"):
            self._data["by_customer"][rec["customer_id"]] = email
        self._save()
        return rec


def _plan_for_price(price_id):
    for plan, cfg in PLANS.items():
        if price_id and os.environ.get(cfg["price_env"], "") == price_id:
            return plan
    return None


# ---------- webhook handlers (called from app.stripe_webhook) ----------

def _subscription_fields(sub, sget):
    """Pull the entitlement fields off a Subscription StripeObject."""
    items = sget(sub, "items", {}) or {}
    data = sget(items, "data", []) or []
    price_id = None
    if data:
        price = sget(data[0], "price", {}) or {}
        price_id = sget(price, "id")
    status = sget(sub, "status", "") or ""
    return {
        "subscription_id": sget(sub, "id"),
        "customer_id": sget(sub, "customer"),
        "status": status,
        "price_id": price_id,
        "plan": _plan_for_price(price_id),
        "current_period_end": sget(sub, "current_period_end"),
    }


def _email_for_customer(customer_id, sget):
    """Resolve a buyer email for a billing event: local index first,
    Stripe Customer lookup second (billing events don't always carry
    the email themselves)."""
    if not customer_id:
        return ""
    rec = _store.get_by_customer(customer_id) if _store else None
    if rec:
        return rec["email"]
    try:
        cust = stripe.Customer.retrieve(customer_id, **_stripe_acct())
        return sget(cust, "email", "") or ""
    except Exception:  # noqa: BLE001 — email resolution must not kill the webhook
        log.warning("SR: could not resolve email for customer %s",
                    customer_id)
        return ""


def record_checkout_session(obj, sget):
    """Record the entitlement from a completed Checkout Session object —
    one rule whether the object arrived as a webhook payload or was
    re-retrieved on the buyer's success return. Returns the summary
    dict, or None when the session is not a SportRoots one (or billing
    is dark)."""
    if not _enabled or _store is None:
        return None
    meta = sget(obj, "metadata", {}) or {}
    if sget(meta, "kind") != "sr_sub":
        return None
    email = sget(sget(obj, "customer_details", {}) or {}, "email", "") \
        or sget(obj, "customer_email", "") or ""
    plan = sget(meta, "plan", "") or ""
    if sget(obj, "mode") == "payment":
        # Lifetime: one-time payment, entitled forever, no period.
        rec = _store.upsert(
            email, customer_id=sget(obj, "customer"),
            subscription_id=None, plan=plan or "lifetime",
            status="lifetime", current_period_end=None,
            source="checkout.session.completed")
        # Lifetime bypasses the status rule above (there is no
        # subscription status to derive from).
        rec["entitled"] = True
        _store._save()
        return {"plan": plan, "status": "lifetime"}
    # Recurring: the session names the subscription; read its real
    # status (trialing right after a trial signup) from Stripe.
    fields = {"plan": plan, "source": "checkout.session.completed"}
    sub_id = sget(obj, "subscription")
    try:
        sub = stripe.Subscription.retrieve(sub_id, **_stripe_acct())
        fields.update(_subscription_fields(sub, sget))
    except Exception:  # noqa: BLE001 — invoice.paid will land next and fix it
        log.warning("SR: subscription %s unreadable at checkout "
                    "completion — invoice.paid will record it", sub_id)
        fields.update({"subscription_id": sub_id,
                       "customer_id": sget(obj, "customer"),
                       "status": "unknown"})
    rec = _store.upsert(email, **fields)
    return {"plan": plan, "status": (rec or {}).get("status")}


def sync_from_checkout_session(session_id, sget):
    """Synchronous twin of the webhook's checkout.session.completed
    path, driven by the buyer's return to /checkout/success: retrieve
    the session and record the entitlement NOW, so Manage/portal and
    entitlement reads work the moment the buyer lands — not whenever
    the webhook happens to arrive (and whichever worker it lands on).
    The webhook stays the lifecycle writer (renewals, cancels, refunds);
    this only front-runs the same record for the signup itself.
    Returns the summary dict, or None when dark/not ours/unreadable."""
    if not _enabled or _store is None or not session_id:
        return None
    try:
        session = stripe.checkout.Session.retrieve(session_id,
                                                   **_stripe_acct())
    except Exception:  # noqa: BLE001 — success page must never crash on it
        log.warning("SR: could not retrieve checkout session %s for "
                    "success-page sync", session_id)
        return None
    status = sget(session, "status", "") or ""
    if status and status != "complete":
        return None
    return record_checkout_session(session, sget)


def handle_event(event, sget):
    """Route one verified webhook event. Returns a summary dict when the
    event belongs to SportRoots billing, None to let the caller fall
    through to the one-time-payment handlers. No-ops when disabled.

    `sget` is app._sget — the only safe StripeObject read pattern in
    this codebase (stripe-python 15.x raises on .get()/missing attrs).
    """
    if not _enabled or _store is None:
        return None
    etype = sget(event, "type", "")
    obj = sget(sget(event, "data", {}) or {}, "object", {}) or {}

    if etype == "checkout.session.completed":
        meta = sget(obj, "metadata", {}) or {}
        if sget(meta, "kind") != "sr_sub":
            return None
        return record_checkout_session(obj, sget)

    if etype == "invoice.paid":
        customer_id = sget(obj, "customer")
        email = sget(obj, "customer_email", "") \
            or _email_for_customer(customer_id, sget)
        fields = {"customer_id": customer_id, "source": "invoice.paid"}
        sub_id = sget(obj, "subscription")
        try:
            sub = stripe.Subscription.retrieve(sub_id, **_stripe_acct())
            fields.update(_subscription_fields(sub, sget))
        except Exception:  # noqa: BLE001 — a paid invoice still means paid
            log.warning("SR: subscription %s unreadable on invoice.paid "
                        "— recording active on the invoice's word", sub_id)
            fields.update({"subscription_id": sub_id, "status": "active"})
        rec = _store.upsert(email, **fields)
        return {"status": (rec or {}).get("status")}

    if etype in ("customer.subscription.updated",
                 "customer.subscription.deleted"):
        customer_id = sget(obj, "customer")
        email = _email_for_customer(customer_id, sget)
        fields = _subscription_fields(obj, sget)
        if etype == "customer.subscription.deleted":
            fields["status"] = "canceled"
        fields["source"] = etype
        rec = _store.upsert(email, **fields)
        return {"status": (rec or {}).get("status")}

    return None


def handle_charge_refunded(charge, sget):
    """A refunded subscription charge revokes entitlement. Called from
    app.py's existing charge.refunded branch (which keeps its digital-
    ledger flagging); no-op when disabled or the charge isn't ours."""
    if not _enabled or _store is None:
        return None
    customer_id = sget(charge, "customer")
    rec = _store.get_by_customer(customer_id) if customer_id else None
    if not rec:
        return None
    return _store.upsert(rec["email"], status="refunded",
                         source="charge.refunded")


# ---------- routes ----------

def _price_for(plan):
    cfg = PLANS.get(plan or "")
    if not cfg:
        return None, None
    return cfg, os.environ.get(cfg["price_env"], "").strip()


def init(app, stripe_ready, stripe_mode, stripe_acct, public_base_url):
    """Wire SportRoots billing into the store app (or don't).

    Flag off (production default): no routes registered, handlers no-op,
    existing checkout is byte-for-byte the behavior it was before.
    """
    global _enabled, _stripe_ready, _stripe_mode, _stripe_acct
    global _public_base_url, _store
    _stripe_ready = stripe_ready
    _stripe_mode = stripe_mode
    _stripe_acct = stripe_acct
    _public_base_url = public_base_url
    _enabled = _flag_on()
    if not _enabled:
        log.info("sportroots billing: DISABLED (SR_BILLING_ENABLED off) "
                 "— routes inert")
        return
    _store = EntitlementStore()
    log.warning("sportroots billing: ENABLED — /api/sr/* routes live "
                "(store: %s)", _store.path)

    @app.post("/api/sr/checkout")
    def sr_checkout():
        if not _stripe_ready:
            return jsonify({"error": "Stripe not configured"}), 503
        data = request.get_json(force=True, silent=True) or {}
        plan = (data.get("plan") or "").strip().lower()
        email = (data.get("email") or "").strip()
        cfg, price_id = _price_for(plan)
        if cfg is None:
            return jsonify({"error": f"unknown plan '{plan}' — expected "
                                     f"one of {', '.join(PLANS)}"}), 400
        if not price_id:
            return jsonify({"error": f"plan '{plan}' is not configured "
                                     f"({cfg['price_env']} unset)"}), 503
        if not email or "@" not in email:
            return jsonify({"error": "a valid email is required — it "
                                     "becomes the subscription's "
                                     "entitlement key"}), 400
        base = _public_base_url()
        kwargs = dict(
            mode=cfg["mode"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email,
            metadata={"kind": "sr_sub", "plan": plan},
            success_url=base + "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base + "/checkout/cancel",
            **_stripe_acct(),
        )
        if cfg["mode"] == "subscription":
            if cfg["trial_days"]:
                # Locked packet (2026-10-03): 30-day free start, NO
                # card up front — payment_method_collection=if_required
                # so Checkout does not demand a card for a $0-due trial.
                kwargs["payment_method_collection"] = "if_required"
                kwargs["subscription_data"] = {
                    "trial_period_days": cfg["trial_days"],
                    "metadata": {"kind": "sr_sub", "plan": plan},
                }
            else:
                # No-trial subscriptions (club) charge immediately —
                # a payment method is required at Checkout.
                kwargs["payment_method_collection"] = "always"
        # Stripe Tax, same rule as the one-time checkout: live only.
        if _stripe_mode == "live":
            kwargs["automatic_tax"] = {"enabled": True}
        try:
            session = stripe.checkout.Session.create(**kwargs)
        except stripe.error.StripeError as e:
            log.warning("SR checkout Session.create failed: %r", e)
            msg = getattr(e, "user_message", None) or str(e) or "checkout failed"
            return jsonify({"error": f"Stripe error: {msg}"}), 502
        return jsonify({"checkout_url": session.url,
                        "session_id": session.id, "plan": plan})

    @app.post("/api/sr/portal")
    def sr_portal():
        if not _stripe_ready:
            return jsonify({"error": "Stripe not configured"}), 503
        data = request.get_json(force=True, silent=True) or {}
        email = (data.get("email") or "").strip()
        rec = _store.get_by_email(email)
        if not rec or not rec.get("customer_id"):
            return jsonify({"error": "no SportRoots subscription found "
                                     "for that email"}), 404
        # Product copy for the served error payload. Dashboard surfaces
        # the payload verbatim; anything else is a defect.
        err_page = ("<p style=\"max-width:34em;margin:2em auto;font:15px/1.5 "
                    "-apple-system,system-ui,sans-serif;color:#333\">"
                    "Manage billing is blocked: Stripe refused access with "
                    "the current key (Permission denied; billing portal "
                    "write required). Nothing changed and no new key was "
                    "invented. The Stripe customer portal will open here "
                    "once the payment key is authorized for billing.</p>")
        try:
            portal = stripe.billing_portal.Session.create(
                customer=rec["customer_id"],
                return_url=_public_base_url() + "/",
                **_stripe_acct(),
            )
        except stripe.error.StripeError as e:
            log.warning("SR portal Session.create failed: %r", e)
            # Never leak the raw Stripe exception text: Stripe includes
            # the used key's fingerprint fragment in Permission-denied
            # errors ("The provided key 'rk_live_...FSNK' does not have
            # the required permi…"). The blocked leg (2026-10-09 Step B,
            # sub_1UOgrzEq7168sUIG5LZL4Pv4): the configured live
            # restricted key cannot create Customer Portal sessions.
            # Portal session creation requires "Write" on the Billing
            # permission resource (Billing > Customer Portal) on the key
            # the app actually uses: STRIPE_LIVE_SECRET_KEY in live mode
            # (the *_STRIPE_TEST_SECRET_KEY env in test mode). Name the
            # exact gap on the operator surface (logs) and fail closed
            # with a contained page for the buyer; do not invent a key.
            log.error("SR portal blocked: Stripe Permission denied — the "
                      "configured Stripe key lacks Write on Billing > "
                      "Customer Portal (billing_portal.sessions.create); "
                      "widen it on the key loaded from "
                      "STRIPE_LIVE_SECRET_KEY (live) or "
                      "STRIPE_TEST_SECRET_KEY (test). Customer portal "
                      "stays closed until then.")
            resp = jsonify({"error": err_page})
            resp.status_code = 502
            return resp
        return jsonify({"portal_url": portal.url})

    @app.get("/api/sr/entitlement")
    def sr_entitlement():
        secret = os.environ.get("SR_ENTITLEMENT_SECRET", "")
        if not secret:
            # Fail closed: subscription status is never leaked unsigned.
            return jsonify({"error": "entitlement check not configured"}), 503
        email = (request.args.get("email") or "").strip().lower()
        given = request.args.get("sig") or ""
        expected = hmac.new(secret.encode(), email.encode(),
                            hashlib.sha256).hexdigest()
        if not email or not hmac.compare_digest(given, expected):
            return jsonify({"error": "bad signature"}), 403
        rec = _store.get_by_email(email)
        if not rec:
            return jsonify({"email": email, "entitled": False,
                            "status": "none", "plan": None,
                            "current_period_end": None})
        return jsonify({
            "email": email,
            "entitled": bool(rec.get("entitled")),
            "status": rec.get("status"),
            "plan": rec.get("plan"),
            "current_period_end": rec.get("current_period_end"),
        })
