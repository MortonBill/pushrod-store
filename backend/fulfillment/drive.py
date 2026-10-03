"""
EverReady fulfillment: paid checkout -> per-buyer Google Drive sharing.

EverReady products are NEVER delivered as store-hosted download links and
NEVER as public "anyone with the link" Drive shares (Bill 2026-09-19,
~/workspace/your_files/everready-family/per-buyer-delivery-decision-2026-09-19.md).
Each buyer is granted individual Viewer access to exactly the file(s) they
purchased, tied to their purchase email; the buyer then gets ONE Brevo
email carrying the Drive links. Same fail-loud / dry-run discipline as
fulfillment/digital.py:

  1. Drive share — Google Drive API v3, stdlib only (no google client in
     requirements.txt, and none may be added by this module). Credentials
     come from EVERREADY_DRIVE_CREDENTIALS_JSON: an authorized-user OAuth
     JSON {"client_id", "client_secret", "refresh_token"} for the account
     that owns the files, exchanged at the OAuth token endpoint. (A
     service-account key JSON is rejected loudly: stdlib has no RS256
     signer, and the files live in a personal account — no domain to
     delegate.) Gate: EVERREADY_DRIVE_ENABLED=1 turns real sharing on.
     Gate off or credentials absent => DRY-RUN: every intended share is
     logged in full and recorded as dry-run in the ledger, never sent.
     Gate ON but credentials missing/malformed => DriveConfigError names
     the missing env var (a paid order must fail loudly, never silently).
  2. Notification fallback — a silent share can fail when the buyer's
     email has no Google account (decision doc, step 3 note). On any
     silent-share rejection we retry once WITH notification so Drive
     invites them; a second failure raises DriveShareError and the
     webhook 500s so Stripe retries.
  3. Email — Brevo via fulfillment.digital.BrevoSender (dry-run when no
     BREVO_API_KEY). One email per session, ledger-guarded.

Idempotency: fulfilled session ids persist in a JSON ledger
(EVERREADY_FULFILLMENTS_PATH, default everready_fulfillments.json next to
this file). A replayed checkout.session.completed returns
{"already_fulfilled": True} and shares/emails nothing twice.

Products without a Drive file on record are NEVER given invented
delivery: they land in the result's "manual" list (per-family website
provisioning, delivery paths still to be wired) and the ledger says so.
"""
import html
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import digital as digital_mod

log = logging.getLogger("pushrod.drive")

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# SKU -> Drive file IDs, copied from the per-buyer delivery decision
# (2026-09-19, verified live in the 2026-09-19 bundle test purchase).
# The bundle has no PDF of its own: bundle buyers get BOTH files.
EVERREADY_DRIVE_FILES = {
    "ER-FCC-001": ["1-vnWoHjDFEBQ4ZNu_R2XeTPRNTwYCcQj"],   # Family Command Center $37
    "ER-EK-001": ["1qyO__m4RJSjKSN1dA2rF-0W1AXhGu5KY"],    # Executor's Kit $37
    "ER-FRB-001": ["1-vnWoHjDFEBQ4ZNu_R2XeTPRNTwYCcQj",    # Family Readiness Bundle $49 = both
                    "1qyO__m4RJSjKSN1dA2rF-0W1AXhGu5KY"],
}

# Every EverReady checkout SKU routes through this module (never the
# download-token path). SKUs absent from EVERREADY_DRIVE_FILES have no
# Drive deliverable on record and are reported for manual fulfillment.
EVERREADY_SKU_PREFIX = "ER-"


def is_drive_sku(sku):
    return (sku or "").startswith(EVERREADY_SKU_PREFIX)


def drive_file_url(file_id):
    return f"https://drive.google.com/file/d/{file_id}/view"


class DriveConfigError(RuntimeError):
    """Drive fulfillment is misconfigured (gate on, credentials bad)."""


class DriveShareError(RuntimeError):
    """A Drive share attempt failed (API rejected/errored)."""


# ---------- Drive API client ----------

