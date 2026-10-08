"""Beta-delivery endpoint gates (2026-10-08).

Covers the promises of backend/beta_delivery.py without touching Kit,
Brevo, or real storage: auth wall, deterministic file resolution
(RE exact-car match, RE honest fallback, fixed IronHead/Stitchfolk/
SkillForge files, EverReady free checklist), one delivery record per
(email, brand, sku), and no second email on a repeat trigger.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["BETA_DELIVERY_TOKEN"] = "test-trigger-token"

from flask import Flask  # noqa: E402

import beta_delivery as beta  # noqa: E402
import leads as leads_mod  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


PRODUCTS = {
    "RE-GD-CHEVROLET-CHEVELLE-1969": {
        "sku": "RE-GD-CHEVROLET-CHEVELLE-1969",
        "title": "1969 Chevrolet Chevelle Restoration Guide",
        "owner": "restorationessentials",
        "fulfillment_type": "digital",
        "digital_file": "RE_chevrolet-chevelle-1969.pdf",
    },
    "RE-GD-1969-FORD-MUSTANG": {
        "sku": "RE-GD-1969-FORD-MUSTANG",
        "title": "1969 Ford Mustang Restoration Guide",
        "owner": "restorationessentials",
        "fulfillment_type": "digital",
        "digital_file": "RE_ford-mustang-1969.pdf",
    },
    "IH-CB750-SOHC": {
        "sku": "IH-CB750-SOHC",
        "title": "Honda CB750 SOHC Restoration Guide",
        "owner": "ironhead",
        "fulfillment_type": "digital",
        "digital_file": "ih-cb750-sohc-restoration-guide.pdf",
    },
    "ST-PETAL-SHAWL-001": {
        "sku": "ST-PETAL-SHAWL-001",
        "title": "Stitchfolk Knit Shawl, Petal Crescent Shawl",
        "owner": "stitchfolk",
        "fulfillment_type": "digital",
        "digital_file": "Stitchfolk_PetalCrescentShawl_Pattern.pdf",
    },
    "SF-ELEC-001": {
        "sku": "SF-ELEC-001",
        "title": "The Electrician AI Playbook",
        "owner": "skillforge",
        "fulfillment_type": "digital",
        "digital_file": "electricians-playbook-final.pdf",
    },
}

KNOWN_FILES = {p["digital_file"] for p in PRODUCTS.values()}
KNOWN_FILES.add(beta.FALLBACK_FILE)


class FakeStorage:
    is_local = True

    def exists(self, name):
        return name in KNOWN_FILES


class FakeSigner:
    def mint(self, sku, email):
        return f"tok.{sku}.{email}"


class FakeSender:
    dry_run = False
    sent = []

    def send(self, to_email, subject, html_content, text_content):
        FakeSender.sent.append({
            "to": to_email, "subject": subject,
            "html": html_content, "text": text_content})
        return {"messageId": "fake-msg-1"}


def main():
    tmp = tempfile.mkdtemp(prefix="beta-delivery-test-")
    leads_mod.DB_PATH = os.path.join(tmp, "leads.db")
    leads_mod.init_db()

    app = Flask(__name__)
    beta.init(
        app,
        root_dir=tmp,
        products_by_sku=PRODUCTS,
        public_base_url=lambda: "https://store.example",
        digital_files_dir=lambda: tmp,
        sender_factory=lambda sender_name=None: FakeSender(),
        signer_factory=lambda: FakeSigner(),
        storage_factory=lambda: FakeStorage(),
    )
    client = app.test_client()
    hdr = {"X-Beta-Delivery-Token": "test-trigger-token"}

    # Auth wall: no token, no delivery, no email.
    r = client.post("/api/beta-deliver",
                    json={"email": "a@example.com", "brand": "skillforge"})
    check("unauthorized trigger rejected (401)", r.status_code == 401)
    check("unauthorized trigger sent nothing", len(FakeSender.sent) == 0)

    # SkillForge fixed file.
    r = client.post("/api/beta-deliver", headers=hdr, json={
        "email": "pro@example.com", "brand": "skillforge",
        "fields": {"trade": "electrician"}})
    body = r.get_json()
    check("skillforge delivery sent", r.status_code == 200
          and body.get("status") == "sent" and body.get("sku") == "SF-ELEC-001")
    check("skillforge email carries token link",
          "https://store.example/download/tok.SF-ELEC-001.pro@example.com"
          in FakeSender.sent[-1]["text"])
    check("skillforge email signed Bill Morton",
          "Bill Morton" in FakeSender.sent[-1]["text"])
    check("skillforge email asks for feedback",
          "reply" in FakeSender.sent[-1]["text"].lower())

    # Idempotency: same email+brand never re-sends.
    r = client.post("/api/beta-deliver", headers=hdr, json={
        "email": "pro@example.com", "brand": "skillforge"})
    check("repeat trigger is already_delivered",
          r.get_json().get("status") == "already_delivered")
    check("repeat trigger sent no second email", len(FakeSender.sent) == 1)

    # RE exact-car match.
    r = client.post("/api/beta-deliver", headers=hdr, json={
        "email": "car@example.com", "brand": "restorationessentials",
        "fields": {"car": "1969 Chevrolet Chevelle"}})
    body = r.get_json()
    check("RE exact car matched to its guide",
          body.get("sku") == "RE-GD-CHEVROLET-CHEVELLE-1969")
    check("RE exact email has no fallback note",
          "couldn't match" not in FakeSender.sent[-1]["text"].lower())

    # RE no car -> honest fallback.
    r = client.post("/api/beta-deliver", headers=hdr, json={
        "email": "nocar@example.com", "brand": "restorationessentials"})
    body = r.get_json()
    check("RE missing car falls back to Master Checklist",
          body.get("sku") == beta.FALLBACK_SKU)
    check("RE fallback email is honest about why",
          "didn't include a car" in FakeSender.sent[-1]["text"])

    # RE unmatched car -> honest fallback naming what they sent.
    r = client.post("/api/beta-deliver", headers=hdr, json={
        "email": "odd@example.com", "brand": "restorationessentials",
        "fields": {"car": "1955 Packard Caribbean"}})
    body = r.get_json()
    check("RE unmatched car falls back", body.get("sku") == beta.FALLBACK_SKU)
    check("RE unmatched email names the unmatched car",
          "Packard" in FakeSender.sent[-1]["text"])

    # Kit webhook envelope: IronHead tag event.
    r = client.post("/api/kit-beta-webhook", headers=hdr, json={
        "delivery_id": 1,
        "events": [{
            "id": "evt-1", "type": "subscriber.tag_added",
            "data": {
                "subscriber": {"email_address": "rider@example.com",
                               "first_name": "Rae", "fields": {}},
                "tag": {"id": 23454694, "name": "Beta - IronHead"}}}],
    })
    body = r.get_json()
    check("webhook IronHead event delivered CB750 volume",
          r.status_code == 200
          and body["results"][0].get("sku") == "IH-CB750-SOHC")
    check("IronHead email honors every-volume-free promise",
          "every volume free" in FakeSender.sent[-1]["text"])

    # Kit webhook: unrelated tag is ignored, never an error.
    r = client.post("/api/kit-beta-webhook", headers=hdr, json={
        "events": [{
            "id": "evt-2", "type": "subscriber.tag_added",
            "data": {"subscriber": {"email_address": "x@example.com"},
                     "tag": {"id": 12345}}}]})
    check("unrelated tag ignored with 200",
          r.status_code == 200
          and body_or(r).get("results", [{}])[0].get("status") == "ignored")

    # Stitchfolk fixed pattern via direct call.
    r = client.post("/api/beta-deliver", headers=hdr, json={
        "email": "knit@example.com", "brand": "stitchfolk"})
    check("stitchfolk delivery is the Petal Crescent Shawl",
          r.get_json().get("sku") == "ST-PETAL-SHAWL-001")

    # EverReady checklist: free magnet, public link, no token.
    r = client.post("/api/kit-beta-webhook", headers=hdr, json={
        "events": [{
            "id": "evt-3", "type": "subscriber.tag_added",
            "data": {"subscriber": {"email_address": "family@example.com"},
                     "tag": {"id": 24224974}}}]})
    check("everready checklist delivered",
          r.status_code == 200
          and body_or(r).get("results", [{}])[0].get("sku")
          == beta.CHECKLIST_SKU)
    check("checklist email links the free PDF directly",
          beta.CHECKLIST_URL in FakeSender.sent[-1]["text"])

    # Delivery records landed in the shared DB, one row per delivery.
    conn = sqlite3.connect(leads_mod.DB_PATH)
    rows = conn.execute(
        "SELECT email, brand, sku, status FROM beta_deliveries"
        " ORDER BY brand").fetchall()
    conn.close()
    check("delivery records persisted (7 sends)",
          len(rows) == 7 and all(row[3] == "sent" for row in rows))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("ALL BETA-DELIVERY CHECKS PASSED")
    return 0


def body_or(resp):
    return resp.get_json() or {}


if __name__ == "__main__":
    sys.exit(main())
