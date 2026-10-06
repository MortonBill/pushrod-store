"""
Family Recipe Cookbook — intake + assembly (EverReady ER-FRC-001).

The cookbook lane of the store app. A family organizer opens a book,
invites contributors, and each contributor photographs handwritten
recipe cards. Every photo runs through a vision transcription that
emits a standardized recipe draft — and NEVER guesses: any uncertain
word or number is returned inline as "[?]" for the contributor who
submitted the card to resolve. Nothing enters the finished book until
its contributor has reviewed the draft against their photo and locked
it. Confirmed recipes assemble into a print-ready 8.5x11 PDF: original
card photo on the left page, clean transcription on the right, in
sections by contributor (brief §5-§7).

Design rules (brief §6, hard requirements):
  - Flag, never guess. Numbers (quantities, temperatures, times) are
    the highest-risk characters in bad cursive and flag first.
  - Period terms are interpreted as REMOVABLE editor's notes, never
    silently rewritten ("oleo" -> margarine + note). Vague-by-design
    measures ("a pinch", "bake till done") are data, kept as-is.
  - The organizer cannot confirm on a contributor's behalf.
  - Family content is private: every route is token-gated, tokens are
    stored only as SHA-256 hashes, books are never publicly listed.

Storage is JSON-per-book under COOKBOOK_DIR (default
<root>/data/cookbook): books/<id>.json, uploads/<book>/<recipe>.jpg,
books/<id>.pdf. Transcription provider: ANTHROPIC_API_KEY env enables
the vision model (model via COOKBOOK_VISION_MODEL); with no provider
configured the draft honestly reports the whole card unread rather
than inventing a single character. No key ever lives in code.

MVP scope (brief §9): fixed EverReady template, link-based intake,
contributor confirm, organizer assembly. Bound printing rides the
existing Lulu hookup later (separate go-live; not here).
"""
import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time

from flask import Response, jsonify, request, send_file

log = logging.getLogger("pushrod.cookbook")

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)

MAX_PHOTO_BYTES = 15 * 1024 * 1024
PAGE_W, PAGE_H = 612.0, 792.0          # 8.5x11in at 72dpi
MIN_BOOK_PAGES = 32                    # perfect-bound print floor (brief §8)

_lock = threading.Lock()
_public_base_url = None


# ---------- storage ----------

def _dir():
    d = os.environ.get("COOKBOOK_DIR", os.path.join(ROOT, "data", "cookbook"))
    os.makedirs(os.path.join(d, "books"), exist_ok=True)
    os.makedirs(os.path.join(d, "uploads"), exist_ok=True)
    return d


def _book_path(book_id):
    return os.path.join(_dir(), "books", f"{book_id}.json")