class DriveShareClient:
    """Minimal Drive v3 permissions client (urllib, stdlib only).

    No credentials / gate off => dry-run: share() logs the intended grant
    and returns {"dry_run": True} — test and local runs stay honest
    without touching a real buyer's access.
    """

    def __init__(self, credentials_json=None, enabled=None):
        raw = credentials_json if credentials_json is not None else \
            os.environ.get("EVERREADY_DRIVE_CREDENTIALS_JSON", "")
        if enabled is None:
            enabled = (os.environ.get("EVERREADY_DRIVE_ENABLED", "")
                       .strip() == "1")
        self._creds = None
        if raw:
            try:
                self._creds = json.loads(raw)
            except ValueError as e:
                raise DriveConfigError(
                    "EVERREADY_DRIVE_CREDENTIALS_JSON is not valid JSON "
                    f"({e}). Set it in the service environment (Render -> "
                    "service -> Environment); values never go in git.")
        self.dry_run = not (enabled and self._creds)
        if enabled and not self._creds:
            raise DriveConfigError(
                "EVERREADY_DRIVE_ENABLED=1 but EVERREADY_DRIVE_CREDENTIALS_JSON "
                "is not set. Create an authorized-user OAuth credential "
                "(client_id / client_secret / refresh_token) for the Google "
                "account that owns the EverReady files and set it in the "
                "service environment; values never go in git.")
        if self.dry_run:
            log.warning("EVERREADY DRIVE DRY-RUN: sharing disabled "
                        "(EVERREADY_DRIVE_ENABLED!=1 or no credentials) — "
                        "shares will be logged, not granted.")
        self._token = None

    def _access_token(self):
        if self._token:
            return self._token
        creds = self._creds or {}
        if creds.get("type") == "service_account" or (
                "private_key" in creds and "refresh_token" not in creds):
            raise DriveConfigError(
                "EVERREADY_DRIVE_CREDENTIALS_JSON holds a service-account "
                "key, which this stdlib-only client cannot sign (no RS256) "
                "and which cannot own the files anyway. Provide an "
                "authorized-user OAuth JSON (client_id / client_secret / "
                "refresh_token) for the account that owns the files.")
        missing = [k for k in ("client_id", "client_secret", "refresh_token")
                   if not creds.get(k)]
        if missing:
            raise DriveConfigError(
                "EVERREADY_DRIVE_CREDENTIALS_JSON is missing field(s): "
                + ", ".join(missing)
                + " (authorized-user OAuth JSON required).")
        body = urllib.parse.urlencode({
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise DriveShareError(
                f"OAuth token exchange {e.code}: "
                f"{e.read().decode(errors='replace')[:300]}")
        except Exception as e:  # noqa: BLE001
            raise DriveShareError(f"OAuth token exchange failed: {e}")
        self._token = payload.get("access_token")
        if not self._token:
            raise DriveShareError("OAuth token exchange returned no token")
        return self._token

    def _api(self, method, url, body):
        """One Drive API call. Tests substitute this method wholesale."""
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            raise DriveShareError(
                f"Drive API {e.code}: "
                f"{e.read().decode(errors='replace')[:300]}")
        except Exception as e:  # noqa: BLE001
            raise DriveShareError(f"Drive API call failed: {e}")

    def share(self, file_id, email, notify=False):
        """Grant `email` Viewer on one file. Returns the API payload (or
        {"dry_run": True}). Never logs the buyer email at info level with
        anything sensitive — email + file id is the audit record."""
        if self.dry_run:
            log.warning(
                "EVERREADY DRIVE DRY-RUN share: file=%s viewer=%s notify=%s",
                file_id, email, notify)
            return {"dry_run": True}
        query = urllib.parse.urlencode(
            {"sendNotificationEmail": "true" if notify else "false",
             "fields": "id,role,emailAddress"})
        return self._api(
            "POST", f"{DRIVE_API_BASE}/files/{file_id}/permissions?{query}",
            {"type": "user", "role": "reader", "emailAddress": email})

    def share_file(self, file_id, email):
        """Silent share first; on rejection (buyer email has no Google
        account — decision doc step 3) retry WITH notification so Drive
        invites them. A second failure raises DriveShareError."""
        try:
            result = self.share(file_id, email, notify=False)
            return {**result, "notified": False}
        except DriveShareError as e:
            log.warning("silent Drive share rejected for file %s (%s) — "
                        "retrying with notification", file_id, e)
            result = self.share(file_id, email, notify=True)
            return {**result, "notified": True}


# ---------- idempotency ledger ----------

def default_ledger_path():
    return os.environ.get(
        "EVERREADY_FULFILLMENTS_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "everready_fulfillments.json"))


class EverReadyLedger:
    """JSON record of fulfilled EverReady sessions (idempotency wall).

    Shape: {"sessions": {session_id: {"email", "skus", "shared",
    "manual", "email_dry_run", "share_dry_run", "shared_at"}}}. Written
    atomically (tmp + os.replace), same discipline as DigitalLedger."""

    def __init__(self, path=None):
        self.path = path or default_ledger_path()
        self._data = {"sessions": {}}
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except (ValueError, OSError):
                log.warning("everready ledger unreadable at %s — starting "
                            "fresh (duplicate-webhook guard degraded)",
                            self.path)
                self._data = {"sessions": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=1)
        os.replace(tmp, self.path)

    def get(self, session_id):
        return self._data["sessions"].get(session_id)

    def is_fulfilled(self, session_id):
        return self.get(session_id) is not None

    def record(self, session_id, email, skus, shared, manual,
               email_dry_run, share_dry_run):
        self._data["sessions"][session_id] = {
            "email": email, "skus": list(skus), "shared": shared,
            "manual": list(manual), "email_dry_run": bool(email_dry_run),
            "share_dry_run": bool(share_dry_run),
            "shared_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ts": int(time.time()),
        }
        self._save()


# ---------- the fulfillment entry point ----------

def fulfill_drive_lines(stripe_session_id, customer_email, drive_lines,
                        products_by_sku, store_name="EverReady Family",
                        drive_client=None, sender=None, ledger=None):
    """Deliver EverReady cart lines for a paid session: per-buyer Drive
    Viewer shares + ONE email with the Drive links. Returns a summary
    dict; at most one fulfillment per session, ever (ledger-guarded).

    drive_lines: [{sku, qty}] already routed here by is_drive_sku().
    A share failure raises BEFORE the ledger write or the email, so the
    webhook 500s, Stripe retries, and nothing is half-announced."""
    ledger = ledger if ledger is not None else EverReadyLedger()
    if ledger.is_fulfilled(stripe_session_id):
        log.info("everready: session %s already fulfilled — no duplicate "
                 "share or email", stripe_session_id)
        return {"already_fulfilled": True, "session_id": stripe_session_id}

    drive_client = drive_client if drive_client is not None \
        else DriveShareClient()
    sender = sender if sender is not None else digital_mod.BrevoSender()

    shared = {}   # sku -> [file_id, ...] actually granted (or dry-run)
    manual = []   # skus with no Drive deliverable on record
    titles = {}
    for line in drive_lines:
        sku = line["sku"]
        product = products_by_sku.get(sku) or {}
        titles[sku] = product.get("title") or sku
        file_ids = EVERREADY_DRIVE_FILES.get(sku)
        if not file_ids:
            manual.append(sku)
            continue
        for file_id in file_ids:
            drive_client.share_file(file_id, customer_email)
        shared[sku] = list(file_ids)

    text_lines = ["Thanks for your order — your EverReady Family files "
                  "are ready:", ""]
    html_items = []
    for sku, file_ids in shared.items():
        for file_id in file_ids:
            url = drive_file_url(file_id)
            text_lines.append(f"{titles[sku]}\n{url}\n")
            html_items.append(
                f'<li><a href="{url}">{titles[sku]}</a></li>')
    for sku in manual:
        text_lines.append(
            f"{titles[sku]}\nWe're setting this up for your family and "
            "will email you as soon as it's ready.\n")
        html_items.append(
            f"<li>{titles[sku]} — we're setting this up for your family "
            "and will email you as soon as it's ready.</li>")
    tail = ("Access is tied to this email address — sign in with the "
            "Google account that matches it. Questions? Just reply to "
            "this email.")
    text_lines.append(tail)
    html_content = ("<p>Thanks for your order — your EverReady Family "
                    f"files are ready:</p><ul>{''.join(html_items)}</ul>"
                    f"<p>{html.escape(tail)}</p>")

    subject = f"Your files from {store_name}"
    result = sender.send(customer_email, subject, html_content,
                         "\n".join(text_lines))
    ledger.record(stripe_session_id, customer_email,
                  [l["sku"] for l in drive_lines], shared=shared,
                  manual=manual, email_dry_run=bool(result.get("dry_run")),
                  share_dry_run=bool(drive_client.dry_run))
    log.info("everready: session %s shared %s to %s, manual %s "
             "(share_dry_run=%s, email_dry_run=%s)",
             stripe_session_id, shared, customer_email, manual,
             bool(drive_client.dry_run), bool(result.get("dry_run")))
    return {"already_fulfilled": False, "session_id": stripe_session_id,
            "shared": shared, "manual": manual,
            "links": [drive_file_url(f) for ids in shared.values()
                      for f in ids],
            "share_dry_run": bool(drive_client.dry_run),
            "email_dry_run": bool(result.get("dry_run"))}
