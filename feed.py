"""The shared job feed: one search, published as static files, read by every
copy of Onward instead of each one searching every board itself.

The search server runs radar.py as usual and then publish(), which writes a
folder that any static host or CDN can serve:

  index.json                 what's current: the latest version, when the
                             search last ran, the snapshot and change files
  snapshots/<version>.json.gz  every open posting
  changes/<version>.json.gz    from version-1 to version: postings added or
                             changed, and postings that closed

Clients pull(): a new or far-behind copy reads the snapshot, an up-to-date
one only the change files since its version. Each file's SHA-256 is in the
index and checked before anything is applied, in one transaction, so a
half-downloaded or tampered file changes nothing.

A posting is the same record radar.store() keeps (FIELDS), minus what each
copy works out for its own user (role tags, the keyword checklist), so the
feed never carries anything about who reads it. Postings stay "open" on a
client while the feed lists them: their last_seen follows the search's last
run. A feed that stops updating makes pull() report it stale, and radar.py
then searches by itself as before.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

FORMAT = 1
KEEP_CHANGES = 96               # two days of runs every 30 minutes; older clients read the snapshot
KEEP_SNAPSHOTS = 2              # the previous one stays while clients may still be downloading it
WINDOW_DAYS = 60                # the longest a client shows a posting (radar.program_days default)
STALE_AFTER = 3 * 3600          # a feed whose search hasn't run for this long isn't trusted
MAX_FILE = 300 * 1024 * 1024    # bytes, compressed

# Posting fields in the feed, with their types. Text is capped at the given
# length when read, so a bad feed can't fill a client's database.
FIELDS: dict[str, tuple[type, int]] = {
    "key": (str, 600), "company": (str, 300), "title": (str, 500), "location": (str, 500),
    "remote": (int, 0), "url": (str, 2000), "source": (str, 40), "posted_at": (float, 0),
    "posted_exact": (int, 0), "sponsorship": (str, 20), "sponsor_note": (str, 2000), "min_yoe": (int, 0),
    "entry_hint": (int, 0), "first_seen": (float, 0), "last_seen": (float, 0), "apply_url": (str, 2000),
    "verify_url": (str, 2000), "posted_field": (str, 100), "posted_raw": (str, 300), "skills": (str, 20000),
    "description": (str, 200000), "no_opt": (int, 0), "country": (str, 60), "pay_min": (float, 0),
    "pay_max": (float, 0), "first_posted": (float, 0), "reposts": (int, 0),
}
REQUIRED = ("key", "company", "title", "location", "url", "source", "first_seen", "last_seen")
# Fields that change every run without the posting changing.
VOLATILE = ("last_seen",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_published (     -- on the search server: what the feed lists
    key     TEXT PRIMARY KEY,
    digest  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_members (       -- on a client: postings the feed lists now
    key     TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS feed_state (         -- one row on either side
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    url      TEXT NOT NULL DEFAULT '',
    version  INTEGER NOT NULL DEFAULT 0,
    checked  REAL NOT NULL DEFAULT 0
);
"""


class FeedError(Exception):
    """The feed couldn't be read or doesn't check out; nothing was changed."""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO feed_state (id) VALUES (1)")


def _state(conn: sqlite3.Connection) -> tuple[str, int, float]:
    return conn.execute("SELECT url, version, checked FROM feed_state WHERE id = 1").fetchone()


def digest(rec: dict) -> str:
    stable = {k: v for k, v in rec.items() if k not in VOLATILE}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------
# Publishing (the search server)

def _open_postings(conn: sqlite3.Connection, now: float, stale_days: float, window_days: float) -> dict[str, dict]:
    cols = list(FIELDS)
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM jobs WHERE last_seen >= ? AND COALESCE(posted_at, first_seen) >= ?",
        (now - stale_days * 86400, now - window_days * 86400))
    return {r[0]: dict(zip(cols, r)) for r in rows}


