"""packaging/feed/run_feed.py: runs of the feed's search on machines that
keep nothing, with the database and the feed in buckets (folders here, and
R2's API through botocore's stubber), and readers following along.

Run: python -m unittest tests.test_feed_run
"""

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import feed
import radar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging" / "feed"))
import run_feed  # noqa: E402

from tests.test_feed import posting  # noqa: E402


class FakeSearch:
    """Stands in for radar.py --feed-out: stores this run's postings in the
    restored database and publishes, as the real search does."""

    def __init__(self):
        self.jobs, self.at = [], time.time()

    def __call__(self, home: Path, out: Path) -> None:
        settings = json.loads((home / "settings.json").read_text())
        assert settings["roles"][0]["id"] == "feed", "the feed's own settings"
        conn = sqlite3.connect(home / "jobs.db")
        try:
            radar.migrate(conn)
            with conn:
                radar.store(conn, self.jobs, self.at)
            feed.publish(conn, out, self.at, boards=2)
        finally:
            conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-feedrun-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config.use(config.Store(self.tmp / "settings.json"))
        self.addCleanup(config.use, None)
        self.feed = run_feed.DirStore(self.tmp / "bucket-feed")
        self.state = run_feed.DirStore(self.tmp / "bucket-state")
        self.search = FakeSearch()
        self.reader = sqlite3.connect(self.tmp / "reader.db")
        self.addCleanup(self.reader.close)
        radar.migrate(self.reader)
        self.runs = 0

    def run_once(self, jobs, at, **kw):
        """A run on a fresh machine: its own empty working folder."""
        self.runs += 1
        self.search.jobs, self.search.at = jobs, at
        work = self.tmp / f"runner-{self.runs}"
        return run_feed.run(kw.pop("feed_store", self.feed), kw.pop("state_store", self.state), work,
                            self.search, log=lambda m: None)

    def pull(self, at):
        return feed.pull(self.reader, (self.tmp / "bucket-feed").as_uri(), at)


class RunTests(Base):
    def test_runs_on_fresh_machines_continue_one_feed(self):
        now = time.time()
        a, b = posting("Aco", "Backend Engineer I", 1), posting("Bco", "Software Engineer", 2)
        idx = self.run_once([a, b], now)
        self.assertEqual((idx["version"], idx["postings"]), (1, 2))
        self.assertIsNotNone(self.state.get(run_feed.STATE_KEY), "the database is saved for the next run")
        self.assertEqual(self.pull(now + 60)["new"], 2)

        # The next machine restores the database: only the new posting is a change.
        c = posting("Cco", "Data Engineer I", 3)
        idx = self.run_once([a, b, c], now + 1800)
        self.assertEqual(idx["version"], 2)
        r = self.pull(now + 1860)
        self.assertEqual((r["upserted"], r["snapshot"]), (1, False))
        restored = sqlite3.connect(self.tmp / "runner-2" / "home" / "jobs.db")
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)

        # Files the index no longer names are deleted from the bucket.
        with mock.patch.object(feed, "KEEP_SNAPSHOTS", 1):
            self.run_once([a, c], now + 3 * 86400)
        self.assertEqual(self.feed.keys("snapshots/"), {"snapshots/3.json.gz"})

    def test_a_run_that_fails_before_its_index_changes_nothing_readers_see(self):
        now = time.time()
        a = posting("Aco", "Backend Engineer I", 1)
        self.run_once([a], now)
        self.pull(now)

        class Broken(run_feed.DirStore):
            def put(self, key, *args, **kw):
                raise OSError("the bucket went away")
        with self.assertRaises(OSError):
            self.run_once([a, posting("Bco", "Software Engineer", 2)], now + 1800,
                          state_store=Broken(self.tmp / "bucket-state"))
        self.assertEqual(json.loads(self.feed.get("index.json"))["version"], 1)
        self.assertEqual(self.pull(now + 1860)["upserted"], 0)

        # The next run publishes version 2 again, from the saved database, over
        # the file the failed run left; readers get the new one, checksum and all.
        idx = self.run_once([a, posting("Bco", "Software Engineer", 2), posting("Cco", "Data Engineer I", 3)],
                            now + 3600)
        self.assertEqual(idx["version"], 2)
        r = self.pull(now + 3660)
        self.assertEqual((r["version"], r["upserted"], r["snapshot"]), (2, 2, False))

    def test_not_set_up_does_nothing(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("R2_", "FEED_", "STATE_"))}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out):
            self.assertEqual(run_feed.main([]), 0)
        self.assertIn("Not set up yet", out.getvalue())

    def test_feed_settings_load_cleanly(self):
        home = self.tmp / "home"
        home.mkdir()
        shutil.copyfile(run_feed.SETTINGS, home / "settings.json")
        st = config.Store(home / "settings.json", home)
        s = st.load()
        self.assertEqual(st.warnings, [])
        self.assertEqual(s["roles"][0]["seniority"], ["intern", "entry", "mid"])
        self.assertEqual(s["radar"]["feed_url"], "", "the search never reads a feed itself")


class R2StoreTests(unittest.TestCase):
    """The calls R2Store makes, checked by botocore's stubber (no network)."""

    def setUp(self):
        try:
            import boto3
            from botocore.stub import Stubber
        except ImportError:
            self.skipTest("boto3 isn't installed")
        self.s3 = boto3.client("s3", endpoint_url="https://acct.r2.cloudflarestorage.com", region_name="auto",
                               aws_access_key_id="test", aws_secret_access_key="test")
        self.stub = Stubber(self.s3)
        self.store = run_feed.R2Store("onward-feed", self.s3)

    def test_calls(self):
        from botocore.response import StreamingBody
        body = b"\x1f\x8b data"
        self.stub.add_response("put_object", {}, {"Bucket": "onward-feed", "Key": "index.json", "Body": body,
                                                  "ContentType": "application/json",
                                                  "CacheControl": run_feed.INDEX_CACHE})
        self.stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(body), len(body))},
                               {"Bucket": "onward-feed", "Key": "index.json"})
        self.stub.add_client_error("get_object", "NoSuchKey", http_status_code=404,
                                   expected_params={"Bucket": "onward-feed", "Key": "jobs.db.gz"})
        self.stub.add_response("list_objects_v2", {"Contents": [{"Key": "changes/2.json.gz"}], "IsTruncated": False},
                               {"Bucket": "onward-feed", "Prefix": "changes/"})
        self.stub.add_response("delete_object", {}, {"Bucket": "onward-feed", "Key": "changes/2.json.gz"})
        with self.stub:
            self.store.put("index.json", body, "application/json", run_feed.INDEX_CACHE)
            self.assertEqual(self.store.get("index.json"), body)
            self.assertIsNone(self.store.get("jobs.db.gz"))
            self.assertEqual(self.store.keys("changes/"), {"changes/2.json.gz"})
            self.store.delete("changes/2.json.gz")
        self.stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
