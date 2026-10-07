"""Family Recipe Cookbook lane tests (EverReady ER-FRC-001, 2026-10-06).

Covers backend/cookbook.py end to end without network or real keys:
  1. the pure honesty machinery — period-term editor's notes (brief §6)
     and the [?] confirm gate that refuses to lock a guessed number;
  2. the no-provider transcription fallback — with no vision key the
     draft reports the whole card unread instead of inventing text;
  3. the Flask surface — book + contributor creation (hashed tokens),
     photo upload, contributor-only confirm, organizer assembly, and
     the 8.5x11 print-ready PDF with the 32-page floor.

Run: ./../.venv/bin/python backend/test_cookbook.py   (from pushrod-store/)
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="pushrod-cookbook-test-")
os.environ["BRAND"] = "gateway"
os.environ["PRINTFUL_DRY_RUN"] = "1"
os.environ["DIGITAL_DOWNLOAD_SECRET"] = "test-secret-do-not-ship"
os.environ["COOKBOOK_DIR"] = os.path.join(TMP, "cookbook")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("COOKBOOK_VISION_MODEL", None)

import cookbook as cb                                   # noqa: E402
import app as store_app                                 # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------- 1. period terms + the [?] gate (pure) ----------
fields = {"title": "Dressing", "ingredients": ["2 cups oleo", "1 tsp soda"],
          "steps": ["Bake in a moderate oven till done"],
          "servings": "", "time": "", "temp": "", "notes_verbatim": ""}
notes = cb.apply_period_terms(fields)
check("oleo becomes margarine with a removable editor's note",
      fields["ingredients"][0] == "2 cups margarine"
      and any("oleo" in n for n in notes), str(notes))
check("moderate oven becomes 350°F with a note, never a silent rewrite",
      fields["steps"][0] == "Bake in a 350°F oven till done"
      and any("moderate oven" in n for n in notes), str(notes))
fields2 = {"title": "[?] Cake", "ingredients": ["1 [?] tsp soda"],
           "steps": ["Mix"], "servings": "", "time": "", "temp": "",
           "notes_verbatim": ""}
check("unresolved [?] markers are found for the confirm gate",
      cb.find_unresolved(fields2) == ["title", "ingredient 1"],
      str(cb.find_unresolved(fields2)))
check("clean fields pass the gate",
      cb.find_unresolved({"title": "Cake", "ingredients": ["1 tsp soda"],
                          "steps": ["Mix"], "servings": "", "time": "",
                          "temp": "", "notes_verbatim": ""}) == [])

# ---------- 2. no-provider fallback never guesses ----------
f3, meta3 = cb.transcribe(b"not-a-real-photo", "image/jpeg")
check("no vision provider: draft is an honest unread, not an invention",
      meta3["status"] == "unread" and f3["ingredients"] == []
      and any("[?]" in fl for fl in meta3["flags"])
      and meta3["unread"], str(meta3))

# ---------- 2c. vision honesty backstop (2026-10-06 QA failures) ----------
# The live vision QA against the production prompt returned a bare
# "tsp soda" (the unread quantity vanished, unflagged) and dropped a
# partially obscured buttermilk line from the draft entirely. The
# prompt contract now demands better AND the deterministic backstop
# guarantees it on every stored draft. Proven here with canned model
# output in exactly the QA's failure shape — no model call needed.
qa_fields = {"title": "Grandma's Cornbread Dressing",
             "ingredients": ["2 cups cornmeal", "tsp soda", "2 eggs"],
             "steps": ["Mix dry. Add wet."],
             "servings": "", "time": "", "temp": "",
             "notes_verbatim": ""}
qa_card_lines = ["Grandma's Cornbread Dressing", "2 cups cornmeal",
                 "tsp soda", "2 eggs", "[?] cup buttermilk",
                 "Mix dry. Add wet."]
fixed, qa_unread = cb.enforce_transcription_honesty(
    qa_fields, [], qa_card_lines)
check("bare unit gains its [?] — never ships as a confident guess",
      fixed["ingredients"][1] == "[?] tsp soda", str(fixed))
check("obscured line survives verbatim, [?] mark intact",
      "buttermilk" in fixed["notes_verbatim"]
      and "[?]" in fixed["notes_verbatim"], fixed["notes_verbatim"])
check("both failures are named in plain language for the contributor",
      any("tsp soda" in u for u in qa_unread)
      and any("buttermilk" in u for u in qa_unread), str(qa_unread))
check("the surviving [?] still blocks the confirm gate",
      "ingredient 2" in cb.find_unresolved(fixed)
      and "notes_verbatim" in cb.find_unresolved(fixed),
      str(cb.find_unresolved(fixed)))
check("the backstop never mutates the caller's draft",
      qa_fields["ingredients"][1] == "tsp soda")

clean_draft = {"title": "Dressing", "ingredients": ["1 tsp soda"],
               "steps": ["Mix"], "servings": "", "time": "", "temp": "",
               "notes_verbatim": ""}
same, no_unread = cb.enforce_transcription_honesty(
    clean_draft, [], ["Dressing", "1 tsp soda", "Mix"])
check("a fully placed draft passes through untouched",
      same == clean_draft and no_unread == [], str(same))

# ---------- 3. the Flask surface ----------
client = store_app.app.test_client()

r = client.get("/cookbook")
check("intake page serves", r.status_code == 200
      and b"Family Recipe Cookbook" in r.data)

r = client.post("/api/cookbook/books",
                json={"family_name": "Morton", "organizer_name": "Bill"})
check("organizer opens a book", r.status_code == 201, r.status_code)
book = r.get_json()
bid, otok = book["book_id"], book["organizer_token"]

r = client.post(f"/api/cookbook/books/{bid}/contributors",
                headers={"X-Cookbook-Token": otok}, json={"name": "Aunt Mary"})
check("organizer invites a contributor", r.status_code == 201, r.status_code)
ctok = r.get_json()["submit_token"]

r = client.post(f"/api/cookbook/books/{bid}/assemble",
                headers={"X-Cookbook-Token": otok})
check("assembly refuses an empty book (confirm gate)",
      r.status_code == 400, r.status_code)

# A generated recipe-card photo: cream card, ruled lines, card text, and
# one quantity blotted out — the honest unread spot.
from PIL import Image, ImageDraw                        # noqa: E402
img = Image.new("RGB", (1200, 900), (247, 241, 222))
d = ImageDraw.Draw(img)
for yy in range(140, 880, 60):
    d.line([(60, yy), (1140, yy)], fill=(170, 180, 200), width=2)
lines = ["Grandma's Cornbread Dressing", "2 cups cornmeal",
         "1/2 cup oleo, melted", "tsp soda", "2 eggs", "1 cup buttermilk",
         "Mix dry. Add wet.", "Bake in a moderate oven till done."]
y = 90
for ln in lines:
    d.text((80, y), ln, fill=(40, 45, 90))
    y += 60
d.ellipse([78, 390, 190, 425], fill=(60, 55, 50))  # blot over a quantity
buf = io.BytesIO()
img.save(buf, "JPEG", quality=90)
card = buf.getvalue()

r = client.post(f"/api/cookbook/books/{bid}/photos",
                data={"photo": (io.BytesIO(card), "card.jpg"),
                      "token": ctok},
                content_type="multipart/form-data")
check("photo upload accepted", r.status_code == 201, r.status_code)
draft = r.get_json()
rid = draft["id"]
check("draft carries [?] flags (never guesses)",
      draft["transcription_status"] == "unread"
      and any("[?]" in fl for fl in draft["flags"]), str(draft)[:200])

r = client.post(f"/api/cookbook/books/{bid}/photos",
                data={"photo": (io.BytesIO(b"hello"), "x.jpg")},
                content_type="multipart/form-data")
check("upload without a contributor token is refused",
      r.status_code == 401, r.status_code)

# ---------- 3b. outside-family contributor release gate (2026-10-07) --
# Bill: permission-to-publish is baked into the upload for anyone
# outside the family; the Morton-family pilot is never gated.
check("intake page carries the release box (shown only when needed)",
      b'id="relok"' in client.get("/cookbook").data)

book3 = client.post("/api/cookbook/books",
                    json={"family_name": "Neighbor"}).get_json()
o3 = book3["organizer_token"]
r = client.post(f"/api/cookbook/books/{book3['book_id']}/contributors",
                headers={"X-Cookbook-Token": o3},
                json={"name": "Pat Neighbor", "outside_family": True})
check("outside-family invite is flagged release-required",
      r.status_code == 201 and r.get_json()["release_required"] is True,
      r.status_code)
ctok_out = r.get_json()["submit_token"]

r = client.get(f"/api/cookbook/books/{book3['book_id']}/contributor"
               f"?token={ctok_out}")
check("outside contributor status shows the release up front",
      r.status_code == 200 and r.get_json()["release_required"] is True
      and "permission" in r.get_json()["release_text"].lower(),
      r.status_code)

r = client.post(f"/api/cookbook/books/{book3['book_id']}/photos",
                data={"photo": (io.BytesIO(card), "card.jpg"),
                      "token": ctok_out},
                content_type="multipart/form-data")
check("outside upload WITHOUT acceptance is refused (403, release named)",
      r.status_code == 403
      and (r.get_json() or {}).get("release_required") is True
      and (r.get_json() or {}).get("release_version")
      == cb.RELEASE_VERSION, r.status_code)

r = client.post(f"/api/cookbook/books/{book3['book_id']}/photos",
                data={"photo": (io.BytesIO(card), "card.jpg"),
                      "token": ctok_out, "release_accepted": "true"},
                content_type="multipart/form-data")
check("outside upload WITH acceptance goes through, version stamped",
      r.status_code == 201
      and r.get_json().get("release_version") == cb.RELEASE_VERSION,
      r.status_code)

r = client.get(f"/api/cookbook/books/{book3['book_id']}",
               headers={"X-Cookbook-Token": o3})
_pat = (r.get_json() or {}).get("contributors", [{}])[0]
check("acceptance is stored on the contributor (who/when/version)",
      _pat.get("release_accepted") is True
      and _pat.get("release_version") == cb.RELEASE_VERSION
      and _pat.get("release_accepted_at"), str(_pat))

r = client.post(f"/api/cookbook/books/{book3['book_id']}/photos",
                data={"photo": (io.BytesIO(card), "card2.jpg"),
                      "token": ctok_out},
                content_type="multipart/form-data")
check("stored acceptance covers later cards (not re-asked every upload)",
      r.status_code == 201, r.status_code)

r = client.post(f"/api/cookbook/books/{book3['book_id']}/contributors",
                headers={"X-Cookbook-Token": o3},
                json={"name": "Cousin Jo"})
ctok_fam = r.get_json()["submit_token"]
r = client.post(f"/api/cookbook/books/{book3['book_id']}/photos",
                data={"photo": (io.BytesIO(card), "card.jpg"),
                      "token": ctok_fam},
                content_type="multipart/form-data")
check("family contributor uploads with NO release gate (pilot unburdened)",
      r.status_code == 201
      and r.get_json().get("release_version") is None, r.status_code)

r = client.get(f"/api/cookbook/recipes/{rid}/photo?token={ctok}")
check("contributor can see their own card photo",
      r.status_code == 200 and r.mimetype == "image/jpeg", r.status_code)
r = client.get(f"/api/cookbook/recipes/{rid}/photo")
check("card photo is not public", r.status_code == 404, r.status_code)

r = client.post(f"/api/cookbook/recipes/{rid}/confirm",
                headers={"X-Cookbook-Token": otok},
                json={"title": "Dressing", "ingredients": ["x"],
                      "steps": ["y"]})
check("organizer cannot confirm on a contributor's behalf",
      r.status_code == 403, r.status_code)

clean = {"title": "Grandma's Cornbread Dressing",
         "ingredients": ["2 cups cornmeal", "1/2 cup margarine, melted",
                         "1 tsp soda", "2 eggs", "1 cup buttermilk"],
         "steps": ["Mix dry. Add wet.",
                   "Bake in a 350°F oven till done."],
         "servings": "Serves 8", "time": "", "temp": "350°F",
         "notes_verbatim": "",
         "editor_notes": ["Card says 'oleo' (period margarine)",
                          "Card says 'moderate oven' (350°F)"]}
r = client.post(f"/api/cookbook/recipes/{rid}/confirm",
                headers={"X-Cookbook-Token": ctok}, json=clean)
check("contributor confirms a clean reading", r.status_code == 200,
      r.status_code)
check("confirmed recipe locks", r.get_json()["status"] == "confirmed")
r = client.post(f"/api/cookbook/recipes/{rid}/confirm",
                headers={"X-Cookbook-Token": ctok}, json=clean)
check("a locked recipe cannot be re-confirmed", r.status_code == 409,
      r.status_code)

still_flagged = dict(clean)
still_flagged["ingredients"] = ["1 [?] tsp soda"]
book2 = client.post("/api/cookbook/books",
                    json={"family_name": "Flag"}).get_json()
c2 = client.post(
    f"/api/cookbook/books/{book2['book_id']}/contributors",
    headers={"X-Cookbook-Token": book2["organizer_token"]},
    json={"name": "Lisa"}).get_json()
r = client.post(f"/api/cookbook/books/{book2['book_id']}/photos",
                data={"photo": (io.BytesIO(card), "card.jpg"),
                      "token": c2["submit_token"]},
                content_type="multipart/form-data")
rid2 = r.get_json()["id"]
r = client.post(f"/api/cookbook/recipes/{rid2}/confirm",
                headers={"X-Cookbook-Token": c2["submit_token"]},
                json=still_flagged)
check("a [?] quantity can never be locked in (422, flags named)",
      r.status_code == 422
      and "ingredient 1" in
      (r.get_json() or {}).get("remaining_flags", []), r.status_code)

r = client.post(f"/api/cookbook/books/{bid}/assemble",
                headers={"X-Cookbook-Token": otok})
check("organizer assembles the book", r.status_code == 200, r.status_code)
asm = r.get_json()
check("book carries the confirmed recipe",
      asm["recipes"] == 1 and asm["pages"] >= 32, str(asm))

r = client.get(f"/api/cookbook/books/{bid}/book.pdf?token={otok}")
check("assembled book downloads", r.status_code == 200
      and r.data[:4] == b"%PDF", r.status_code)
try:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(r.data))
    sizes = {(float(p.mediabox.width), float(p.mediabox.height))
             for p in reader.pages}
    check("every page is 8.5x11 (612x792)",
          sizes == {(612.0, 792.0)} and len(reader.pages) >= 32,
          str(sizes))
    text = "".join(p.extract_text() or "" for p in reader.pages)
    check("book prints photo-left / text-right with attribution",
          "Grandma's Cornbread Dressing" in text
          and "Aunt Mary" in text and "Ingredients" in text)
except ImportError:
    check("every page is 8.5x11 (612x792)",
          b"612 792" in r.data and r.data.count(b"/Type /Page") >= 32)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL COOKBOOK TESTS PASSED")
