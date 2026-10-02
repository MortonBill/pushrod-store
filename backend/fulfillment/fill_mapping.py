#!/usr/bin/env python3
"""Fill printful_mapping.json against the live Printful API.

Reads backend/fulfillment/printful_mapping.json (built by build_mapping.py) and,
for every entry whose catalog_variant_id / print_file_url is still null:

  1. Resolves the blank product via GET /v2/catalog-products (+ variants).
  2. Pushes each local print PNG to the public GitHub repo
     MortonBill/pushrod-print-files (created if missing) so Printful can fetch it.
  3. Registers the public URL via POST /v2/files (File Library API).
  4. Writes the resolved catalog_variant_id + print_file_url into the mapping.

DRY RUN ONLY: this script never creates Printful orders. It only reads the
catalog, registers files, and writes the local mapping JSON.

Requires a working Printful token in the custom.printful connector. As of
2026-09-30 the stored credential returned 401 'access token invalid'; run the
connector reconnect flow (credentials.request_api_access) before using this.

Usage:
  python3 backend/fulfillment/fill_mapping.py [--dry-run] [--limit N]
                                             [--mapping PATH]
"""
import argparse, json, os, sys, time, base64, urllib.request, urllib.error

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MAPPING = os.path.join(REPO, "backend", "fulfillment", "printful_mapping.json")
PRINT_FILES = os.path.expanduser("~/workspace/pushrod-print-files")
PF_REPO_OWNER, PF_REPO_NAME, PF_BRANCH = "MortonBill", "pushrod-print-files", "main"
PF_API = "https://api.printful.com"
GH_API = "https://api.github.com"
UA = {"User-Agent": "PushRodStore/1.0 (printful-mapping-fill)"}


def pf(method, path, payload=None, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(PF_API + path, data=data, method=method,
                                     headers={"Accept": "application/json", **UA})
        if data:
            req.add_header("Content-Type", "application/json")
        add_surrogate_to_request(req, "custom.printful", allowed_hosts=("api.printful.com",))
        try:
            return read_json_response(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"Printful {method} {path} -> HTTP {e.code}: {body}")


def gh(method, path, payload=None, retries=4):
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(GH_API + path, data=data, method=method,
                                     headers={"Accept": "application/vnd.github+json", **UA})
        if data:
            req.add_header("Content-Type", "application/json")
        add_surrogate_to_request(req, "custom.github", allowed_hosts=("api.github.com",))
        try:
            return read_json_response(urllib.request.urlopen(req, timeout=120))
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            if e.code in (502, 503, 504) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"GitHub {method} {path} -> HTTP {e.code}: {body}")


# ---------------------------------------------------------------- products
PRODUCT_MATCH = {  # blank_product -> distinctive substrings to find in catalog names
    "Comfort Colors 1717": ["1717"],
    "Gildan 18000 Heavy Blend Crewneck": ["18000"],
    "Yupoong 6606 Retro Trucker": ["6606"],
    "11oz Black Mug": ["11oz black mug"],
    "Kiss-Cut Stickers": ["kiss-cut"],
    "Embroidered Patches": ["embroidered patch"],
}


def resolve_product(blank_name):
    # Printful v2 list endpoints paginate with limit+offset and IGNORE
    # `page` (verified 2026-10-02: page=1 and page=2 return identical
    # data). The old page-based loop re-scanned page 1 forever and never
    # saw products past the first 100 — the root cause of the failed
    # 2026-10-01 fill runs ("no variant matched" / hangs).
    needles = [n.lower() for n in PRODUCT_MATCH[blank_name]]
    seen, offset = [], 0
    while True:
        resp = pf("GET", f"/v2/catalog-products?limit=100&offset={offset}")
        items = resp.get("data", [])
        if not items:
            break
        for p in items:
            name = (p.get("name") or p.get("title") or "").lower()
            if all(n in name for n in needles):
                return p
        seen.extend(items)
        if len(items) < 100:
            break
        offset += 100
        time.sleep(0.6)
    names = [p.get("name") for p in seen[:8]]
    raise RuntimeError(f"blank product not found: {blank_name} (sample catalog names: {names})")


def get_variants(product_id):
    # Offset pagination (see resolve_product note): `page` is ignored by
    # the v2 API, so the old loop never terminated on >100-variant
    # products and never saw variants past the first 100 (e.g. Maroon
    # lives at offset>=100 on the Gildan 18000).
    variants, offset = [], 0
    while True:
        resp = pf("GET", f"/v2/catalog-products/{product_id}/catalog-variants?limit=100&offset={offset}")
        items = resp.get("data", [])
        if not items:
            break
        variants.extend(items)
        if len(items) < 100:
            break
        offset += 100
        time.sleep(0.6)
    return variants


