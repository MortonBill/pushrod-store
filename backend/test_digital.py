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
     /api/fulfill delivers digital-only orders with no shipping address;
  5. delivery storage (fulfillment/storage.py) — local backend unchanged,
     S3-compatible backend against an in-process fake bucket: signed
     token -> storage fetch -> %PDF bytes, garbage token -> 403, missing
     object -> 404, misconfiguration -> loud error, never a silent miss;
  6. catalog staging + flip (Lane 1) — the listed=0 flag: loaded but never
     listed and never purchasable; the real RE guide catalog (383 rows,
     382 deliverables) staged dark, then flipped live 2026-10-02
     after the $29.95 test-purchase gate passed — file-backed rows listed
     and purchasable, the 1 remaining source gap still dark at live-index prices.

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
check("delivery email is branded (banner + brand accent)",
      sender.sent and "SkillForge AI" in sender.sent[0]["html"]
      and "#2dd4bf" in sender.sent[0]["html"])
check("delivery email carries the brand logo",
      sender.sent and "<img" in sender.sent[0]["html"]
      and "skillforge-ai-logo.png" in sender.sent[0]["html"])
check("delivery text opens with the brand banner",
      sender.sent and sender.sent[0]["text"].startswith("SkillForge AI"))
check("email style resolves by store name",
      dmod.email_style_for("IronHead")["accent"] == "#c8402a"
      and dmod.email_style_for("Restoration Essentials")["domain"]
      == "restoreessentials.com")
check("email style resolves by sku prefix when store name is off",
      dmod.email_style_for("", ["ER-FRC-001"])["display"]
      == "EverReady Family"
      and dmod.email_style_for("", ["ST-MUSHROOM-001"])["display"]
      == "Stitchfolk")

# Delivery brand follows the PURCHASED product, not the service boot
# brand: the shared service boots as SkillForge AI, but a
# RestorationEssentials guide buyer's email must say Restoration
# Essentials (regression: every delivery arrived "from SkillForge AI").
BY_SKU_RE_OWNED = {
    "RE-GD-1": {"sku": "RE-GD-1", "title": "1970 Chevelle Guide",
                "fulfillment_type": "digital",
                "digital_file": "chevelle.pdf",
                "owner": "restorationessentials"},
    "IH-GD-1": {"sku": "IH-GD-1", "title": "CB750 Buyer's Guide",
                "fulfillment_type": "digital",
                "digital_file": "cb750.pdf", "owner": "ironhead"},
}
re_sender = StubSender()
dmod.fulfill_digital_lines(
    "cs_test_brand_re", "buyer@example.com",
    [{"sku": "RE-GD-1", "qty": 1}], BY_SKU_RE_OWNED,
    "https://shop.example", store_name="SkillForge AI",
    signer=signer, sender=re_sender, kit_tagger=StubKit(),
    ledger=dmod.DigitalLedger(LEDGER))
check("delivery brand follows purchased product owner",
      re_sender.sent
      and re_sender.sent[0]["subject"] == "Your download from "
      "Restoration Essentials", re_sender.sent)
mixed_sender = StubSender()
dmod.fulfill_digital_lines(
    "cs_test_brand_mixed", "buyer@example.com",
    [{"sku": "RE-GD-1", "qty": 1}, {"sku": "IH-GD-1", "qty": 1}],
    BY_SKU_RE_OWNED, "https://shop.example", store_name="SkillForge AI",
    signer=signer, sender=mixed_sender, kit_tagger=StubKit(),
    ledger=dmod.DigitalLedger(LEDGER))
check("mixed-brand cart falls back to caller store name",
      mixed_sender.sent
      and mixed_sender.sent[0]["subject"] == "Your download from "
      "SkillForge AI", mixed_sender.sent)
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

# ---------- 5. delivery storage backends ----------
import threading  # noqa: E402
import urllib.parse  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import yaml as _yaml  # noqa: E402

from catalog import load_catalog as _load_catalog  # noqa: E402
from fulfillment import storage as storage_mod  # noqa: E402

# local backend (the default) — same bytes the Flask surface served above.
local_store = storage_mod.get_storage(FILES_DIR)
check("default storage backend is local", local_store.is_local is True)
check("local storage finds an existing deliverable",
      local_store.exists("playbook-a.pdf") is True)
check("local storage reports a missing deliverable",
      local_store.exists("nope.pdf") is False)
try:
    local_store.open("../outside.pdf")
    check("local storage refuses path traversal", False)
except storage_mod.StorageNotFound:
    check("local storage refuses path traversal", True)

# fake private bucket: path-style S3 over local HTTP, SigV4 header checked.
FAKE_OBJECTS = {"storage-guide.pdf": b"%PDF-1.7 storage bytes\n"}


