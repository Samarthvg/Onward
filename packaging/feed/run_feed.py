"""One run of the shared job feed's search, on a machine that keeps nothing
between runs (a GitHub Actions runner): restore the search's database, search,
publish, upload, save the database.

  python packaging/feed/run_feed.py

reads its storage from the environment:

  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
  FEED_BUCKET    public: what copies of Onward download (feed.py's files)
  STATE_BUCKET   private: the search's database between runs

or, for trying it out, --store DIR keeps both in folders under DIR.

The order is what keeps readers consistent if a step fails: new snapshot and
change files first (nothing names them yet), then the database, then
index.json, which makes the new version visible, and only then the deletion
of files the new index no longer names. A run that fails before the index
leaves the feed as it was; the next one publishes that version again from
the saved database, overwriting the files nothing points to.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = Path(__file__).with_name("feed-settings.json")
STATE_KEY = "jobs.db.gz"
IMMUTABLE = "public, max-age=31536000, immutable"   # a version's files never change once named
INDEX_CACHE = "public, max-age=60"


class DirStore:
    """A bucket as a folder."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def get(self, key: str) -> bytes | None:
        try:
            return (self.root / key).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key: str, data: bytes, content_type: str, cache: str | None = None) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def delete(self, key: str) -> None:
        try:
            (self.root / key).unlink()
        except FileNotFoundError:
            pass

    def keys(self, prefix: str) -> set[str]:
        base = self.root / prefix
        return {p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file()} if base.is_dir() else set()


class R2Store:
    """A Cloudflare R2 bucket, through its S3-compatible API."""

    def __init__(self, bucket: str, client) -> None:
        self.bucket, self.s3 = bucket, client

    @staticmethod
    def client(account: str, key_id: str, secret: str):
        import boto3
        return boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                            aws_access_key_id=key_id, aws_secret_access_key=secret, region_name="auto")

    def get(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def put(self, key: str, data: bytes, content_type: str, cache: str | None = None) -> None:
        extra = {"CacheControl": cache} if cache else {}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def keys(self, prefix: str) -> set[str]:
        out: set[str] = set()
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.update(o["Key"] for o in page.get("Contents", []))
        return out


def search(home: Path, out: Path) -> None:
    """radar.py's search into the feed folder, with home as its data folder."""
    env = {**os.environ, "ONWARD_HOME": str(home)}
    code = subprocess.run([sys.executable, str(ROOT / "radar.py"), "--feed-out", str(out)], cwd=ROOT, env=env).returncode
    if code != 0:
        raise SystemExit(f"the search failed (exit {code}); nothing was published")


def named(index: dict) -> set[str]:
    return {s["path"] for s in index.get("snapshots", [])} | {c["path"] for c in index.get("changes", [])}


def run(feed_store, state_store, work: Path, search_fn: Callable[[Path, Path], None] = search,
        log: Callable[[str], None] = print) -> dict:
    """One run. Returns the index it published."""
    home, out = work / "home", work / "public"
    home.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SETTINGS, home / "settings.json")

    saved = state_store.get(STATE_KEY)
    if saved:
        (home / "jobs.db").write_bytes(gzip.decompress(saved))
        log(f"restored the search's database ({len(saved) // 1024} KB compressed)")
    else:
        log("no saved database: this is the feed's first run")
    published = feed_store.get("index.json")
    if published:
        (out / "index.json").write_bytes(published)     # publish() builds on the versions already out
    before = json.loads(published) if published else {}

    search_fn(home, out)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))

    there = feed_store.keys("snapshots/") | feed_store.keys("changes/")
    # Everything this run wrote, even under a name already in the bucket: a run
    # that failed before its index may have left a different file there.
    for path in sorted(named(index) - named(before)):
        feed_store.put(path, (out / path).read_bytes(), "application/gzip", IMMUTABLE)
        log(f"uploaded {path}")
    state_store.put(STATE_KEY, gzip.compress((home / "jobs.db").read_bytes(), 6), "application/gzip")
    feed_store.put("index.json", (out / "index.json").read_bytes(), "application/json", INDEX_CACHE)
    for path in sorted(there - named(index)):
        feed_store.delete(path)
    log(f"feed version {index['version']} (was {before.get('version', 'none')}), {index['postings']} postings")
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", metavar="DIR", help="keep the feed and the database in folders under DIR instead of R2")
    args = ap.parse_args(argv)
    if args.store:
        feed_store, state_store = DirStore(Path(args.store) / "feed"), DirStore(Path(args.store) / "state")
    else:
        need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "FEED_BUCKET", "STATE_BUCKET")
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            print("Not set up yet, so nothing to do: missing " + ", ".join(missing))
            return 0
        client = R2Store.client(os.environ["R2_ACCOUNT_ID"], os.environ["R2_ACCESS_KEY_ID"],
                                os.environ["R2_SECRET_ACCESS_KEY"])
        feed_store, state_store = R2Store(os.environ["FEED_BUCKET"], client), R2Store(os.environ["STATE_BUCKET"], client)
    with tempfile.TemporaryDirectory(prefix="onward-feed-") as work:
        run(feed_store, state_store, Path(work))
    return 0


if __name__ == "__main__":
    sys.exit(main())