def pick_variant(variants, fill, key):
    """Pick the variant matching color_candidates (+ size for apparel)."""
    cands = [c.lower() for c in fill.get("color_candidates", [])]
    size = fill.get("size")
    shape = fill.get("patch_shape")

    def color_ok(v):
        if not cands:
            return True
        vc = (v.get("color") or "").lower()
        return any(c == vc or c in vc for c in cands)

    scored = []
    for v in variants:
        if not color_ok(v):
            continue
        score = 0
        if size and (v.get("size") or "").upper() == size.upper():
            score += 10
        if shape and shape.lower().replace('"', "") in (v.get("name") or "").lower():
            score += 10
        if not size and not shape:
            score += 1
        if score:
            # prefer the first-listed color candidate
            vc = (v.get("color") or "").lower()
            rank = next((i for i, c in enumerate(cands) if c == vc or c in vc), 99)
            scored.append((score, rank, v))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2]


# ---------------------------------------------------------------- files
def ensure_repo():
    try:
        gh("GET", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}")
        print(f"repo exists: {PF_REPO_OWNER}/{PF_REPO_NAME}")
        return
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    gh("POST", "/user/repos", {
        "name": PF_REPO_NAME,
        "description": "Public print-ready art for the PushRod store (Printful fulfillment).",
        "private": False,
        "auto_init": True,
    })
    print(f"created repo {PF_REPO_OWNER}/{PF_REPO_NAME}")