def _write_gz(path: Path, data: dict) -> tuple[str, int]:
    """Writes compressed JSON next to its final name, then moves it into place.
    Returns (sha256, size) of the compressed bytes."""
    raw = gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 9, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _load_index(out: Path) -> dict | None:
    try:
        return json.loads((out / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def publish(conn: sqlite3.Connection, out_dir: Path | str, now: float, *, boards: int = 0, failed: int = 0,
            stale_days: float = 2, window_days: float = WINDOW_DAYS) -> dict:
    """Writes this run's postings as the next feed version into out_dir.
    A run that changed nothing only refreshes the index's checked time.
    Returns the index written."""
    out = Path(out_dir)
    migrate(conn)
    current = _open_postings(conn, now, stale_days, window_days)
    published = dict(conn.execute("SELECT key, digest FROM feed_published"))
    digests = {k: digest(r) for k, r in current.items()}
    upsert = [current[k] for k in sorted(current) if published.get(k) != digests[k]]
    gone = sorted(set(published) - set(current))
    last_seen = dict(conn.execute("SELECT key, last_seen FROM jobs WHERE key IN (SELECT value FROM json_each(?))",
                                  (json.dumps(gone),))) if gone else {}
    closed = [[k, last_seen.get(k)] for k in gone]

    _, version, _ = _state(conn)
    index = _load_index(out) or {}
    if upsert or closed or not index:
        version += 1
        changes = list(index.get("changes") or [])
        if version > 1:
            sha, size = _write_gz(out / "changes" / f"{version}.json.gz", {
                "format": FORMAT, "from": version - 1, "to": version, "generated": now,
                "upsert": upsert, "closed": closed})
            changes.append({"from": version - 1, "to": version, "path": f"changes/{version}.json.gz",
                            "sha256": sha, "bytes": size})
        sha, size = _write_gz(out / "snapshots" / f"{version}.json.gz", {
            "format": FORMAT, "version": version, "generated": now,
            "postings": [current[k] for k in sorted(current)]})
        snapshots = [s for s in index.get("snapshots") or [] if s["version"] < version]
        snapshots = snapshots[-(KEEP_SNAPSHOTS - 1):] + [{"version": version, "path": f"snapshots/{version}.json.gz",
                                                          "sha256": sha, "bytes": size}]
        changes = changes[-KEEP_CHANGES:]
    else:
        changes, snapshots = index["changes"], index["snapshots"]

    new_index = {"format": FORMAT, "version": version, "generated": now, "checked": now,
                 "postings": len(current), "boards": boards, "failed": failed,
                 "snapshots": snapshots, "changes": changes}
    tmp = out / "index.json.tmp"
    out.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(new_index, indent=1), encoding="utf-8")
    os.replace(tmp, out / "index.json")     # last, so it only ever names files that exist

    with conn:
        conn.executemany("INSERT OR REPLACE INTO feed_published (key, digest) VALUES (?, ?)",
                         [(r["key"], digests[r["key"]]) for r in upsert])
        conn.executemany("DELETE FROM feed_published WHERE key = ?", [(k,) for k in gone])
        conn.execute("UPDATE feed_state SET version = ?, checked = ? WHERE id = 1", (version, now))
    _prune(out, new_index)
    return new_index


def _prune(out: Path, index: dict) -> None:
    """Deletes files the index no longer names."""
    keep = {s["path"] for s in index["snapshots"]} | {c["path"] for c in index["changes"]}
    for sub in ("snapshots", "changes"):
        for f in (out / sub).glob("*.json.gz") if (out / sub).is_dir() else ():
            if f"{sub}/{f.name}" not in keep:
                try:
                    f.unlink()
                except OSError:
                    pass


# --------------------------------------------------------------------------
# Reading (every copy of the app)

SAFE_PATH = re.compile(r"(snapshots|changes)/\d{1,12}\.json\.gz")


def check_url(url: str) -> str:
    """A feed is read over HTTPS, or from this computer (tests, a local server)."""
    u = urllib.parse.urlparse(url)
    local = u.hostname in ("127.0.0.1", "localhost", "::1")
    if not (u.scheme == "https" or (u.scheme == "http" and local) or u.scheme == "file"):
        raise FeedError("The feed address must start with https://")
    return url if url.endswith("/") else url + "/"


def http_get(url: str, timeout: float = 60) -> bytes:
    if url.startswith("file:"):
        with urllib.request.urlopen(url) as r:       # noqa: S310 (a file the user named)
            return r.read(MAX_FILE + 1)
    req = urllib.request.Request(url, headers={"User-Agent": "Onward/1.0 (feed)", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (checked by check_url)
        return r.read(MAX_FILE + 1)


def _fetch_json(base: str, entry: dict, get: Callable[[str], bytes]) -> dict:
    path = str(entry.get("path", ""))
    if not SAFE_PATH.fullmatch(path):
        raise FeedError(f"The feed names an unexpected file: {path[:80]!r}")
    try:
        raw = get(base + path)
    except Exception as e:
        raise FeedError(f"Couldn't download {path}: {e}") from e
    if len(raw) > MAX_FILE:
        raise FeedError(f"{path} is larger than a feed file can be")
    if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
        raise FeedError(f"{path} doesn't match the feed's index (changed or cut short); try again later")
    try:
        data = json.loads(gzip.decompress(raw))
    except (OSError, ValueError) as e:
        raise FeedError(f"{path} isn't a readable feed file") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise FeedError(f"{path} is in a feed format this version doesn't read; update Onward")
    return data


def clean(rec) -> dict | None:
    """A posting from the feed with only known fields of the right types, or
    None when it lacks what a posting needs."""
    if not isinstance(rec, dict):
        return None
    out = {}
    for k, (typ, cap) in FIELDS.items():
        v = rec.get(k)
        if v is None:
            out[k] = None
        elif typ is str:
            out[k] = str(v)[:cap] if isinstance(v, (str, int, float)) else None
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            out[k] = None
        else:
            out[k] = typ(v)
    if any(out[k] in (None, "") for k in REQUIRED):
        return None
    for k in ("remote", "posted_exact", "entry_hint", "no_opt", "reposts"):
        out[k] = int(out[k] or 0)
    for k in ("sponsorship", "sponsor_note", "apply_url", "verify_url", "posted_field", "posted_raw", "country"):
        out[k] = out[k] or ""
    out["skills"] = out["skills"] or "{}"
    out["description"] = out["description"] or ""
    return out


_UPSERT = None


def _upsert_sql() -> str:
    global _UPSERT
    if _UPSERT is None:
        cols = list(FIELDS)
        _UPSERT = (f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
                   "ON CONFLICT(key) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in cols if c != "key"))
    return _UPSERT


def pull(conn: sqlite3.Connection, url: str, now: float, get: Callable[[str], bytes] = http_get) -> dict:
    """Brings this copy's jobs up to the feed at url. Returns what happened:
    version, new (postings this copy hadn't stored), upserted, closed,
    snapshot (whether the full snapshot was read), checked, boards, failed.
    Raises FeedError, having changed nothing, when the feed can't be read,
    doesn't check out, or is stale."""
    base = check_url(url)
    try:
        index = json.loads(get(base + "index.json"))
    except FeedError:
        raise
    except Exception as e:
        raise FeedError(f"Couldn't read the feed's index: {e}") from e
    if not isinstance(index, dict) or index.get("format") != FORMAT:
        raise FeedError("The feed is in a format this version doesn't read; update Onward")
    try:
        version, checked = int(index["version"]), float(index["checked"])
        snapshots, changes = list(index["snapshots"]), list(index["changes"])
    except (KeyError, TypeError, ValueError) as e:
        raise FeedError("The feed's index is incomplete") from e
    if now - checked > STALE_AFTER:
        raise FeedError(f"The feed hasn't been updated for {(now - checked) / 3600:.0f} hours")

    migrate(conn)
    have_url, have, _ = _state(conn)
    if have_url != base:
        have = 0                    # another feed: start over from its snapshot
    steps = {int(c["from"]): c for c in changes if isinstance(c, dict) and "from" in c}
    chain = []
    v = have
    while have and v < version and v in steps:
        chain.append(steps[v])
        v = int(steps[v]["to"])
    use_snapshot = not have or v != version or have > version

    upsert: dict[str, dict] = {}
    closed: dict[str, float | None] = {}
    if use_snapshot:
        snap = next((s for s in snapshots if isinstance(s, dict) and int(s.get("version", -1)) == version), None)
        if snap is None:
            raise FeedError("The feed's index names no snapshot for its version")
        data = _fetch_json(base, snap, get)
        for rec in data.get("postings") or []:
            r = clean(rec)
            if r:
                upsert[r["key"]] = r
    elif version != have:
        for entry in chain:
            data = _fetch_json(base, entry, get)
            for rec in data.get("upsert") or []:
                r = clean(rec)
                if r:
                    upsert[r["key"]] = r
                    closed.pop(r["key"], None)
            for item in data.get("closed") or []:
                if isinstance(item, list) and item and isinstance(item[0], str):
                    last = item[1] if len(item) > 1 and isinstance(item[1], (int, float)) else None
                    closed[item[0]] = last
                    upsert.pop(item[0], None)

    existing = {k for (k,) in conn.execute("SELECT key FROM jobs WHERE key IN (SELECT value FROM json_each(?))",
                                           (json.dumps(list(upsert)),))} if upsert else set()
    with conn:
        conn.executemany(_upsert_sql(), list(upsert.values()))
        if use_snapshot:
            # What the snapshot doesn't list has closed (or was never this feed's).
            conn.execute("DELETE FROM feed_members")
        else:
            conn.executemany("DELETE FROM feed_members WHERE key = ?", [(k,) for k in closed])
            for k, last in closed.items():
                if last is not None:
                    conn.execute("UPDATE jobs SET last_seen = MIN(last_seen, ?) WHERE key = ?", (last, k))
        conn.executemany("INSERT OR IGNORE INTO feed_members (key) VALUES (?)", [(k,) for k in upsert])
        # Still listed: still open as of the search's last run.
        conn.execute("UPDATE jobs SET last_seen = MAX(last_seen, ?) WHERE key IN (SELECT key FROM feed_members)",
                     (checked,))
        conn.execute("UPDATE feed_state SET url = ?, version = ?, checked = ? WHERE id = 1", (base, version, checked))
    return {"version": version, "new": len(set(upsert) - existing), "upserted": len(upsert), "closed": len(closed),
            "snapshot": use_snapshot, "checked": checked, "boards": int(index.get("boards") or 0),
            "failed": int(index.get("failed") or 0)}
