"""Native lead capture (lead-magnet repair, 2026-10-08).

One small endpoint — POST /api/leads — backs the storefront captures
that cannot live in Kit:

* PushRod shop-notes signup (pushrodshop.com) — no Kit form exists for
  PushRod and the Kit API cannot create one, so capture is native here.
* SportRoots coach waitlist (sportrootsdrills.com/book) — previously a
  mailto: hand-off: no server record existed unless the visitor's own
  email client sent the note. Now recorded server-side.

Kit-form captures (EverReady checklist, SportRoots 5-free-drills, the
beta forms) keep posting to Kit from the browser exactly like the Kit
embeds always did; only the post-signup delivery moved onto our pages.

Persistence: SQLite at LEADS_DB (defaults to /var/data/leads.db when the
Render persistent disk is mounted, else <repo>/data/leads.db locally),
mirroring the wholesale module's data-root pattern.
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

bp = Blueprint("leads", __name__)

DB_PATH = None
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
_MAX_FIELDS = 24
_MAX_VALUE = 2000


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            brand TEXT NOT NULL,
            kind TEXT NOT NULL,
            email TEXT NOT NULL,
            name TEXT,
            fields TEXT,
            ip TEXT
        )
        """
    )
    conn.commit()
    conn.close()


@bp.post("/api/leads")
def create_lead():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict()
    brand = str(data.get("brand") or "").strip()[:40]
    kind = str(data.get("kind") or "").strip()[:40]
    email = str(data.get("email") or "").strip()[:254]
    name = str(data.get("name") or "").strip()[:120] or None
    if not brand or not kind:
        return jsonify({"ok": False, "error": "brand and kind are required"}), 400
    if not _EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "a valid email is required"}), 400
    raw_fields = data.get("fields") or {}
    if isinstance(raw_fields, str):
        try:
            raw_fields = json.loads(raw_fields)
        except ValueError:
            raw_fields = {"note": raw_fields[:_MAX_VALUE]}
    fields = {}
    if isinstance(raw_fields, dict):
        for key, value in list(raw_fields.items())[:_MAX_FIELDS]:
            fields[str(key)[:60]] = str(value)[:_MAX_VALUE]
    conn = _db()
    cur = conn.execute(
        "INSERT INTO leads (created_at, brand, kind, email, name, fields, ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            brand,
            kind,
            email,
            name,
            json.dumps(fields) if fields else None,
            (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
        ),
    )
    conn.commit()
    lead_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": lead_id})


def init(app, root_dir):
    """Wire the leads blueprint into the Flask app. Called once from app.py."""
    global DB_PATH
    data_root = "/var/data" if os.path.isdir("/var/data") else os.path.join(root_dir, "data")
    DB_PATH = os.environ.get("LEADS_DB", os.path.join(data_root, "leads.db"))
    init_db()
    app.register_blueprint(bp)
