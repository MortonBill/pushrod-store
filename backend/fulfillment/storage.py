"""
Byte storage for digital delivery (Lane 1 storage decision, 2026-10-02).

SkillForge ships its PDFs inside the deploy (data/digital/, ~56 MB). The RE
guide catalog is 383 PDFs / ~587 MB — that must NOT go into git (checkout
migration plan §4b). This module is the seam behind the existing token-gated
/download/<token> flow: the endpoint verifies the signed token exactly as
before, then fetches the deliverable's bytes from a storage backend.

Backends (DIGITAL_STORAGE_BACKEND env):
  local  (default) — files under DIGITAL_FILES_DIR. Byte-for-byte the
         pre-storage behavior: the app serves with send_from_directory.
  s3     — any S3-compatible private bucket (AWS S3, Cloudflare R2,
         Backblaze B2, MinIO). Requests are SigV4-signed with stdlib only
         (no boto3) so requirements.txt stays untouched. The bucket stays
         private: the service fetches with its own credentials and streams
         the bytes through the token check; buyers never see a bucket URL.

Env (names only — values live in the service environment, never in git):
  DIGITAL_STORAGE_BACKEND   local|s3            (default local)
  DIGITAL_S3_BUCKET         bucket name         (required for s3)
  DIGITAL_S3_REGION         default us-east-1   ("auto" for R2)
  DIGITAL_S3_ENDPOINT       optional base URL for R2/B2/MinIO path-style
                            access (e.g. https://<acct>.r2.cloudflarestorage.com);
                            blank = AWS S3 virtual-host style
  DIGITAL_S3_ACCESS_KEY_ID  required for s3
  DIGITAL_S3_SECRET_ACCESS_KEY  required for s3
  DIGITAL_S3_PREFIX         optional key prefix (default "")

Keys are always the deliverable's basename (the catalog's digital_file,
already basename-normalized by backend/catalog.py), plus the optional
prefix. A storage key can therefore never escape the configured bucket
prefix, mirroring the local path-containment guard.

Misconfiguration is LOUD (StorageConfigError naming the missing env vars),
never a silent fall back to "no file": a paid download that 404s quietly
is exactly the failure this seam exists to prevent.
"""
import hashlib
import hmac
import logging
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("pushrod.storage")

CHUNK = 64 * 1024


class StorageError(RuntimeError):
    """A storage fetch failed (backend error, network, HTTP status)."""


class StorageNotFound(StorageError):
    """The requested object does not exist in the backend."""


class StorageConfigError(StorageError):
    """Storage is misconfigured (missing env, unknown backend)."""


def _basename(name):
    """Storage keys are basenames only — catalog values can never traverse."""
    return os.path.basename((name or "").strip())


def _content_type(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# ---------- local filesystem backend ----------

class LocalStorage:
    """Default backend: the repo-local digital-files directory.

    The app serves these with send_from_directory exactly as before this
    module existed; open()/exists() exist for tests and tooling.
    """

    is_local = True

    def __init__(self, files_dir):
        self.files_dir = os.path.realpath(files_dir or "")

    def _path(self, name):
        filename = _basename(name)
        path = os.path.realpath(os.path.join(self.files_dir, filename))
        if os.path.dirname(path) != self.files_dir:
            raise StorageNotFound(f"no such file: {filename}")
        return path

    def exists(self, name):
        try:
            return os.path.isfile(self._path(name))
        except StorageNotFound:
            return False

    def open(self, name):
        """Returns (chunk_iterator, size, content_type)."""
        path = self._path(name)
        if not os.path.isfile(path):
            raise StorageNotFound(f"no such file: {_basename(name)}")

        def chunks():
            with open(path, "rb") as f:
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    yield block

        return chunks(), os.path.getsize(path), _content_type(path)


# ---------- S3-compatible backend ----------

def _signing_key(secret, date_stamp, region):
    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret).encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "s3")
    return _hmac(k_service, "aws4_request")


