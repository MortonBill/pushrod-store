"""Digital fulfillment tests (SkillForge pilot, 2026-10-02).

Covers the digital seam end to end without network or real keys:
  1. catalog seam — fulfillment_type / digital_file columns, honest
     purchasability for digital SKUs, legacy CSVs unchanged;
  2. signed download tokens — round-trip, tamper, wrong secret, expiry;
  3. fulfill_digital_lines — one email, verifiable links, ledger
     idempotency (no duplicate email on a replayed session), dry-run
     discipline for unconfigured Brevo/Kit;
  4. the Flask surface — /download streams only on a valid token,
     digital-only checkout skips Printful mapping + shipping collection,
     /api/fulfill delivers digital-only orders with no shipping address.

Run: ./../.venv/bin/python backend/test_digital.py   (from pushrod-store/)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="pushrod-digital-test-")
FILES_DIR = os.path.join(TMP, "files")
os.makedirs(FILES_DIR)
LEDGER = os.path.join(TMP, "ledger.json")

os.environ["BRAND"] = "gateway"
os.environ["PRINTFUL_DRY_RUN"] = "1"
os.environ["DIGITAL_DOWNLOAD_SECRET"] = "test-secret-do-not-ship"
os.environ["DIGITAL_FILES_DIR"] = FILES_DIR
os.environ["DIGITAL_FULFILLMENTS_PATH"] = LEDGER
os.environ.pop("BREVO_API_KEY", None)
os.environ.pop("KIT_API_KEY", None)
os.environ.pop("KIT_BUYER_TAG_ID", None)

from catalog import load_catalog                       # noqa: E402
from fulfillment import digital as dmod                # noqa: E402
from fulfillment.fulfill import split_cart_lines       # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------- 1. catalog seam ----------
def write_catalog(rows, with_digital_cols=True, fname="cat.csv"):
    path = os.path.join(TMP, fname)
    cols = ["sku", "type", "title", "description", "base_color", "design_file"]
    if with_digital_cols:
        cols += ["fulfillment_type", "digital_file"]
    with open(path, "w") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")
    return path


prices_path = os.path.join(TMP, "prices.json")
with open(prices_path, "w") as f:
    json.dump({"products": [
        {"sku": "PR-D001", "msrp": 29.95},
        {"sku": "PR-D002", "msrp": 19.95},
        {"sku": "PR-T900", "msrp": 24.95},
    ]}, f)

csv_path = write_catalog([
    {"sku": "PR-D001", "type": "playbook", "title": "Digital With File",
     "description": "d", "base_color": "n/a", "design_file": "x.png",
     "fulfillment_type": "digital", "digital_file": "playbook-a.pdf"},
    {"sku": "PR-D002", "type": "playbook", "title": "Digital No File",
     "description": "d", "base_color": "n/a", "design_file": "x.png",
     "fulfillment_type": "digital", "digital_file": ""},
    {"sku": "PR-T900", "type": "tee", "title": "Print Tee",
     "description": "d", "base_color": "black", "design_file": "x.png",
     "fulfillment_type": "", "digital_file": ""},
])
prods = {p["sku"]: p for p in load_catalog(csv_path, prices_path, mapping={})}
check("digital row parsed", prods["PR-D001"]["fulfillment_type"] == "digital"
      and prods["PR-D001"]["digital_file"] == "playbook-a.pdf")
check("digital with file purchasable without Printful mapping",
      prods["PR-D001"]["purchasable"] is True)
check("digital without file NOT purchasable (honest gate)",
      prods["PR-D002"]["purchasable"] is False)
check("print row defaults + unmapped not purchasable (unchanged)",
      prods["PR-T900"]["fulfillment_type"] == "print"
      and prods["PR-T900"]["purchasable"] is False)

legacy_path = write_catalog([
    {"sku": "PR-T900", "type": "tee", "title": "Legacy Tee",
     "description": "d", "base_color": "black", "design_file": "x.png"},
], with_digital_cols=False, fname="legacy.csv")
legacy = {p["sku"]: p for p in load_catalog(legacy_path, prices_path, mapping={})}
check("legacy CSV without digital columns loads as print",
      legacy["PR-T900"]["fulfillment_type"] == "print"
      and legacy["PR-T900"]["digital_file"] == "")

# ---------- 2. signed tokens ----------
signer = dmod.DownloadTokenSigner("unit-test-secret")
tok = signer.mint("PR-D001", "Buyer@Example.com", ttl_seconds=600)
payload = signer.verify(tok)
check("token round-trip", payload["sku"] == "PR-D001"
      and payload["email"] == "buyer@example.com")
try:
    signer.verify(tok[:-2] + ("AA" if not tok.endswith("AA") else "BB"))
    check("tampered token rejected", False)
except ValueError as e:
    check("tampered token rejected", str(e) == "bad_signature", str(e))
try:
    dmod.DownloadTokenSigner("other-secret").verify(tok)
    check("wrong-secret token rejected", False)
except ValueError as e:
    check("wrong-secret token rejected", str(e) == "bad_signature", str(e))
stale = signer.mint("PR-D001", "buyer@example.com", ttl_seconds=-10)
try:
    signer.verify(stale)
    check("expired token rejected", False)
except ValueError as e:
    check("expired token rejected", str(e) == "expired", str(e))
try:
    dmod.DownloadTokenSigner("")
    check("missing signing secret is a hard config error", False)
except dmod.DigitalConfigError:
    check("missing signing secret is a hard config error", True)
check("missing Brevo key => dry-run, no raise",
      dmod.BrevoSender(api_key="").send("a@b.c", "s", "<p>x</p>", "x")
      == {"dry_run": True})
check("unconfigured Kit => queued no-op",
      dmod.KitTagger(api_key="", tag_id="").tag_buyer("a@b.c") == "queued")

# ---------- 3. digital fulfillment + idempotency ----------
class StubSender:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, html_content, text_content):
        self.sent.append({"to": to, "subject": subject,
                          "html": html_content, "text": text_content})
        return {"messageId": "stub-1"}


class StubKit:
    def __init__(self):
        self.tagged = []

    def tag_buyer(self, email):
        self.tagged.append(email)
        return "tagged"


BY_SKU_DIGITAL = {
    "PR-D001": {"sku": "PR-D001", "title": "Electrician AI Playbook",
                "fulfillment_type": "digital",
                "digital_file": "playbook-a.pdf"},
    "PR-T900": {"sku": "PR-T900", "title": "Print Tee",
                "fulfillment_type": "print", "digital_file": ""},
}
print_l, digital_l = split_cart_lines(
    [{"sku": "PR-D001", "qty": 1}, {"sku": "PR-T900", "qty": 2},
     {"sku": "PR-UNKNOWN", "qty": 1}], BY_SKU_DIGITAL)
check("split: digital separated, unknown stays print (loud failure path)",
      [l["sku"] for l in digital_l] == ["PR-D001"]
      and [l["sku"] for l in print_l] == ["PR-T900", "PR-UNKNOWN"])

sender, kit = StubSender(), StubKit()
res = dmod.fulfill_digital_lines(
    "cs_test_digital_1", "buyer@example.com",
    [{"sku": "PR-D001", "qty": 1}], BY_SKU_DIGITAL,
    "https://shop.example", store_name="SkillForge AI",
    signer=signer, sender=sender, kit_tagger=kit,
    ledger=dmod.DigitalLedger(LEDGER))
check("one delivery email sent", len(sender.sent) == 1, len(sender.sent))
check("email addressed to buyer with brand subject",
      sender.sent and sender.sent[0]["to"] == "buyer@example.com"
      and "SkillForge AI" in sender.sent[0]["subject"])
link = res["links"][0] if res.get("links") else ""
check("link points at /download/", "/download/" in link, link)
emailed_token = link.rsplit("/download/", 1)[-1]
check("emailed token verifies for the right sku+buyer",
      signer.verify(emailed_token)["sku"] == "PR-D001")
check("kit tagged once", kit.tagged == ["buyer@example.com"], kit.tagged)
check("ledger persisted across instances",
      dmod.DigitalLedger(LEDGER).is_fulfilled("cs_test_digital_1"))

res2 = dmod.fulfill_digital_lines(
    "cs_test_digital_1", "buyer@example.com",
    [{"sku": "PR-D001", "qty": 1}], BY_SKU_DIGITAL,
    "https://shop.example", signer=signer, sender=sender, kit_tagger=kit,
    ledger=dmod.DigitalLedger(LEDGER))
check("replayed session: already_fulfilled, NO second email",
      res2["already_fulfilled"] is True and len(sender.sent) == 1)

dry = dmod.fulfill_digital_lines(
    "cs_test_digital_2", "buyer@example.com",
    [{"sku": "PR-D001", "qty": 1}], BY_SKU_DIGITAL,
    "https://shop.example", signer=signer,
    sender=dmod.BrevoSender(api_key=""), kit_tagger=StubKit(),
    ledger=dmod.DigitalLedger(LEDGER))
check("dry-run sender: delivered-but-flagged, once",
      dry["email_dry_run"] is True
      and dmod.DigitalLedger(LEDGER).get("cs_test_digital_2")["email_dry_run"]
      is True)
led = dmod.DigitalLedger(LEDGER)
check("refund flags a fulfilled session",
      led.mark_refunded("cs_test_digital_1") is True
      and led.get("cs_test_digital_1")["refunded"] is True)
check("refund of unknown session reports False, no crash",
      led.mark_refunded("cs_nope") is False)

# ---------- 4. Flask surface ----------
import app as store_app  # noqa: E402

with open(os.path.join(FILES_DIR, "playbook-a.pdf"), "wb") as f:
    f.write(b"%PDF-1.4 test bytes\n")
# Tokens minted for the HTTP layer must use the env signing secret the app
# reads (section 2's signer deliberately used its own).
app_signer = dmod.DownloadTokenSigner()
store_app.BY_SKU["PR-D001"] = {
    "sku": "PR-D001", "prefix": "PR-", "owner": "pushrod", "type": "playbook",
    "fulfillment_type": "digital", "digital_file": "playbook-a.pdf",
    "title": "Electrician AI Playbook", "description": "d",
    "base_color": "n/a", "design_file": "x.png",
    "image_url": "/img/pushrod/x.png",
    "price": {"amount": 29.95, "status": "confirmed"},
    "purchasable": True, "needs_size": False,
}
client = store_app.app.test_client()

good = app_signer.mint("PR-D001", "buyer@example.com", ttl_seconds=600)
r = client.get(f"/download/{good}")
check("/download streams the file on a valid token",
      r.status_code == 200 and b"%PDF-1.4 test bytes" in r.data,
      r.status_code)
try:
    app_signer.verify(good)
    bad = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
except ValueError:
    bad = good + "x"
r = client.get(f"/download/{bad}")
check("/download rejects a tampered token (403)", r.status_code == 403,
      r.status_code)
r = client.get("/download/" + app_signer.mint("PR-D001", "buyer@example.com",
                                              ttl_seconds=-10))
check("/download rejects an expired token (410)", r.status_code == 410,
      r.status_code)
r = client.get("/download/" + app_signer.mint("PR-NOPE", "buyer@example.com",
                                              ttl_seconds=600))
check("/download 404s a token for an unknown sku", r.status_code == 404,
      r.status_code)

captured = {}


class FakeCheckoutSession:
    id = "cs_test_digital_3"
    url = "https://checkout.stripe.com/pay/cs_test_digital_3"


def fake_session_create(**kw):
    captured.update(kw)
    return FakeCheckoutSession()


store_app.stripe.checkout.Session.create = fake_session_create
store_app.STRIPE_READY = True
r = client.post("/api/checkout", json={"items": [{"sku": "PR-D001", "qty": 1}]})
check("digital-only checkout creates a session (no Printful mapping needed)",
      r.status_code == 200 and r.get_json().get("session_id")
      == "cs_test_digital_3", r.status_code)
check("digital-only checkout collects NO shipping address",
      "shipping_address_collection" not in captured)


class FakePaidSession(dict):
    """Dict-like stub: app._sget reads dicts / StripeObjects via
    `in` + item access, so the stub must behave the same way."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


fake_paid = FakePaidSession(
    id="cs_test_digital_4",
    payment_status="paid",
    metadata={"cart": json.dumps(
        [{"sku": "PR-D001", "size": None, "qty": 1}])},
    customer_details={"email": "buyer@example.com"},
    shipping_details={"name": "", "address": {}},
)


store_app.stripe.checkout.Session.retrieve = lambda sid: fake_paid
r = client.get("/api/fulfill?session_id=cs_test_digital_4")
body = r.get_json() or {}
check("/api/fulfill delivers digital-only order with no shipping address",
      r.status_code == 200 and body.get("digital")
      and body["digital"]["already_fulfilled"] is False,
      f"{r.status_code} {body}")
check("/api/fulfill used the dry-run email path (no BREVO_API_KEY)",
      body.get("digital", {}).get("email_dry_run") is True, str(body))

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL DIGITAL TESTS PASSED")
