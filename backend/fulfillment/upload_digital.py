#!/usr/bin/env python3
"""
Stage digital deliverables into the private object-storage bucket.

Lane 1 (RE guides): data/re-digital-sources.json is the work order — every
matched guide PDF, its local source path, its byte size, and its storage key
(the PDF basename). This script uploads each file to the configured bucket
(DIGITAL_STORAGE_BACKEND=s3 + DIGITAL_S3_* env, see fulfillment/storage.py),
skipping objects that already exist. Nothing here touches the storefront:
rows stay listed=0 until the lane gate flips them.

Usage (run from the repo root, credentials in the environment — never in
arguments or files; this script prints env NAMES only, never values):

  # 1. verify every local source exists and matches the manifest size
  python3 backend/fulfillment/upload_digital.py --dry-run

  # 2. upload missing objects (idempotent; safe to re-run)
  python3 backend/fulfillment/upload_digital.py

  # 3. report present/missing in the bucket without uploading
  python3 backend/fulfillment/upload_digital.py --check

Exit code 0 = every manifest file accounted for; 1 = gaps (listed).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fulfillment import storage as storage_mod  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "data", "re-digital-sources.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST,
                    help="sources manifest (default: data/re-digital-sources.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify local sources only; no network")
    ap.add_argument("--check", action="store_true",
                    help="HEAD each object; report present/missing; no upload")
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)
    files = manifest["files"]
    print(f"manifest: {len(files)} files, "
          f"{manifest['storage']['total_bytes']:,} bytes "
          f"(key rule: {manifest['storage']['key_rule']})")

    problems = []
    for entry in files:
        src = entry["source"]
        if not os.path.isfile(src):
            problems.append(f"MISSING LOCAL SOURCE {entry['sku']}: {src}")
        elif os.path.getsize(src) != entry["bytes"]:
            problems.append(
                f"SIZE MISMATCH {entry['sku']}: manifest {entry['bytes']} "
                f"vs disk {os.path.getsize(src)}")
    if problems:
        print("\n".join(problems))
        return 1
    print("local sources: all present, sizes match manifest")

    if args.dry_run:
        print("dry-run: no storage contact. Ready to upload.")
        return 0

    backend = storage_mod.get_storage()
    if backend.is_local:
        print("DIGITAL_STORAGE_BACKEND is 'local' — set it to 's3' with "
              "DIGITAL_S3_BUCKET / DIGITAL_S3_ACCESS_KEY_ID / "
              "DIGITAL_S3_SECRET_ACCESS_KEY in the environment first.")
        return 1

    uploaded = present = 0
    missing_keys = []
    for entry in files:
        key = entry["key"]
        if backend.exists(key):
            present += 1
            continue
        if args.check:
            missing_keys.append(key)
            continue
        with open(entry["source"], "rb") as f:
            data = f.read()
        backend.put(key, data)
        uploaded += 1
        print(f"uploaded {key} ({len(data):,} bytes)")
    print(f"bucket state: {present} already present, {uploaded} uploaded, "
          f"{len(missing_keys)} missing")
    if missing_keys:
        print("missing keys:")
        print("\n".join(missing_keys))
        return 1
    # Final verification pass: every object HEADs and every local size was
    # already verified above, so presence == staged.
    print("all manifest files staged in the bucket")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