def push_print_files(dry_run=False):
    """Push ~/workspace/pushrod-print-files to the public repo via git-database API."""
    entries = []
    for root, _dirs, files in os.walk(PRINT_FILES):
        for fn in sorted(files):
            if not fn.endswith(".png"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, PRINT_FILES)
            entries.append((rel, full))
    entries.append(("README.md", None))
    print(f"{len(entries) - 1} PNGs to host")
    if dry_run:
        return {rel: public_url(rel) for rel, _ in entries if rel != "README.md"}

    try:
        ref = gh("GET", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/refs/heads/{PF_BRANCH}")
        parent = ref["object"]["sha"]
        empty_repo = False
    except RuntimeError as e:
        # empty repo -> 409 "Git Repository is empty." ; missing branch -> 404
        if "404" not in str(e) and "empty" not in str(e):
            raise
        parent = None
        empty_repo = "empty" in str(e)

    if empty_repo:
        # the git-database blob API 409s on a repo with zero commits; seed one
        seed = base64.b64encode(
            b"# pushrod-print-files\nPublic print-ready PNGs for PushRod store fulfillment.\n"
        ).decode()
        gh("PUT", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/contents/README.md",
           {"message": "initial commit", "content": seed, "branch": PF_BRANCH})
        ref = gh("GET", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/refs/heads/{PF_BRANCH}")
        parent = ref["object"]["sha"]
        print("seeded initial commit")

    blobs = []
    for i, (rel, full) in enumerate(entries):
        if full is None:
            content = base64.b64encode(
                b"# pushrod-print-files\nPublic print-ready PNGs for PushRod store fulfillment.\n"
            ).decode()
        else:
            with open(full, "rb") as f:
                content = base64.b64encode(f.read()).decode()
        blob = gh("POST", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/blobs",
                  {"content": content, "encoding": "base64"})
        blobs.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if (i + 1) % 50 == 0:
            print(f"  blobs {i + 1}/{len(entries)}")
    tree = gh("POST", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/trees",
              {"tree": blobs, "base_tree": None})
    commit_payload = {"message": "print-ready art for Printful fulfillment",
                      "tree": tree["sha"]}
    if parent:
        commit_payload["parents"] = [parent]
    commit = gh("POST", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/commits", commit_payload)
    if parent:
        gh("PATCH", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/refs/heads/{PF_BRANCH}",
           {"sha": commit["sha"], "force": True})
    else:
        gh("POST", f"/repos/{PF_REPO_OWNER}/{PF_REPO_NAME}/git/refs",
           {"ref": f"refs/heads/{PF_BRANCH}", "sha": commit["sha"]})
    print("pushed:", f"https://github.com/{PF_REPO_OWNER}/{PF_REPO_NAME}/commit/{commit['sha']}")
    return {rel: public_url(rel) for rel, _ in entries if rel != "README.md"}


def public_url(rel):
    return f"https://raw.githubusercontent.com/{PF_REPO_OWNER}/{PF_REPO_NAME}/{PF_BRANCH}/{rel}"


def register_file(url, dry_run=False):
    if dry_run:
        return {"id": "dry-run", "url": url}
    resp = pf("POST", "/v2/files", {"url": url})
    return resp.get("data", {})


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--mapping", default=DEFAULT_MAPPING)
    args = ap.parse_args()

    with open(args.mapping) as f:
        data = json.load(f)
    meta = data.get("_meta", {})
    mapping = data.get("mappings", {})

    # sanity: credential check first
    try:
        me = pf("GET", "/v2/catalog-products?limit=1")
        assert me.get("data") is not None
        print("Printful auth OK")
    except Exception as e:
        print(f"PRINTFUL AUTH FAILED: {e}")
        print("Run the custom.printful connector reconnect flow, then retry.")
        sys.exit(2)

    keys = [k for k in mapping if not k.startswith("_")]
    todo = [k for k in keys
            if not mapping[k].get("catalog_variant_id") or not mapping[k].get("print_file_url")]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} of {len(keys)} keys need filling")

    # 1. host the art
    needed_files = {}
    for k in todo:
        local = mapping[k]["_fill"]["print_file_local"]
        rel = os.path.relpath(os.path.expanduser(local), PRINT_FILES)
        needed_files[rel] = os.path.expanduser(local)
    missing = [r for r, full in needed_files.items() if not os.path.exists(full)]
    if missing:
        print(f"WARNING: {len(missing)} local print files missing; e.g. {missing[:3]}")

    if not args.dry_run:
        ensure_repo()
    url_by_rel = push_print_files(dry_run=args.dry_run)

    # 2. register files with Printful (one per unique design)
    file_url_by_rel = {}
    for i, rel in enumerate(sorted(needed_files)):
        if rel not in url_by_rel:
            continue
        reg = register_file(url_by_rel[rel], dry_run=args.dry_run)
        file_url_by_rel[rel] = reg.get("url") or url_by_rel[rel]
        mapping_entry_note = reg.get("id")
        if (i + 1) % 25 == 0:
            print(f"  files {i + 1}/{len(needed_files)}")
        time.sleep(0.6)  # stay under the 120/min leaky bucket

    # 3. resolve products + variants
    product_cache, variant_cache = {}, {}
    filled, failed = 0, []
    for k in todo:
        e = mapping[k]
        fill = e["_fill"]
        try:
            blank = fill["blank_product"]
            if blank not in product_cache:
                p = resolve_product(blank)
                product_cache[blank] = p
                variant_cache[blank] = get_variants(p["id"])
                print(f"blank '{blank}' -> catalog product {p['id']} "
                      f"({p.get('name')}), {len(variant_cache[blank])} variants")
                # placement / technique sanity check
                placements = [pl.get("placement") or pl.get("code") or pl
                              for pl in p.get("placements", [])]
                techniques = [t.get("technique") or t.get("code") or t
                              for t in p.get("techniques", [])]
                if e["placement"] not in placements and placements:
                    print(f"  WARNING placement '{e['placement']}' not in {placements}")
                if e["technique"] and e["technique"] not in techniques and techniques:
                    print(f"  WARNING technique '{e['technique']}' not in {techniques}")
            v = pick_variant(variant_cache[blank], fill, k)
            if not v:
                failed.append((k, "no variant matched color/size"))
                continue
            rel = os.path.relpath(os.path.expanduser(fill["print_file_local"]), PRINT_FILES)
            e["catalog_variant_id"] = v["id"]
            e["print_file_url"] = file_url_by_rel.get(rel)
            if not e["print_file_url"]:
                failed.append((k, "no public file URL"))
                continue
            if fill.get("color_review"):
                e["_fill"]["color_review_result"] = (
                    f"fill chose variant color '{v.get('color')}' "
                    f"(id {v['id']}); manual review recommended"
                )
            filled += 1
        except Exception as ex:  # noqa: BLE001 - keep going, report at end
            failed.append((k, str(ex)[:160]))
        time.sleep(0.3)

    print(f"filled={filled} failed={len(failed)}")
    for k, why in failed[:20]:
        print("  FAILED", k, "-", why)

    if args.dry_run:
        print("dry run: mapping NOT written")
        return

    mapping["_meta"] = meta
    meta["filled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["filled_keys"] = sum(1 for k in mapping
                              if mapping[k].get("catalog_variant_id")
                              and mapping[k].get("print_file_url"))
    data["mappings"] = mapping
    data["_meta"] = meta
    backup = args.mapping + ".pre-fill.bak"
    with open(backup, "w") as f:
        json.dump({"mappings": mapping}, f, indent=1)
    with open(args.mapping, "w") as f:
        json.dump(data, f, indent=1)
    print(f"wrote {args.mapping} (backup: {backup})")


if __name__ == "__main__":
    main()
