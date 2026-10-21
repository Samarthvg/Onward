"""Undo for changes made from the dashboard.

Each change the helper makes on the user's behalf (a settings save, a new
master resume, an apply queue choice, an Applied mark) first records what it
replaces, and the answer carries the record's id. POST /api/undo with that id
puts the old state back. Records live in jobs.db so an undo still works
after the page reloads; they are pruned by age and count, so the table
stays small however long the app runs.

What a record holds depends on its kind (server.py applies them):
  settings  {"before": {section: value}}: the sections the save replaced
  files     {"files": {path: base64 or None}, "settings": {...}}: file
            contents before the change (None: the file didn't exist)
  prep      {"rows": {job_key: row or None}, "applications": [job_key]}
  tailored  {"id": tailored_id}: a new resume version to take back out
  key       {"provider": name}: the replaced API key itself is held only in
            this process's memory (SECRETS), never written to disk
"""

from __future__ import annotations

import json
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS undo (
    id       TEXT PRIMARY KEY,
    created  REAL NOT NULL,
    kind     TEXT NOT NULL,
    label    TEXT NOT NULL,
    payload  TEXT NOT NULL,
    used     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS undo_created ON undo(created);
"""

KEEP_SECONDS = 7 * 86400
KEEP_ROWS = 300

# Replaced API keys, by undo id. Memory only: a restart forgets them, and
# the undo then says to paste the key again.
SECRETS: dict[str, str | None] = {}
_SECRETS_LOCK = threading.Lock()
SECRETS_MAX = 20


class Gone(Exception):
    """The record is unknown, already used, or too old to apply."""


def record(conn, kind: str, label: str, payload: dict) -> str:
    """Store what a change replaces; returns the id the dashboard undoes it by."""
    uid = uuid.uuid4().hex[:16]
    now = time.time()
    with conn:
        conn.execute("INSERT INTO undo (id, created, kind, label, payload) VALUES (?, ?, ?, ?, ?)",
                     (uid, now, kind, label[:200], json.dumps(payload)))
        conn.execute("DELETE FROM undo WHERE created < ?", (now - KEEP_SECONDS,))
        conn.execute("DELETE FROM undo WHERE id NOT IN (SELECT id FROM undo ORDER BY created DESC LIMIT ?)", (KEEP_ROWS,))
    return uid


def take(conn, uid: str) -> tuple[str, str, dict]:
    """(kind, label, payload) of an unused record, now marked used, so the
    same change can't be undone twice."""
    with conn:
        row = conn.execute("SELECT kind, label, payload, used, created FROM undo WHERE id = ?", (uid,)).fetchone()
        if not row or row[3] or row[4] < time.time() - KEEP_SECONDS:
            raise Gone(uid)
        conn.execute("UPDATE undo SET used = 1 WHERE id = ?", (uid,))
    return row[0], row[1], json.loads(row[2])


def release(conn, uid: str) -> None:
    """Make a record usable again after applying it failed."""
    with conn:
        conn.execute("UPDATE undo SET used = 0 WHERE id = ?", (uid,))


def keep_secret(uid: str, value: str | None) -> None:
    with _SECRETS_LOCK:
        SECRETS[uid] = value
        while len(SECRETS) > SECRETS_MAX:
            SECRETS.pop(next(iter(SECRETS)))


def pop_secret(uid: str) -> tuple[bool, str | None]:
    """(known, value): unknown when the helper restarted since the change."""
    with _SECRETS_LOCK:
        if uid not in SECRETS:
            return False, None
        return True, SECRETS.pop(uid)