def _new_id(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


def _hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def _load_book(book_id):
    path = _book_path(book_id)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _save_book(book):
    path = _book_path(book["id"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(book, f, indent=1)
    os.replace(tmp, path)


def _photo_path(book_id, recipe_id):
    return os.path.join(_dir(), "uploads", book_id, f"{recipe_id}.jpg")


# ---------- text hygiene ----------

_VULGAR = {"⅓": "1/3", "⅔": "2/3", "⅛": "1/8", "⅜": "3/8",
           "⅝": "5/8", "⅞": "7/8", "⅕": "1/5", "⅖": "2/5"}


def clean_text(text):
    """Normalize characters the print fonts can't carry; keep ¼ ½ ¾
    (WinAnsi-safe) and the degree sign. Never alters wording."""
    if text is None:
        return ""
    out = str(text)
    for k, v in _VULGAR.items():
        out = out.replace(k, v)
    return out.replace("°F", "°F").strip()


# Period language (brief §6 table): clean text shows the modern reading,
# an editor's note preserves what the card actually said. Notes are data
# the contributor may remove at confirm — nothing is silently rewritten.
_PERIOD_RULES = [
    (re.compile(r"\boleo\b", re.I), "margarine",
     "Card says 'oleo' (period margarine)"),
    (re.compile(r"\bmoderate oven\b", re.I), "350°F oven",
     "Card says 'moderate oven' (350°F)"),
    (re.compile(r"\bslow oven\b", re.I), "300°F oven",
     "Card says 'slow oven' (300°F)"),
    (re.compile(r"\bquick oven\b", re.I), "400°F oven",
     "Card says 'quick oven' (400°F)"),
    (re.compile(r"\bbutter (?:the )?size of an egg\b", re.I), "~4 Tbsp butter",
     "Card says butter 'the size of an egg' (~4 Tbsp)"),
    (re.compile(r"\bscant\b", re.I), "scant (slightly less than level)",
     "Card says 'scant' — kept as written"),
    (re.compile(r"\bheaping\b", re.I), "heaping (slightly more than level)",
     "Card says 'heaping' — kept as written"),
]


def apply_period_terms(fields):
    """Apply the period-term table to a transcription draft in place.
    Returns the list of editor's notes added. Pure and deterministic so
    the table is unit-testable without any model call."""
    notes = []

    def _sub(line):
        for rx, repl, note in _PERIOD_RULES:
            if rx.search(line):
                line = rx.sub(repl, line)
                if note not in notes:
                    notes.append(note)
        return line

    fields["ingredients"] = [_sub(clean_text(x))
                             for x in fields.get("ingredients", [])]
    fields["steps"] = [_sub(clean_text(x)) for x in fields.get("steps", [])]
    for key in ("title", "servings", "time", "temp", "notes_verbatim"):
        fields[key] = clean_text(fields.get(key, ""))
    return notes


# ---------- transcription ----------

VISION_PROMPT = """You transcribe ONE photographed handwritten family recipe card.

Return ONLY a JSON object, no prose, with exactly these keys:
{"title": str, "ingredients": [str], "steps": [str], "servings": str,
 "time": str, "temp": str, "notes_verbatim": str, "unread": [str]}

Rules — NEVER GUESS:
- Every word or number you cannot read with confidence goes in the text
  as "[?]" exactly where it belongs (e.g. "1 [?] tsp soda"), and is also
  listed in "unread" in plain language ("the quantity before 'tsp soda'").
- Numbers are guilty until proven innocent: quantities, temperatures,
  times and pan sizes flag at ANY doubt (1 vs 7, 1/4 vs 1/2 in cursive).
- No title on the card -> title is "[Untitled — contributor to name]".
- Ingredients: one per line, quantity + unit + item, as written.
- Steps: in written order, one step per entry.
- "a pinch", "a dash", "some", "bake till done" are vague BY DESIGN:
  keep them verbatim; they are not uncertainties.
- Old terms (oleo, moderate oven, scant) are copied verbatim — the
  system annotates them later; do not modernize them yourself.
- Anything on the card that fits no field goes in "notes_verbatim"
  word-for-word ("bake in the blue pan").
"""


def _fallback_draft():
    fields = {"title": "[Untitled — contributor to name]",
              "ingredients": [], "steps": [], "servings": "",
              "time": "", "temp": "", "notes_verbatim": ""}
    return fields, {
        "status": "unread",
        "provider": None,
        "flags": ["title [?]", "ingredients [?]", "steps [?]"],
        "unread": ["the whole card — automatic transcription is not "
                   "switched on for this workspace yet. Nothing was "
                   "guessed: type the recipe from your photo and it "
                   "locks in exactly as you write it."],
    }


def _anthropic_draft(image_bytes, media_type):
    """Vision transcription via the Anthropic Messages API. The key
    comes from the ANTHROPIC_API_KEY environment variable only — it is
    never stored in code, catalog data, or this repo."""
    import base64

    import requests

    model = os.environ.get("COOKBOOK_VISION_MODEL", "claude-sonnet-4-5")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 2000,
              "messages": [{"role": "user", "content": [
                  {"type": "image", "source": {
                      "type": "base64", "media_type": media_type,
                      "data": base64.b64encode(image_bytes).decode()}},
                  {"type": "text", "text": VISION_PROMPT}]}]},
        timeout=90)
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in
                   resp.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("vision model returned no JSON object")
    raw = json.loads(m.group(0))
    fields = {"title": raw.get("title", ""), "ingredients": raw.get("ingredients", []),
              "steps": raw.get("steps", []), "servings": raw.get("servings", ""),
              "time": raw.get("time", ""), "temp": raw.get("temp", ""),
              "notes_verbatim": raw.get("notes_verbatim", "")}
    blob = json.dumps(fields)
    flags = sorted(set(re.findall(r"[^\"]*?\[\?\]", blob)))
    return fields, {"status": "transcribed", "provider": "anthropic",
                    "flags": flags,
                    "unread": [str(u) for u in raw.get("unread", [])]}


def transcribe(image_bytes, media_type="image/jpeg"):
    """Photo -> recipe draft. Returns (fields, meta). The honesty
    contract: with no provider configured, the draft says so and flags
    everything — it never invents a character."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            fields, meta = _anthropic_draft(image_bytes, media_type)
        except Exception as e:  # provider down: honest unread, no guess
            log.warning("cookbook vision transcription failed: %r", e)
            fields, meta = _fallback_draft()
            meta["unread"] = ["automatic transcription failed just now — "
                               "nothing was guessed. You can type the "
                               "recipe from your photo, or ask the "
                               "organizer to retry."]
            meta["status"] = "unread_provider_error"
    else:
        fields, meta = _fallback_draft()
    meta["editor_notes"] = apply_period_terms(fields)
    meta["flags"] = _collect_flags(fields, meta)
    return fields, meta


def _collect_flags(fields, meta):
    flags = list(meta.get("flags", []))
    for key in ("title", "servings", "time", "temp", "notes_verbatim"):
        if "[?]" in (fields.get(key) or ""):
            flags.append(f"{key} [?]")
    for i, line in enumerate(fields.get("ingredients", []), 1):
        if "[?]" in line:
            flags.append(f"ingredient {i} [?]")
    for i, line in enumerate(fields.get("steps", []), 1):
        if "[?]" in line:
            flags.append(f"step {i} [?]")
    return flags


def find_unresolved(fields):
    """Remaining [?] markers across confirmable fields — the confirm
    gate refuses to lock while any survive (brief §6)."""
    out = []
    for key in ("title", "servings", "time", "temp", "notes_verbatim"):
        if "[?]" in (fields.get(key) or ""):
            out.append(key)
    for i, line in enumerate(fields.get("ingredients", []), 1):
        if "[?]" in line:
            out.append(f"ingredient {i}")
    for i, line in enumerate(fields.get("steps", []), 1):
        if "[?]" in line:
            out.append(f"step {i}")
    return out


# ---------- auth ----------

def _presented_token():
    return (request.headers.get("X-Cookbook-Token")
            or request.args.get("token")
            or (request.form.get("token") if request.form else None) or "")


def _organizer_ok(book):
    return secrets.compare_digest(_hash(_presented_token()),
                                  book["organizer_token_hash"])


def _contributor_for(book):
    h = _hash(_presented_token())
    for c in book["contributors"]:
        if secrets.compare_digest(h, c["token_hash"]):
            return c
    return None


def _recipe_view(book, recipe):
    contributor = next((c for c in book["contributors"]
                        if c["id"] == recipe["contributor_id"]), None)
    base = _public_base_url() if _public_base_url else ""
    return {
        "id": recipe["id"], "book_id": book["id"],
        "contributor": contributor["name"] if contributor else "",
        "contributor_id": recipe["contributor_id"],
        "status": recipe["status"],
        "transcription_status": recipe["transcription_status"],
        "fields": recipe["fields"], "flags": recipe["flags"],
        "unread": recipe["unread"],
        "editor_notes": recipe["editor_notes"],
        "photo_url": f"{base}/api/cookbook/recipes/{recipe['id']}/photo",
        "submitted_at": recipe["submitted_at"],
        "confirmed_at": recipe.get("confirmed_at"),
    }


# ---------- PDF assembly ----------

def build_book_pdf(book, out_path):
    """Render the print-ready book: 8.5x11, photo left page / clean
    text right page, sections by contributor, front/back matter, notes
    pages to the 32-page perfect-bound floor. Returns (pages, recipes).
    Only CONFIRMED recipes are ever included (brief §5 hard gate)."""
    from reportlab.lib.utils import ImageReader, simpleSplit
    from reportlab.pdfgen import canvas

    confirmed = [r for r in book["recipes"] if r["status"] == "confirmed"]
    by_contributor = {}
    for c in book["contributors"]:
        mine = [r for r in confirmed if r["contributor_id"] == c["id"]]
        if mine:
            by_contributor[c["name"]] = mine

    c = canvas.Canvas(out_path, pagesize=(PAGE_W, PAGE_H))
    pages = 0

    def footer():
        if pages == 0:
            return
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.45, 0.4, 0.33)
        c.drawCentredString(PAGE_W / 2, 30,
                            "EverReady Family · Family Recipe Cookbook")
        c.drawRightString(PAGE_W - 54, 30, str(pages + 1))
        c.setFillColorRGB(0, 0, 0)

    def new_page():
        nonlocal pages
        footer()
        c.showPage()
        pages += 1

    def text_lines(lines, x, y, font="Times-Roman", size=12, leading=16,
                   width=PAGE_W - 108):
        for ln in lines:
            for part in simpleSplit(ln, font, size, width):
                if y < 72:
                    new_page()
                c.setFont(font, size)
                c.drawString(x, y, part)
                y -= leading
            y -= 2
        return y

    # Cover
    c.setFillColorRGB(0.55, 0.42, 0.16)
    c.rect(36, 36, PAGE_W - 72, PAGE_H - 72, stroke=1, fill=0)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Times-Bold", 30)
    c.drawCentredString(PAGE_W / 2, 500, f"The {book['family_name']}")
    c.setFont("Times-Bold", 30)
    c.drawCentredString(PAGE_W / 2, 462, "Family Recipe Cookbook")
    c.setFont("Times-Italic", 13)
    c.drawCentredString(PAGE_W / 2, 420,
                        "Remember, settle, and save what matters.")
    c.setFont("Times-Roman", 11)
    c.drawCentredString(PAGE_W / 2, 120,
                        time.strftime("%B %Y"))
    new_page()

    # How this book was made
    c.setFont("Times-Bold", 18)
    c.drawString(54, 720, "How this book was made")
    y = text_lines([
        "Every recipe in this book began as a photograph of a family "
        "recipe card — handwritten, often in cursive, sometimes stained "
        "or faded. Each card was transcribed into the clean text you "
        "see on the right-hand pages, and every reading the "
        "transcription could not make with certainty was flagged and "
        "confirmed by the family member who submitted the card. "
        "Nothing was guessed.",
        "",
        "The original card appears on the left page exactly as it was "
        "submitted. Her handwriting on the left; the recipe, readable, "
        "on the right.",
        "",
        "These recipes are reproduced as family history. For canning "
        "and preserving, follow current USDA guidance.",
    ], 54, 688)
    new_page()

    # Contributors
    c.setFont("Times-Bold", 18)
    c.drawString(54, 720, "From our kitchens")
    y = 688
    for name, mine in by_contributor.items():
        y = text_lines([f"{name} — {len(mine)} "
                        f"recipe{'s' if len(mine) != 1 else ''}"],
                       54, y, size=12)
    new_page()

    index = []
    for name, mine in by_contributor.items():
        c.setFont("Times-Bold", 22)
        c.drawCentredString(PAGE_W / 2, 470, name)
        c.setFont("Times-Italic", 12)
        c.drawCentredString(PAGE_W / 2, 444,
                            "recipes from this kitchen")
        new_page()
        for r in mine:
            f = r["fields"]
            index.append((f["title"], name))
            # LEFT page: the original card photo, exactly as submitted.
            photo = _photo_path(book["id"], r["id"])
            if os.path.isfile(photo):
                c.setStrokeColorRGB(0.55, 0.42, 0.16)
                c.rect(54, 54, PAGE_W - 108, PAGE_H - 108,
                       stroke=1, fill=0)
                c.setStrokeColorRGB(0, 0, 0)
                c.drawImage(ImageReader(photo), 63, 63, PAGE_W - 126,
                            PAGE_H - 126, preserveAspectRatio=True,
                            anchor="c", mask="auto")
            c.setFont("Times-Italic", 10)
            c.drawCentredString(PAGE_W / 2, 40,
                                f"Original card — submitted by {name}")
            new_page()
            # RIGHT page: the clean transcription.
            y = 720
            c.setFont("Times-Bold", 20)
            for part in simpleSplit(f["title"], "Times-Bold", 20,
                                    PAGE_W - 108):
                c.drawString(54, y, part)
                y -= 24
            c.setFont("Times-Italic", 11)
            c.drawString(54, y, f"From {name}'s kitchen")
            y -= 22
            meta_bits = [b for b in (f.get("servings"), f.get("time"),
                                    f.get("temp")) if b]
            if meta_bits:
                y = text_lines([" · ".join(meta_bits)], 54, y,
                               font="Times-Roman", size=11)
            y -= 6
            c.setFont("Times-Bold", 13)
            c.drawString(54, y, "Ingredients")
            y -= 18
            y = text_lines([f"•  {i}" for i in f["ingredients"]], 54, y)
            y -= 8
            c.setFont("Times-Bold", 13)
            c.drawString(54, y, "Steps")
            y -= 18
            y = text_lines([f"{n}.  {s}" for n, s in
                            enumerate(f["steps"], 1)], 54, y)
            if r.get("editor_notes"):
                y -= 6
                y = text_lines(["Editor's notes"] +
                               [f"— {n}" for n in r["editor_notes"]],
                               54, y, font="Times-Italic", size=10,
                               leading=13)
            if f.get("notes_verbatim"):
                y -= 6
                y = text_lines([f"On the card: “{f['notes_verbatim']}”"],
                               54, y, font="Times-Italic", size=10,
                               leading=13)
            new_page()

    # Index
    c.setFont("Times-Bold", 18)
    c.drawString(54, 720, "Recipe index")
    y = 688
    for title, name in sorted(index):
        y = text_lines([f"{title} — {name}"], 54, y, size=11, leading=14)
    new_page()

    # Notes pages to the perfect-bound floor.
    while True:
        c.setFont("Times-Bold", 16)
        c.drawString(54, 720, "Notes & New Favorites")
        c.setStrokeColorRGB(0.8, 0.76, 0.68)
        ly = 680
        while ly > 72:
            c.line(54, ly, PAGE_W - 54, ly)
            ly -= 28
        c.setStrokeColorRGB(0, 0, 0)
        if pages + 1 >= MIN_BOOK_PAGES:
            break
        new_page()

    footer()
    c.showPage()
    c.save()
    return pages + 1, len(confirmed)


# ---------- intake page (link-based, no accounts) ----------

INTAKE_HTML = """<!doctype html>
<title>Family Recipe Cookbook — submit your cards</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:Georgia,serif;background:#141210;color:#f3ede2;margin:2em auto;max-width:640px;padding:0 1em}
 h1{font-size:1.4em} .card{background:#221e19;border:1px solid #c9a24b55;border-radius:8px;padding:1em;margin:1em 0}
 input,textarea,button{font:inherit;width:100%;margin:.3em 0;padding:.5em;background:#1a1713;color:#f3ede2;border:1px solid #a89e8d;border-radius:4px}
 button{background:#c9a24b;color:#141210;font-weight:bold;cursor:pointer}
 .flag{color:#e8b34b} img{max-width:100%;border:1px solid #c9a24b}
</style>
<h1>Family Recipe Cookbook</h1>
<p><i>Remember, settle, and save what matters.</i> Photograph a recipe
card — flat, straight-on, good light, front <i>and</i> back, one card per
photo. We transcribe it; anything we can't read for certain comes back
flagged <span class="flag">[?]</span> for you to fix. We never guess.
Nothing locks until you confirm it.</p>
<div class="card">
 <label>Your contributor link token</label>
 <input id="tok" placeholder="paste the token from your invitation">
 <label>Photograph of the card</label>
 <input id="photo" type="file" accept="image/*">
 <button onclick="send()">Submit card</button>
 <div id="out"></div>
</div>
<script>
let B=new URLSearchParams(location.search).get('book')||'';
async function send(){
 const t=document.getElementById('tok').value.trim();
 const f=document.getElementById('photo').files[0];
 const out=document.getElementById('out');
 if(!t||!f){out.textContent='Add your token and a photo first.';return;}
 const fd=new FormData(); fd.append('photo',f); fd.append('token',t);
 const r=await fetch('/api/cookbook/books/'+B+'/photos',{method:'POST',body:fd});
 const j=await r.json();
 if(!r.ok){out.textContent=j.error||'Something went wrong.';return;}
 out.innerHTML='<img src="'+j.photo_url+'?token='+encodeURIComponent(t)+'">'
  +'<p>Draft below — fix anything flagged <span class="flag">[?]</span>, then confirm.</p>'
  +'<textarea id="d" rows="14">'+JSON.stringify(j.fields,null,1)+'</textarea>'
  +(j.unread.length?'<p class="flag">Please check: '+j.unread.join('; ')+'</p>':'')
  +'<button onclick="confirmR(\\''+j.id+'\\')">Confirm this recipe</button><div id="c"></div>';
 window._tok=t;
}
async function confirmR(id){
 const c=document.getElementById('c');
 let fields; try{fields=JSON.parse(document.getElementById('d').value);}
 catch(e){c.textContent='That draft text is not valid — check the formatting.';return;}
 const r=await fetch('/api/cookbook/recipes/'+id+'/confirm',{method:'POST',
  headers:{'Content-Type':'application/json','X-Cookbook-Token':window._tok},
  body:JSON.stringify(fields)});
 const j=await r.json();
 c.textContent=r.ok?'Confirmed and locked — thank you!':(j.error||'Not yet.');
}
</script>
"""


# ---------- routes ----------

def init(app, public_base_url=None):
    """Wire the cookbook lane into the store app. Routes are always
    registered (they are token-gated and inert without books); a
    failure here must never take the store down, so app.py wraps this
    call and the handlers fail closed with JSON errors."""
    global _public_base_url
    _public_base_url = public_base_url

    @app.get("/cookbook")
    def cookbook_intake_page():
        return Response(INTAKE_HTML, mimetype="text/html")

    @app.post("/api/cookbook/books")
    def cookbook_create_book():
        data = request.get_json(force=True, silent=True) or {}
        family = clean_text(data.get("family_name", ""))
        if not family:
            return jsonify({"error": "family_name is required"}), 400
        token = secrets.token_urlsafe(24)
        book = {"id": _new_id("bk"), "family_name": family,
                "organizer_name": clean_text(data.get("organizer_name", "")),
                "organizer_token_hash": _hash(token),
                "created_at": int(time.time()), "contributors": [],
                "recipes": []}
        with _lock:
            _save_book(book)
        log.info("cookbook: book %s created", book["id"])
        return jsonify({"book_id": book["id"], "family_name": family,
                        "organizer_token": token,
                        "intake_path": "/cookbook"}), 201

    @app.get("/api/cookbook/books/<book_id>")
    def cookbook_get_book(book_id):
        book = _load_book(book_id)
        if not book or not _organizer_ok(book):
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "book_id": book["id"], "family_name": book["family_name"],
            "contributors": [{"id": c["id"], "name": c["name"]}
                             for c in book["contributors"]],
            "recipes": [_recipe_view(book, r) for r in book["recipes"]],
            "confirmed": sum(1 for r in book["recipes"]
                             if r["status"] == "confirmed"),
        })

    @app.post("/api/cookbook/books/<book_id>/contributors")
    def cookbook_add_contributor(book_id):
        book = _load_book(book_id)
        if not book or not _organizer_ok(book):
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True, silent=True) or {}
        name = clean_text(data.get("name", ""))
        if not name:
            return jsonify({"error": "name is required"}), 400
        token = secrets.token_urlsafe(24)
        contributor = {"id": _new_id("ct"), "name": name,
                       "token_hash": _hash(token),
                       "created_at": int(time.time())}
        with _lock:
            book["contributors"].append(contributor)
            _save_book(book)
        return jsonify({"contributor_id": contributor["id"], "name": name,
                        "submit_token": token,
                        "submit_path": f"/cookbook?book={book_id}"}), 201

    @app.post("/api/cookbook/books/<book_id>/photos")
    def cookbook_submit_photo(book_id):
        book = _load_book(book_id)
        if not book:
            return jsonify({"error": "not found"}), 404
        contributor = _contributor_for(book)
        if not contributor:
            return jsonify({"error": "a valid contributor token is "
                                     "required"}), 401
        upload = request.files.get("photo")
        if not upload:
            return jsonify({"error": "photo file is required"}), 400
        raw = upload.read()
        if not raw or len(raw) > MAX_PHOTO_BYTES:
            return jsonify({"error": "photo is empty or over 15MB"}), 400
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img.load()
            if img.format not in ("JPEG", "PNG", "WEBP"):
                return jsonify({"error": "photo must be JPEG, PNG, or "
                                         "WebP"}), 400
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=88)
            jpeg = buf.getvalue()
        except Exception:
            return jsonify({"error": "that file is not a readable "
                                     "photo"}), 400
        recipe = {"id": _new_id("rc"),
                  "contributor_id": contributor["id"],
                  "status": "draft", "submitted_at": int(time.time()),
                  "confirmed_at": None}
        fields, meta = transcribe(jpeg, "image/jpeg")
        recipe.update({"fields": fields,
                       "transcription_status": meta["status"],
                       "flags": meta["flags"], "unread": meta["unread"],
                       "editor_notes": meta["editor_notes"]})
        with _lock:
            os.makedirs(os.path.dirname(
                _photo_path(book_id, recipe["id"])), exist_ok=True)
            with open(_photo_path(book_id, recipe["id"]), "wb") as f:
                f.write(jpeg)
            book["recipes"].append(recipe)
            _save_book(book)
        log.info("cookbook: recipe %s submitted to book %s (%s)",
                 recipe["id"], book_id, meta["status"])
        return jsonify(_recipe_view(book, recipe)), 201

    def _find_recipe(recipe_id):
        # Recipe ids are unique across books; scan the books dir. MVP
        # scale (one family book at a time) makes this cheap.
        books_dir = os.path.join(_dir(), "books")
        for fname in os.listdir(books_dir):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(books_dir, fname)) as f:
                book = json.load(f)
            for r in book["recipes"]:
                if r["id"] == recipe_id:
                    return book, r
        return None, None

    @app.get("/api/cookbook/recipes/<recipe_id>")
    def cookbook_get_recipe(recipe_id):
        book, recipe = _find_recipe(recipe_id)
        if not book:
            return jsonify({"error": "not found"}), 404
        if not (_organizer_ok(book) or _contributor_for(book)):
            return jsonify({"error": "not found"}), 404
        return jsonify(_recipe_view(book, recipe))

    @app.get("/api/cookbook/recipes/<recipe_id>/photo")
    def cookbook_recipe_photo(recipe_id):
        book, recipe = _find_recipe(recipe_id)
        if not book:
            return jsonify({"error": "not found"}), 404
        if not (_organizer_ok(book) or _contributor_for(book)):
            return jsonify({"error": "not found"}), 404
        path = _photo_path(book["id"], recipe_id)
        if not os.path.isfile(path):
            return jsonify({"error": "photo missing"}), 404
        return send_file(path, mimetype="image/jpeg")

    @app.post("/api/cookbook/recipes/<recipe_id>/confirm")
    def cookbook_confirm_recipe(recipe_id):
        book, recipe = _find_recipe(recipe_id)
        if not book:
            return jsonify({"error": "not found"}), 404
        # The submitter confirms their own card — never the organizer
        # on their behalf (brief §5: that is what keeps relatives
        # trusting the text).
        contributor = _contributor_for(book)
        if not contributor or contributor["id"] != recipe["contributor_id"]:
            return jsonify({"error": "only the contributor who "
                                     "submitted this card can confirm "
                                     "it"}), 403
        if recipe["status"] == "confirmed":
            return jsonify({"error": "this recipe is already confirmed "
                                     "and locked"}), 409
        data = request.get_json(force=True, silent=True) or {}
        fields = {
            "title": clean_text(data.get("title", "")),
            "ingredients": [clean_text(x) for x in
                            data.get("ingredients", []) if clean_text(x)],
            "steps": [clean_text(x) for x in
                      data.get("steps", []) if clean_text(x)],
            "servings": clean_text(data.get("servings", "")),
            "time": clean_text(data.get("time", "")),
            "temp": clean_text(data.get("temp", "")),
            "notes_verbatim": clean_text(data.get("notes_verbatim", "")),
        }
        if not fields["title"] or fields["title"].startswith("[Untitled"):
            return jsonify({"error": "give the recipe its title first",
                            "remaining_flags": ["title"]}), 422
        remaining = find_unresolved(fields)
        if remaining:
            return jsonify({
                "error": "a few readings still need your eyes — "
                         "nothing locks while a [?] remains",
                "remaining_flags": remaining}), 422
        notes = data.get("editor_notes", recipe["editor_notes"])
        with _lock:
            recipe["fields"] = fields
            recipe["editor_notes"] = [clean_text(n) for n in notes]
            recipe["status"] = "confirmed"
            recipe["confirmed_at"] = int(time.time())
            _save_book(book)
        log.info("cookbook: recipe %s confirmed in book %s",
                 recipe_id, book["id"])
        return jsonify(_recipe_view(book, recipe))

    @app.post("/api/cookbook/books/<book_id>/assemble")
    def cookbook_assemble(book_id):
        book = _load_book(book_id)
        if not book or not _organizer_ok(book):
            return jsonify({"error": "not found"}), 404
        if not any(r["status"] == "confirmed" for r in book["recipes"]):
            return jsonify({"error": "no confirmed recipes yet — a "
                                     "recipe enters the book only when "
                                     "its contributor confirms it"}), 400
        out = os.path.join(_dir(), "books", f"{book_id}.pdf")
        try:
            pages, count = build_book_pdf(book, out)
        except Exception as e:  # assembly failure must be loud, not 500-HTML
            log.exception("cookbook assembly failed for %s", book_id)
            return jsonify({"error": f"assembly failed: {e}"}), 500
        log.info("cookbook: book %s assembled (%d pages, %d recipes)",
                 book_id, pages, count)
        return jsonify({"book_id": book_id, "pages": pages,
                        "recipes": count,
                        "pdf_path": f"/api/cookbook/books/{book_id}/book.pdf"})

    @app.get("/api/cookbook/books/<book_id>/book.pdf")
    def cookbook_book_pdf(book_id):
        book = _load_book(book_id)
        if not book or not _organizer_ok(book):
            return jsonify({"error": "not found"}), 404
        path = os.path.join(_dir(), "books", f"{book_id}.pdf")
        if not os.path.isfile(path):
            return jsonify({"error": "book not assembled yet"}), 404
        return send_file(path, mimetype="application/pdf",
                         as_attachment=True,
                         download_name="family-recipe-cookbook.pdf")

    log.info("cookbook lane registered (data dir: %s)", _dir())
    return app