class _FakeS3(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output readable
        pass

    def _key(self):
        path = urllib.parse.unquote(self.path).lstrip("/")
        return path.split("/", 1)[1]  # strip bucket segment

    def _authed(self):
        return (self.headers.get("Authorization") or "").startswith(
            "AWS4-HMAC-SHA256 Credential=fake-access-key/")

    def do_GET(self):
        if not self._authed():
            self.send_response(401); self.end_headers(); return
        data = FAKE_OBJECTS.get(self._key())
        if data is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if not self._authed():
            self.send_response(401); self.end_headers(); return
        self.send_response(200 if self._key() in FAKE_OBJECTS else 404)
        self.end_headers()

    def do_PUT(self):
        if not self._authed():
            self.send_response(401); self.end_headers(); return
        FAKE_OBJECTS[self._key()] = self.rfile.read(
            int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("ETag", '"fake-etag"')
        self.end_headers()


_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeS3)
threading.Thread(target=_server.serve_forever, daemon=True).start()
_s3_env = {
    "DIGITAL_STORAGE_BACKEND": "s3",
    "DIGITAL_S3_BUCKET": "fake-bucket",
    "DIGITAL_S3_REGION": "auto",
    "DIGITAL_S3_ENDPOINT": f"http://127.0.0.1:{_server.server_address[1]}",
    "DIGITAL_S3_ACCESS_KEY_ID": "fake-access-key",
    "DIGITAL_S3_SECRET_ACCESS_KEY": "fake-secret-key",
}
_saved_env = {k: os.environ.get(k) for k in _s3_env}
os.environ.update(_s3_env)
try:
    s3_store = storage_mod.get_storage()
    check("s3 backend selected by env", s3_store.is_local is False)
    _chunks, _size, _ctype = s3_store.open("storage-guide.pdf")
    check("storage fetch returns the %PDF bytes",
          b"".join(_chunks) == b"%PDF-1.7 storage bytes\n"
          and _size == len(b"%PDF-1.7 storage bytes\n")
          and _ctype == "application/pdf")
    check("storage exists() true/false",
          s3_store.exists("storage-guide.pdf") is True
          and s3_store.exists("absent.pdf") is False)
    try:
        s3_store.open("absent.pdf")
        check("missing object raises StorageNotFound", False)
    except storage_mod.StorageNotFound:
        check("missing object raises StorageNotFound", True)
    s3_store.put("put-guide.pdf", b"%PDF-1.5 put bytes\n")
    check("storage put -> fetch round-trip",
          b"".join(s3_store.open("put-guide.pdf")[0])
          == b"%PDF-1.5 put bytes\n")
    try:
        storage_mod.S3Storage.from_env({"DIGITAL_STORAGE_BACKEND": "s3"})
        check("missing bucket/keys is a loud config error", False)
    except storage_mod.StorageConfigError as e:
        check("missing bucket/keys is a loud config error",
              "DIGITAL_S3_BUCKET" in str(e), str(e))
    try:
        storage_mod.get_storage(env={"DIGITAL_STORAGE_BACKEND": "carrier-pigeon"})
        check("unknown backend is a loud config error", False)
    except storage_mod.StorageConfigError:
        check("unknown backend is a loud config error", True)

    # Flask surface over object storage: token -> storage fetch -> %PDF.
    store_app.BY_SKU["PR-D010"] = {
        "sku": "PR-D010", "prefix": "PR-", "owner": "pushrod",
        "type": "guide", "fulfillment_type": "digital",
        "digital_file": "storage-guide.pdf", "title": "Storage Guide",
        "description": "d", "base_color": "n/a", "design_file": "g.pdf",
        "image_url": "/img/muscle/g.pdf",
        "price": {"amount": 29.95, "status": "draft"},
        "purchasable": True, "needs_size": False,
    }
    store_app.BY_SKU["PR-D011"] = {
        **store_app.BY_SKU["PR-D010"], "sku": "PR-D011",
        "digital_file": "absent.pdf",
    }
    r = client.get("/download/" + app_signer.mint(
        "PR-D010", "buyer@example.com", ttl_seconds=600))
    check("/download streams %PDF bytes from object storage",
          r.status_code == 200 and r.data == b"%PDF-1.7 storage bytes\n",
          f"{r.status_code} {r.data[:40]}")
    check("storage download is an attachment with the PDF name",
          "storage-guide.pdf" in (r.headers.get("Content-Disposition") or ""),
          r.headers.get("Content-Disposition"))
    r = client.get("/download/not-a-token")
    check("/download rejects a garbage token (403) on storage backend",
          r.status_code == 403, r.status_code)
    r = client.get("/download/" + app_signer.mint(
        "PR-D010", "buyer@example.com", ttl_seconds=-10))
    check("/download rejects an expired token (410) on storage backend",
          r.status_code == 410, r.status_code)
    r = client.get("/download/" + app_signer.mint(
        "PR-D011", "buyer@example.com", ttl_seconds=600))
    check("/download 404s when the storage object is missing",
          r.status_code == 404, r.status_code)
finally:
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _server.shutdown()

r = client.get(f"/download/{good}")
check("local backend still serves exactly as before (env restored)",
      r.status_code == 200 and b"%PDF-1.4 test bytes" in r.data,
      r.status_code)

# ---------- 6. dark catalog staging (listed=0) ----------
dark_csv = os.path.join(TMP, "dark.csv")
with open(dark_csv, "w") as f:
    f.write("sku,type,title,description,base_color,design_file,"
            "fulfillment_type,digital_file,listed\n")
    f.write("PR-D020,guide,Dark Guide,d,n/a,g.pdf,digital,g.pdf,0\n")
    f.write("PR-D021,guide,Listed Guide,d,n/a,g.pdf,digital,g.pdf,1\n")
    f.write("PR-D022,guide,Blank Flag Guide,d,n/a,g.pdf,digital,g.pdf,\n")
dark_prices = os.path.join(TMP, "dark-prices.json")
with open(dark_prices, "w") as f:
    json.dump({"products": [
        {"sku": s, "msrp": 29.95} for s in ("PR-D020", "PR-D021", "PR-D022")
    ]}, f)
dark = {p["sku"]: p for p in _load_catalog(dark_csv, dark_prices, mapping={})}
check("listed=0 loads dark: present, unlisted, NOT purchasable",
      dark["PR-D020"]["listed"] is False
      and dark["PR-D020"]["purchasable"] is False
      and dark["PR-D020"]["price"] is not None)
check("listed=1 behaves exactly as before",
      dark["PR-D021"]["listed"] is True
      and dark["PR-D021"]["purchasable"] is True)
check("blank listed flag = listed (legacy default)",
      dark["PR-D022"]["listed"] is True
      and legacy["PR-T900"]["listed"] is True)

# dark rows never surface publicly
store_app.BY_SKU["PR-D020"] = dark["PR-D020"]
store_app.PRODUCTS.append(dark["PR-D020"])
r = client.get("/api/products")
check("/api/products hides unlisted rows",
      r.status_code == 200
      and "PR-D020" not in {p["sku"] for p in r.get_json()})
check("/api/products/<sku> 404s an unlisted row",
      client.get("/api/products/PR-D020").status_code == 404)
check("/product/<sku> 404s an unlisted row",
      client.get("/product/PR-D020").status_code == 404)
check("listed product page + API still serve",
      client.get("/product/PR-D001").status_code == 200
      and client.get("/api/products/PR-D001").status_code == 200)
store_app.PRODUCTS.remove(dark["PR-D020"])
del store_app.BY_SKU["PR-D020"]

# the real staged RE guide catalog (checkout migration Lane 1)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
re_prods = _load_catalog(os.path.join(REPO_ROOT, "data", "re-catalog.csv"),
                         os.path.join(REPO_ROOT, "data", "re-prices.json"),
                         sku_prefixes=["RE-GD-"])
check("RE guide catalog loads 385 rows", len(re_prods) == 385, len(re_prods))
_GATE_SKU = "RE-GD-CHEVROLET-BEL-AIR-1955"  # Lane 1 test-purchase gate (opened 2026-10-02, passed $32.05)
# 2026-10-07 (Bill: "if something is broken fix it"): 9 duplicate titles
# carried 10 redundant rows (incl. the 1970 Olds 442 W30/W45 pair). The
# weaker copy of each pair stays LOADED with its deliverable but is
# unlisted and never sellable; the record/file are never deleted.
_RETIRED_20261007 = {
    "RE-GD-FORD-MUSTANG-1965", "RE-GD-CHEVROLET-CAMARO-1967",
    "RE-GD-1967-MUSTANG", "RE-GD-OLDSMOBILE-442-1968-W45",
    "RE-GD-FORD-MUSTANG-BOSS-1969", "RE-GD-OLDSMOBILE-442-1969-W45",
    "RE-GD-CHEVROLET-CHEVELLE-1970", "RE-GD-OLDSMOBILE-442-1970-W30",
    "RE-GD-OLDSMOBILE-442-1970-W45", "RE-GD-PLYMOUTH-BARRACUDA-1970"}
# 2026-10-08 guide relist: the hash-verified rebuilt guides were placed
# into the delivery store and their rows flipped listed. The shelf is now
# 345 listed of 385; the 40 dark rows below NEVER sell: the 10
# duplicate-retired rows, the 24 rebuilt-but-held Oldsmobile rows (20 here
# plus the 4 retired W30/W45 variants), the 3 Buick Nailhead 401/425 rows
# (banner hold), and 7 rows whose corrected files were never placed.
_DARK_AFTER_RELIST_20261008 = _RETIRED_20261007 | {
    "RE-GD-OLDSMOBILE-442-1964", "RE-GD-OLDSMOBILE-442-1965",
    "RE-GD-OLDSMOBILE-442-1966", "RE-GD-OLDSMOBILE-442-1967",
    "RE-GD-OLDSMOBILE-442-1968", "RE-GD-OLDSMOBILE-442-1969",
    "RE-GD-OLDSMOBILE-442-1970", "RE-GD-OLDSMOBILE-442-1971",
    "RE-GD-OLDSMOBILE-442-1972", "RE-GD-OLDSMOBILE-442-1973",
    "RE-GD-OLDSMOBILE-CUTLASS-1964", "RE-GD-OLDSMOBILE-CUTLASS-1965",
    "RE-GD-OLDSMOBILE-CUTLASS-1966", "RE-GD-OLDSMOBILE-CUTLASS-1967",
    "RE-GD-OLDSMOBILE-CUTLASS-1968", "RE-GD-OLDSMOBILE-CUTLASS-1969",
    "RE-GD-OLDSMOBILE-CUTLASS-1970", "RE-GD-OLDSMOBILE-CUTLASS-1971",
    "RE-GD-OLDSMOBILE-CUTLASS-1972", "RE-GD-OLDSMOBILE-CUTLASS-1973",
    "RE-GD-1965-BUICK-GRAN-SPORT-401-425", "RE-GD-1965-BUICK-RIVIERA-GS-401",
    "RE-GD-1966-BUICK-GRAN-SPORT-401-425",
    "RE-GD-CHEVROLET-IMPALA-1972", "RE-GD-FORD-FALCON-SPRINT-1963-HALF",
    "RE-GD-FORD-FALCON-SPRINT-1964", "RE-GD-FORD-FALCON-SPRINT-1965",
    "RE-GD-FORD-MUSTANG-BOSS-429-1969", "RE-GD-FORD-MUSTANG-BOSS-429-1970",
    "RE-GD-MERCURY-CYCLONE-1971"}
check("RE guide rows: 345 listed+purchasable after the 2026-10-08 relist; "
      "the 40 held rows (10 duplicate-retired, 24 Oldsmobile, 3 Nailhead, "
      "7 never-placed) stay loaded, unlisted, never sellable",
      _DARK_AFTER_RELIST_20261008 <= {p["sku"] for p in re_prods}
      and sum(1 for p in re_prods if p["listed"]) == 345
      and all((p["listed"] is False and p["purchasable"] is False)
              if p["sku"] in _DARK_AFTER_RELIST_20261008
              else (p["listed"] is True and p["purchasable"] is True)
              for p in re_prods if p["digital_file"]))
check("every RE guide row WITHOUT a deliverable stays dark, never sellable",
      all(p["listed"] is False and p["purchasable"] is False
          for p in re_prods if not p["digital_file"]))
_gate = next(p for p in re_prods if p["sku"] == _GATE_SKU)
check("RE gate SKU is listed and purchasable at $29.95",
      _gate["listed"] is True and _gate["purchasable"] is True
      and _gate["price"]["amount"] == 29.95)
# 2026-10-07: Lane 1 is live, so the draft staging flag is retired —
# prices confirmed, amounts UNCHANGED (the flip kills the "intro price"
# badge in store.js, which renders it only for status == "draft").
check("RE guide prices are confirmed (2026-10-07 draft->confirmed flip)",
      all(p["price"] and p["price"]["status"] == "confirmed" for p in re_prods))
_amounts = {}
for p in re_prods:
    _amounts[p["price"]["amount"]] = _amounts.get(p["price"]["amount"], 0) + 1
check("RE guide prices: all guides $29.95 (Bill's standing rule, x385)",
      _amounts == {29.95: 385}, str(_amounts))
_with_file = [p for p in re_prods if p["digital_file"]]
check("385 RE guides carry a deliverable; 0 await a source PDF",
      len(_with_file) == 385
      and all(p["digital_file"].endswith(".pdf") for p in _with_file)
      and len(re_prods) - len(_with_file) == 0)
_duster = next(p for p in re_prods if p["sku"] == "RE-GD-1970-PLYMOUTH-DUSTER")
# 2026-10-07: the Duster's probe-era $19.95 was a defect against Bill's
# all-guides-$29.95 rule; the record is finished (file placed in the
# re-wave/rebuilt store, delivery path + manifest entry repaired).
check("the Duster guide sells at the standard $29.95 guide price "
      "(2026-10-07 record finish)",
      _duster["price"]["amount"] == 29.95
      and _duster["price"]["status"] == "confirmed"
      and _duster["listed"] is True and _duster["purchasable"] is True
      and _duster["digital_file"] == "RE_1970-plymouth-duster.pdf")

with open(os.path.join(REPO_ROOT, "data", "re-digital-sources.json")) as f:
    _sources = json.load(f)
check("upload work order: 385 files / 675,077,630 bytes / 0 gaps",
      _sources["storage"]["total_files"] == 385
      and _sources["storage"]["total_bytes"] == 675077630
      and sum(e["bytes"] for e in _sources["files"]) == 675077630
      and len(_sources["missing_source_skus"]) == 0
      and all(e["size_verified_against_manifest"] for e in _sources["files"]))

# ---------- 6b. SkillForge forms packs + bundles (checkout migration 2026-10-03) ----------
# 11 of the 12 legacy no-SKU site products, priced from live site copy
# (forms packs $19; playbook+forms bundles $49) and staged DARK
# (listed=0): loaded, priced, file-backed, never listed and never
# purchasable until the parent's gate flips them (flipped LIVE
# 2026-10-03). The 12th product, starter-pack, was a $299/qtr quarterly
# SUBSCRIPTION on the live site until Bill ruled on the model
# 2026-10-03: $59 one-time for both playbooks (SF-BND-QBO-JOB-001,
# live below); the quarterly subscription is retired.
# 2026-10-05: HVAC + Plumber forms packs built to the same 8-form standard
# (SF-FRM-HVAC-001 / SF-FRM-PLUM-001, files sha256-verified on the store
# digital disk) and their playbook+forms bundles (SF-BND-HVAC-001 /
# SF-BND-PLUM-001 at $49 sibling parity) shipped LIVE with them — catalog
# is now 38 rows (22 playbooks + 8 forms + 8 bundles).
sf_prods = _load_catalog(os.path.join(REPO_ROOT, "data", "skillforge-catalog.csv"),
                         os.path.join(REPO_ROOT, "data", "skillforge-prices.json"),
                         sku_prefixes=["SF-"])
sf_by_sku = {p["sku"]: p for p in sf_prods}
SF_FORMS = {"SF-FRM-AUTO-001": ("Auto Repair Forms Pack", 19.0),
            "SF-FRM-ELEC-001": ("Electrician Forms Pack", 19.0),
            "SF-FRM-PEST-001": ("Pest Control Forms Pack", 19.0),
            "SF-FRM-ROOF-001": ("Roofing Forms Pack", 19.0),
            "SF-FRM-VET-001": ("Veterinary Practice Forms Pack", 19.0),
            "SF-FRM-HVAC-001": ("HVAC Forms Pack", 19.0),
            "SF-FRM-PLUM-001": ("Plumber Forms Pack", 19.0),
            "SF-FRM-CONT-001": ("Contractor Forms Pack", 19.0)}
SF_BUNDLES = {"SF-BND-AUTO-001": ("SF-AUTO-001", "SF-FRM-AUTO-001"),
              "SF-BND-ELEC-001": ("SF-ELEC-001", "SF-FRM-ELEC-001"),
              "SF-BND-PEST-001": ("SF-PEST-001", "SF-FRM-PEST-001"),
              "SF-BND-ROOF-001": ("SF-ROOF-001", "SF-FRM-ROOF-001"),
              "SF-BND-VET-001": ("SF-VET-001", "SF-FRM-VET-001"),
              "SF-BND-HVAC-001": ("SF-HVAC-001", "SF-FRM-HVAC-001"),
              "SF-BND-PLUM-001": ("SF-PLUM-001", "SF-FRM-PLUM-001")}
check("SkillForge catalog loads 38 rows (22 playbooks + 8 forms + 8 bundles)",
      len(sf_prods) == 38, len(sf_prods))
check("the 22 playbooks stay listed and purchasable at live prices ($37 x21, BLD $19)",
      sum(1 for p in sf_prods if p["type"] == "playbook") == 22
      and all(p["listed"] is True and p["purchasable"] is True
              and p["price"]["amount"] == (19.0 if p["sku"] == "SF-BLD-001" else 37.0)
              for p in sf_prods if p["type"] == "playbook"))
check("all 8 forms packs are priced from live site copy and live (flipped 2026-10-03; HVAC+Plumber added live 2026-10-05)",
      all(s in sf_by_sku and sf_by_sku[s]["title"] == t
          and sf_by_sku[s]["price"]["amount"] == amt
          and sf_by_sku[s]["listed"] is True
          and sf_by_sku[s]["purchasable"] is True
          and sf_by_sku[s]["digital_file"].endswith(".pdf")
          for s, (t, amt) in SF_FORMS.items()))
check("all 7 bundles are priced $49, live (flipped 2026-10-03; HVAC+Plumber added live 2026-10-05), components resolve to files",
      all(s in sf_by_sku and sf_by_sku[s]["price"]["amount"] == 49.0
          and sf_by_sku[s]["listed"] is True
          and sf_by_sku[s]["purchasable"] is True
          and sf_by_sku[s]["bundle_skus"] == [a, b]
          and sf_by_sku[a]["digital_file"] and sf_by_sku[b]["digital_file"]
          for s, (a, b) in SF_BUNDLES.items()))

check("starter pack bundle live at $59 one-time (Bill ruled 2026-10-03), components resolve",
      "SF-BND-QBO-JOB-001" in sf_by_sku
      and sf_by_sku["SF-BND-QBO-JOB-001"]["price"]["amount"] == 59.0
      and sf_by_sku["SF-BND-QBO-JOB-001"]["listed"] is True
      and sf_by_sku["SF-BND-QBO-JOB-001"]["purchasable"] is True
      and sf_by_sku["SF-BND-QBO-JOB-001"]["bundle_skus"] == ["SF-QBO-001", "SF-JOB-001"]
      and sf_by_sku["SF-QBO-001"]["digital_file"]
      and sf_by_sku["SF-JOB-001"]["digital_file"])


with open(os.path.join(REPO_ROOT, "brands", "restorationessentials.yaml")) as f:
    _re_brand = _yaml.safe_load(f)
check("RE brand yaml claims RE-GD- and stages the guide catalog",
      "RE-GD-" in _re_brand["brand"]["sku_prefixes"]
      and any("re-catalog.csv" in (c.get("csv") or "")
              for c in _re_brand["catalog"]["catalogs"])
      and "download" in (_re_brand.get("fulfillment") or {}).get("delivery", ""))



# ---------- 6c. IronHead guides first wave (checkout migration Lane 2, 2026-10-03) ----------
# First wave of the canonical IH lineup staged DARK (listed=0): the 4 hero
# restoration guides + the Shovelhead buyer's guide carry real deliverables
# (QA-passed publish-lane bundles; work order data/ih-digital-sources.json)
# but stay unlisted and unpurchasable until the lane's own live
# test-purchase gate flips them. The six-pack and full-catalog pass name
# no deliverable yet (component guides stage in later waves) and stay dark.
ih_prods = _load_catalog(os.path.join(REPO_ROOT, "data", "ironhead-catalog.csv"),
                         os.path.join(REPO_ROOT, "data", "ironhead-prices.json"),
                         sku_prefixes=["IH-"])
ih_by_sku = {p["sku"]: p for p in ih_prods}
IH_WAVE1 = {"IH-SHOVELHEAD-RESTORATION": 29.95, "IH-CB750-SOHC": 29.95,
            "IH-KZ1000": 29.95, "IH-DUCATI-BEVEL": 29.95,
            "IH-SHOVELHEAD-BUYERS-GUIDE": 19.95}
check("IronHead catalog loads 7 seeded rows", len(ih_prods) == 7, len(ih_prods))
check("IronHead first wave is priced from the canonical lineup, file-backed, staged dark",
      all(s in ih_by_sku and ih_by_sku[s]["price"]["amount"] == amt
          and ih_by_sku[s]["listed"] is False
          and ih_by_sku[s]["purchasable"] is False
          and ih_by_sku[s]["digital_file"].endswith(".pdf")
          for s, amt in IH_WAVE1.items()))
check("IronHead bundles carry no deliverable yet and stay dark",
      all(s in ih_by_sku and ih_by_sku[s]["listed"] is False
          and ih_by_sku[s]["purchasable"] is False
          and not ih_by_sku[s]["digital_file"]
          for s in ("IH-HONDA-SOHC-6PACK", "IH-FULL-CATALOG-PASS")))

with open(os.path.join(REPO_ROOT, "data", "ih-digital-sources.json")) as f:
    _ih_sources = json.load(f)
check("IronHead staging work order: 5 files / 1,245,851 bytes / 0 gaps",
      _ih_sources["storage"]["total_files"] == 5
      and _ih_sources["storage"]["total_bytes"] == 1245851
      and sum(e["bytes"] for e in _ih_sources["files"]) == 1245851
      and _ih_sources["missing_source_skus"] == []
      and all(e["size_verified_against_manifest"] for e in _ih_sources["files"]))

with open(os.path.join(REPO_ROOT, "brands", "skillforge.yaml")) as f:
    _sf_brand_ih = _yaml.safe_load(f)
check("SkillForge service claims IH- and stages the IronHead catalog",
      "IH-" in _sf_brand_ih["brand"]["sku_prefixes"]
      and any("ironhead-catalog.csv" in (c.get("csv") or "")
              for c in _sf_brand_ih["catalog"]["catalogs"]))

# ---------- 7. EverReady lane (checkout migration, Bill 2026-10-02) ----------
# Canonical lineup. Delivery is the STANDARD store token path
# (fulfillment/digital.py): signed, expiring download links emailed on
# payment, files served from the store disk — same as SkillForge/RE.
# (Bill 2026-10-02 superseded the 2026-09-19 per-buyer Drive-sharing
# decision; fulfillment/drive.py stays in the tree dormant, env-gated,
# and routes nothing.) The ER-FCC-001 $37 test-purchase gate passed
# 2026-10-02 (charge -> tax -> byte-identical PDF), then the four
# remaining deliverable rows flipped live; the four file-less rows
# (AUTO/EAP/FPA/CSO) stay dark and unpurchasable by construction.
from fulfillment import drive as drive_mod                     # noqa: E402
import fulfillment.fulfill as _fulfill_mod                     # noqa: E402
from fulfillment.fulfill import fulfill_paid_order             # noqa: E402

os.environ.pop("EVERREADY_DRIVE_ENABLED", None)
os.environ.pop("EVERREADY_DRIVE_CREDENTIALS_JSON", None)

er_prods = _load_catalog(
    os.path.join(REPO_ROOT, "data", "everready-catalog.csv"),
    os.path.join(REPO_ROOT, "data", "everready-prices.json"),
    sku_prefixes=["ER-"])
er_by_sku = {p["sku"]: p for p in er_prods}
# Pinned ER prices: the 9 canonical-lineup amounts verbatim, plus
# ER-FBK-001 Family Bidding Kit (added 2026-10-05, open-bidding-only
# recreation on Bill's call; NOT in the canonical lineup file — $37 is
# the canonical manual-kit tier, cf. Executor's Kit $37).
CANONICAL_PRICES = {
    "ER-FCC-001": 37.0, "ER-EK-001": 37.0, "ER-FRB-001": 49.0,
    "ER-AUTO-001": 49.0, "ER-EAP-001": 49.0, "ER-DAI-001": 19.95,
    "ER-LSIK-001": 37.0, "ER-FPA-001": 49.0, "ER-CSO-001": 49.0,
    "ER-FBK-001": 37.0, "ER-FRC-001": 37.0,
}
check("EverReady catalog loads the 11 priced products "
      "(9 canonical + Family Bidding Kit + Family Recipe Cookbook)",
      set(er_by_sku) == set(CANONICAL_PRICES), str(sorted(er_by_sku)))
check("EverReady prices match the pinned lineup and are confirmed",
      all(er_by_sku[s]["price"]["amount"] == amt
          and er_by_sku[s]["price"]["status"] == "confirmed"
          for s, amt in CANONICAL_PRICES.items()))
FILELESS = {"ER-AUTO-001", "ER-EAP-001", "ER-FPA-001", "ER-CSO-001"}
LIVE_ER = {"ER-FCC-001", "ER-EK-001", "ER-DAI-001", "ER-LSIK-001", "ER-FRB-001"}
check("the five deliverable EverReady products are listed and purchasable "
      "at canonical prices (ER-FCC-001 gate passed 2026-10-02, then flip-all)",
      all(er_by_sku[s]["listed"] is True
          and er_by_sku[s]["purchasable"] is True
          and er_by_sku[s]["price"]["amount"] == CANONICAL_PRICES[s]
          for s in LIVE_ER), str(sorted(LIVE_ER)))
check("the four file-less EverReady rows stay unlisted (dark) and NOT purchasable",
      all(p["listed"] is False and p["purchasable"] is False
          for p in er_prods if p["sku"] in FILELESS),
      str(sorted(FILELESS)))
check("every EverReady row is owned by everready and digital",
      all(p["owner"] == "everready" and p["fulfillment_type"] == "digital"
          for p in er_prods))
FILE_WIRED = {"ER-FCC-001": "family-command-center.pdf",
              "ER-EK-001": "executors-kit.pdf",
              "ER-DAI-001": "digital-asset-inventory-workbook.pdf",
              "ER-LSIK-001": "life-story-interview-kit.pdf"}
check("four EverReady rows carry their disk deliverable",
      all(er_by_sku[s]["digital_file"] == f for s, f in FILE_WIRED.items()),
      str({s: er_by_sku[s]["digital_file"] for s in FILE_WIRED}))
check("the bundle carries no file of its own but names both components",
      er_by_sku["ER-FRB-001"]["digital_file"] == ""
      and er_by_sku["ER-FRB-001"]["bundle_skus"]
      == ["ER-FCC-001", "ER-EK-001"])
check("manual/file-less rows can never be sold (no file, no bundle)",
      all(er_by_sku[s]["digital_file"] == ""
          and er_by_sku[s]["bundle_skus"] == [] for s in FILELESS),
      str(sorted(FILELESS)))
check("the free Five Conversations magnet is NOT a checkout product",
      not any("five" in p["title"].lower() for p in er_prods))

# Family Bidding Kit (2026-10-05): open-bidding-only paper kit, recreated
# after the prior sealed-bid edition was banned by Bill's standing rule.
# Staged listed=0 until the EverReady checkout gate; flipped LIVE
# 2026-10-06 with the five-SKU shelf (gap census F1/F2 fake-hold verdict:
# the gate passed 2026-10-02 and the current-account re-proof runs on
# this row's sibling ER-DAI-001). Deliverable ships in the repo at
# data/digital/family-bidding-kit.pdf.
_fbk = er_by_sku["ER-FBK-001"]
check("Family Bidding Kit carries its disk deliverable",
      _fbk["digital_file"] == "family-bidding-kit.pdf",
      _fbk["digital_file"])
check("Family Bidding Kit live (listed, purchasable) after the "
      "2026-10-06 shelf flip",
      _fbk["listed"] is True and _fbk["purchasable"] is True
      and _fbk["price"]["amount"] == 37.0)
store_app.BY_SKU["ER-FBK-001"] = _fbk
rf = client.post("/api/checkout", json={"items": [{"sku": "ER-FBK-001",
                                                    "qty": 1}]})
check("live Family Bidding Kit is accepted at checkout (session or "
      "Stripe-key error, never 'cannot be sold')",
      rf.status_code != 400 and "cannot be sold" not in
      (rf.get_json() or {}).get("error", ""), rf.status_code)
check("listed Family Bidding Kit serves on the public API",
      client.get("/api/products/ER-FBK-001").status_code == 200)
del store_app.BY_SKU["ER-FBK-001"]

# Family Recipe Cookbook (2026-10-06): the guided build — per-
# contributor card-photo intake, never-guess transcription, confirm-
# and-lock, 8.5x11 photo-left/text-right assembly (backend/
# cookbook.py; the lane itself is covered by test_cookbook.py).
# Bill-confirmed prices: digital build/PDF $37 (this row's checkout
# price), bound printed copy $49 incl. the build, extra bound copies
# $29.95 (print lane). Staged listed=0 until the EverReady checkout
# gate; flipped LIVE 2026-10-06 on Bill's go, exactly like
# ER-FBK-001 earlier that day. The $49/$29.95 bound copies are not a
# checkout SKU — they stay dark until the Lulu paid proof passes.
_frc = er_by_sku["ER-FRC-001"]
check("Family Recipe Cookbook carries its disk deliverable",
      _frc["digital_file"] == "family-recipe-cookbook.pdf",
      _frc["digital_file"])
check("Family Recipe Cookbook live (listed, purchasable) at the "
      "Bill-confirmed $37 after the 2026-10-06 listing go",
      _frc["listed"] is True and _frc["purchasable"] is True
      and _frc["price"]["amount"] == 37.0)
store_app.BY_SKU["ER-FRC-001"] = _frc
rf = client.post("/api/checkout", json={"items": [{"sku": "ER-FRC-001",
                                                    "qty": 1}]})
check("live Family Recipe Cookbook is accepted at checkout (session or "
      "Stripe-key error, never 'cannot be sold')",
      rf.status_code != 400 and "cannot be sold" not in
      (rf.get_json() or {}).get("error", ""), rf.status_code)
check("listed Family Recipe Cookbook serves on the public API",
      client.get("/api/products/ER-FRC-001").status_code == 200)
del store_app.BY_SKU["ER-FRC-001"]

with open(os.path.join(REPO_ROOT, "brands", "everready.yaml")) as f:
    _er_brand = _yaml.safe_load(f)
check("EverReady brand yaml claims ER- and stages the everready catalog",
      "ER-" in _er_brand["brand"]["sku_prefixes"]
      and any("everready-catalog.csv" in (c.get("csv") or "")
              for c in _er_brand["catalog"]["catalogs"])
      and "download" in (_er_brand.get("fulfillment") or {}).get("delivery", ""))
check("EverReady brand yaml has NO Connect account routing",
      "stripe_connect" not in json.dumps(_er_brand.get("store", {})))

with open(os.path.join(REPO_ROOT, "brands", "skillforge.yaml")) as f:
    _sf_brand = _yaml.safe_load(f)
check("the shared skillforge service loads ER rows dark (same store)",
      "ER-" in _sf_brand["brand"]["sku_prefixes"]
      and any("everready-catalog.csv" in (c.get("csv") or "")
              for c in _sf_brand["catalog"]["catalogs"]))

# a dark (file-less) ER row cannot be sold even when loaded into the live catalog
store_app.BY_SKU["ER-EAP-001"] = er_by_sku["ER-EAP-001"]
r = client.post("/api/checkout", json={"items": [{"sku": "ER-EAP-001",
                                                   "qty": 1}]})
check("dark EverReady SKU is rejected at checkout (400)",
      r.status_code == 400 and "cannot be sold" in
      (r.get_json() or {}).get("error", ""), r.status_code)
check("unlisted EverReady SKU 404s on the public API",
      client.get("/api/products/ER-EAP-001").status_code == 404)
del store_app.BY_SKU["ER-EAP-001"]

# drive.py is dormant: unconfigured it can only dry-run, gate-on without
# credentials is still a loud error, and the fulfillment router no
# longer imports it — ER orders can never depend on it again.
dry_client = drive_mod.DriveShareClient(credentials_json="", enabled=False)
check("dormant Drive module: no credentials => dry-run only",
      dry_client.dry_run is True
      and dry_client.share("fileX", "buyer@example.com") == {"dry_run": True})
try:
    drive_mod.DriveShareClient(credentials_json="", enabled=True)
    check("dormant Drive module: gate ON without credentials stays loud", False)
except drive_mod.DriveConfigError as e:
    check("dormant Drive module: gate ON without credentials stays loud",
          "EVERREADY_DRIVE_CREDENTIALS_JSON" in str(e), str(e))
check("fulfill.py no longer routes anything through Drive",
      not hasattr(_fulfill_mod, "drive_mod"))


# fulfillment: ER lines take the standard token path — one signed link
# per deliverable file, the bundle's two links in its ONE fulfillment.
def _er_order(session, lines):
    return fulfill_paid_order(
        stripe_session_id=session, customer_email="buyer@example.com",
        shipping_address={}, cart_lines=lines,
        products_by_sku=er_by_sku, mapping_path="unused.json",
        order_prefix="er", download_base_url="https://example.invalid",
        store_name="EverReady Family")


_er_signer = dmod.DownloadTokenSigner()


def _link_skus(result):
    return [ _er_signer.verify(link.rsplit("/download/", 1)[1])["sku"]
             for link in result["digital"]["links"] ]


fcc_order = _er_order("cs_test_er_tok_1", [{"sku": "ER-FCC-001", "qty": 1}])
check("fulfill_paid_order routes EverReady to the token path",
      isinstance(fcc_order, dict) and "digital" in fcc_order
      and "everready_drive" not in fcc_order
      and fcc_order.get("printful") is None
      and len(fcc_order["digital"]["links"]) == 1
      and _link_skus(fcc_order) == ["ER-FCC-001"]
      and fcc_order["digital"]["email_dry_run"] is True,
      str(fcc_order))

bundle_order = _er_order("cs_test_er_tok_2", [{"sku": "ER-FRB-001", "qty": 1}])
check("the bundle delivers BOTH component files in one fulfillment",
      isinstance(bundle_order, dict)
      and len(bundle_order["digital"]["links"]) == 2
      and _link_skus(bundle_order) == ["ER-FCC-001", "ER-EK-001"]
      and bundle_order["digital"]["items"]
      == ["Family Command Center", "Executor's Kit"],
      str(bundle_order))

replay = _er_order("cs_test_er_tok_2", [{"sku": "ER-FRB-001", "qty": 1}])
check("replayed ER session emails nothing twice (ledger wall)",
      replay["digital"].get("already_fulfilled") is True,
      str(replay))

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL DIGITAL TESTS PASSED")