class S3Storage:
    """Private S3-compatible object storage, SigV4-signed with stdlib."""

    is_local = False

    def __init__(self, bucket, access_key, secret_key, region="us-east-1",
                 endpoint="", prefix=""):
        self.bucket = bucket
        self.access_key = access_key
        self._secret_key = secret_key
        self.region = region or "us-east-1"
        self.endpoint = (endpoint or "").rstrip("/")
        self.prefix = (prefix or "").strip("/")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        missing = [v for v in ("DIGITAL_S3_BUCKET", "DIGITAL_S3_ACCESS_KEY_ID",
                               "DIGITAL_S3_SECRET_ACCESS_KEY")
                   if not (env.get(v) or "").strip()]
        if missing:
            raise StorageConfigError(
                "DIGITAL_STORAGE_BACKEND=s3 but required env var(s) not set: "
                + ", ".join(missing)
                + ". Set them in the service environment (Render -> service "
                  "-> Environment); values never go in git. The bucket must "
                  "stay private — the service streams bytes through the "
                  "signed-token check.")
        return cls(
            bucket=env["DIGITAL_S3_BUCKET"].strip(),
            access_key=env["DIGITAL_S3_ACCESS_KEY_ID"].strip(),
            secret_key=env["DIGITAL_S3_SECRET_ACCESS_KEY"].strip(),
            region=(env.get("DIGITAL_S3_REGION") or "us-east-1").strip(),
            endpoint=(env.get("DIGITAL_S3_ENDPOINT") or "").strip(),
            prefix=(env.get("DIGITAL_S3_PREFIX") or "").strip(),
        )

    def _key(self, name):
        base = _basename(name)
        return f"{self.prefix}/{base}" if self.prefix else base

    def _url(self, key):
        quoted = urllib.parse.quote(key, safe="/-._~")
        if self.endpoint:
            return f"{self.endpoint}/{self.bucket}/{quoted}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{quoted}"

    def _signed_request(self, method, key, body=b""):
        url = self._url(key)
        parsed = urllib.parse.urlparse(url)
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "host": parsed.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_headers = "".join(
            f"{h}:{headers[h]}\n" for h in signed_headers.split(";"))
        canonical_request = "\n".join([
            method, parsed.path, "", canonical_headers, signed_headers,
            payload_hash])
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest()])
        signature = hmac.new(
            _signing_key(self._secret_key, date_stamp, self.region),
            string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
        if body:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body or None, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        return req

    def _send(self, method, key, body=b""):
        req = self._signed_request(method, key, body)
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise StorageNotFound(f"no such object: {key}") from e
            detail = e.read().decode(errors="replace")[:200] if e.fp else ""
            raise StorageError(
                f"storage {method} {key} failed: HTTP {e.code} {detail}"
            ) from e
        except Exception as e:  # noqa: BLE001 — network errors surface loud
            raise StorageError(f"storage {method} {key} failed: {e}") from e

    def exists(self, name):
        try:
            self._send("HEAD", self._key(name)).close()
            return True
        except StorageNotFound:
            return False

    def open(self, name):
        """Returns (chunk_iterator, size_or_None, content_type)."""
        key = self._key(name)
        resp = self._send("GET", key)

        def chunks():
            try:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    yield block
            finally:
                resp.close()

        size = resp.headers.get("Content-Length")
        content_type = (resp.headers.get("Content-Type")
                        or _content_type(key))
        return chunks(), (int(size) if size else None), content_type

    def put(self, name, data, content_type=None):
        """Upload bytes (staging tooling; the storefront never writes)."""
        key = self._key(name)
        req = self._signed_request(
            "PUT", key, data)
        req.add_header("Content-Type", content_type or _content_type(key))
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.headers.get("ETag", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200] if e.fp else ""
            raise StorageError(
                f"storage PUT {key} failed: HTTP {e.code} {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"storage PUT {key} failed: {e}") from e


# ---------- factory ----------

def get_storage(files_dir=None, env=None):
    """Resolve the configured backend. Reads env at call time (no caching)
    so tests and per-service env stay honest. `files_dir` is the app's
    resolved DIGITAL_FILES_DIR for the local backend."""
    env = os.environ if env is None else env
    backend = (env.get("DIGITAL_STORAGE_BACKEND") or "local").strip().lower()
    if backend in ("local", "filesystem", "file"):
        return LocalStorage(
            files_dir or env.get("DIGITAL_FILES_DIR", ""))
    if backend == "s3":
        return S3Storage.from_env(env)
    raise StorageConfigError(
        f"unknown DIGITAL_STORAGE_BACKEND {backend!r} — expected 'local' "
        "or 's3'.")
