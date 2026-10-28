"""The shared job feed (feed.py): publishing versions from a search, and
clients catching up from the snapshot or the change files, never applying a
file that doesn't check out. Then radar.py reading a feed instead of
searching, and searching by itself when the feed can't be used.

Run: python -m unittest tests.test_feed
"""

import gzip
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import feed
import radar


def posting(company, title, jid, days_ago=1, location="Boston, MA"):
    j = radar.build_job(
        company=company, title=title, location=location, country="US", remote=False,
        url=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{jid}", source="greenhouse",
        posted_at=time.time() - days_ago * 86400, posted_exact=True,
        description="Requirements:\n- Experience with Python and PostgreSQL\n",
        apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={company.lower()}&token={jid}",
        verify_url="", posted_field="first_published", posted_raw="x")
    assert j is not None, title
    return j


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-feed-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config.use(config.Store(self.tmp / "settings.json"))
        self.addCleanup(config.use, None)
        self.server = self.db("server.db")
        self.client = self.db("client.db")
        self.out = self.tmp / "out"
        self.url = self.out.as_uri()
        self.now = time.time()

    def db(self, name):
        conn = sqlite3.connect(self.tmp / name)
        self.addCleanup(conn.close)
        radar.migrate(conn)
        return conn

    def search(self, jobs, at):
        """A search run on the server, then its feed."""
        with self.server:
            radar.store(self.server, jobs, at)
        return feed.publish(self.server, self.out, at, boards=3, failed=1)

    def keys(self, conn):
        return {k for (k,) in conn.execute("SELECT key FROM jobs")}

    def open_keys(self, conn, at):
        return {k for (k,) in conn.execute("SELECT key FROM jobs WHERE last_seen >= ?", (at - 2 * 86400,))}


class PublishTests(Base):
    def test_first_version_is_a_snapshot(self):
        idx = self.search([posting("Aco", "Backend Engineer I", 1), posting("Bco", "Software Engineer", 2)], self.now)
        self.assertEqual((idx["version"], idx["postings"], idx["changes"], idx["boards"]), (1, 2, [], 3))
        snap = json.loads(gzip.decompress((self.out / idx["snapshots"][0]["path"]).read_bytes()))
        self.assertEqual(len(snap["postings"]), 2)
        for p in snap["postings"]:
            self.assertEqual(set(p), set(feed.FIELDS), "only posting facts, nothing about a user")

    def test_changes_hold_only_what_changed(self):
        a, b = posting("Aco", "Backend Engineer I", 1), posting("Bco", "Software Engineer", 2)
        self.search([a, b], self.now)
        a.sponsorship = "no"
        c = posting("Cco", "Data Engineer I", 3)
        idx = self.search([a, c], self.now + 1800)
        self.assertEqual(idx["version"], 2)
        change = json.loads(gzip.decompress((self.out / idx["changes"][-1]["path"]).read_bytes()))
        self.assertEqual(sorted(p["company"] for p in change["upsert"]), ["Aco", "Cco"])
        self.assertEqual(change["closed"], [])     # Bco is unseen, not yet closed
        # Two days unseen: closed, with when it was last seen.
        idx = self.search([a, c], self.now + 3 * 86400)
        change = json.loads(gzip.decompress((self.out / idx["changes"][-1]["path"]).read_bytes()))
        self.assertEqual([k for k, _ in change["closed"]], [b.key])
        self.assertAlmostEqual(change["closed"][0][1], self.now, delta=1)

    def test_a_run_that_changed_nothing_only_refreshes_the_index(self):
        a = posting("Aco", "Backend Engineer I", 1)
        self.search([a], self.now)
        idx = self.search([a], self.now + 1800)
        self.assertEqual(idx["version"], 1, "last_seen moving isn't a change")
        self.assertEqual(idx["checked"], self.now + 1800)

    def test_old_files_are_pruned(self):
        with mock.patch.object(feed, "KEEP_CHANGES", 2):
            for i in range(5):
                self.search([posting("Aco", f"Backend Engineer, Team {'ABCDE'[i]}", i)], self.now + i)
        idx = json.loads((self.out / "index.json").read_text())
        self.assertEqual([c["to"] for c in idx["changes"]], [4, 5])
        self.assertEqual(sorted(f.name for f in (self.out / "changes").iterdir()), ["4.json.gz", "5.json.gz"])
        self.assertEqual(sorted(f.name for f in (self.out / "snapshots").iterdir()), ["4.json.gz", "5.json.gz"])


class PullTests(Base):
    def test_new_copy_reads_the_snapshot_then_changes(self):
        a, b = posting("Aco", "Backend Engineer I", 1), posting("Bco", "Software Engineer", 2)
        self.search([a, b], self.now)
        r = feed.pull(self.client, self.url, self.now + 60)
        self.assertEqual((r["version"], r["new"], r["snapshot"], r["boards"]), (1, 2, True, 3))
        self.assertEqual(self.keys(self.client), {a.key, b.key})
        first_seen = self.client.execute("SELECT first_seen FROM jobs WHERE key = ?", (a.key,)).fetchone()[0]
        self.assertAlmostEqual(first_seen, self.now, delta=1, msg="the search's own first sighting")

        c = posting("Cco", "Data Engineer I", 3)
        self.search([a, b, c], self.now + 1800)
        r = feed.pull(self.client, self.url, self.now + 1860)
        self.assertEqual((r["version"], r["new"], r["upserted"], r["snapshot"]), (2, 1, 1, False))
        # Still listed: still open as of the search's last run.
        self.assertEqual(self.open_keys(self.client, self.now + 1860), {a.key, b.key, c.key})

        self.search([a, c], self.now + 3 * 86400)
        r = feed.pull(self.client, self.url, self.now + 3 * 86400 + 60)
        self.assertEqual(r["closed"], 1)
        self.assertEqual(self.open_keys(self.client, self.now + 3 * 86400 + 60), {a.key, c.key})

    def test_up_to_date_copy_downloads_nothing_but_the_index(self):
        self.search([posting("Aco", "Backend Engineer I", 1)], self.now)
        feed.pull(self.client, self.url, self.now)
        got = []
        r = feed.pull(self.client, self.url, self.now, get=lambda u: got.append(u) or feed.http_get(u))
        self.assertEqual((r["upserted"], [u.rsplit("/", 1)[-1] for u in got]), (0, ["index.json"]))

    def test_far_behind_copy_reads_the_snapshot(self):
        with mock.patch.object(feed, "KEEP_CHANGES", 1):
            self.search([posting("Aco", "Backend Engineer I", 1)], self.now)
            feed.pull(self.client, self.url, self.now)
            for i in range(3):
                self.search([posting("Aco", "Backend Engineer I", 1), posting("Bco", f"Software Engineer, Team {'ABC'[i]}", 10 + i)],
                            self.now + i + 1)
        r = feed.pull(self.client, self.url, self.now + 10)
        self.assertTrue(r["snapshot"])
        self.assertEqual(r["version"], 4)

    def test_a_file_that_doesnt_check_out_changes_nothing(self):
        self.search([posting("Aco", "Backend Engineer I", 1)], self.now)
        feed.pull(self.client, self.url, self.now)
        idx = self.search([posting("Aco", "Backend Engineer I", 1), posting("Bco", "Software Engineer", 2)],
                          self.now + 60)
        path = self.out / idx["changes"][-1]["path"]
        path.write_bytes(path.read_bytes()[:-5])         # cut short
        before = self.keys(self.client)
        with self.assertRaisesRegex(feed.FeedError, "doesn't match"):
            feed.pull(self.client, self.url, self.now + 120)
        self.assertEqual(self.keys(self.client), before)
        self.assertEqual(self.client.execute("SELECT version FROM feed_state").fetchone()[0], 1)

    def test_stale_or_odd_feeds_are_refused(self):
        self.search([posting("Aco", "Backend Engineer I", 1)], self.now)
        with self.assertRaisesRegex(feed.FeedError, "hasn't been updated"):
            feed.pull(self.client, self.url, self.now + 4 * 3600)
        idx = json.loads((self.out / "index.json").read_text())
        idx["snapshots"][0]["path"] = "../../settings.json"
        (self.out / "index.json").write_text(json.dumps(idx))
        with self.assertRaisesRegex(feed.FeedError, "unexpected file"):
            feed.pull(self.client, self.url, self.now)
        (self.out / "index.json").write_text('{"format": 99}')
        with self.assertRaisesRegex(feed.FeedError, "format"):
            feed.pull(self.client, self.url, self.now)
        with self.assertRaisesRegex(feed.FeedError, "https"):
            feed.pull(self.client, "http://feed.example.com/", self.now)
        self.assertEqual(self.keys(self.client), set())

    def test_records_are_cleaned(self):
        good = {k: None for k in feed.FIELDS} | {"key": "k", "company": "Aco", "title": "T", "location": "L",
                                                 "url": "u", "source": "greenhouse", "first_seen": 1, "last_seen": 2}
        r = feed.clean(good | {"extra": "dropped", "title": "x" * 10000, "remote": True, "pay_min": "lots"})
        self.assertNotIn("extra", r)
        self.assertEqual((len(r["title"]), r["remote"], r["pay_min"], r["skills"]), (500, 0, None, "{}"))
        self.assertIsNone(feed.clean(good | {"key": ""}))
        self.assertIsNone(feed.clean(["not", "a", "posting"]))


class RadarWithFeedTests(Base):
    """radar.main() reads the feed instead of searching, and searches every
    board itself when the feed can't be used."""

    def setUp(self):
        # radar.main() sets these from the test's settings; put them back after.
        saved = {k: getattr(radar, k) for k in (
            "DB_PATH", "DASHBOARD_PATH", "LOG_PATH", "H1B_DIR", "DASHBOARD_DAYS", "PROGRAM_DAYS", "FETCH_DAYS",
            "STALE_AFTER_DAYS", "PURGE_AFTER_DAYS", "WORKERS", "TIMEOUT", "ORACLE_DATE_FACET", "SEARCH", "SETTINGS",
            "STATUS")}
        self.addCleanup(lambda: [setattr(radar, k, v) for k, v in saved.items()])
        super().setUp()
        self.store = config.Store(self.tmp / "client")
        config.use(self.store)
        self.search([posting("Aco", "Backend Engineer I", 1)], self.now)
        self.calls = []
        empty = radar.FetchResult(jobs=[], missing=[], failed=[], throttled=[], attempted=[])
        for target, value in (("fetch_all", lambda companies, skip, simplify, progress: self.calls.append(
                                  (companies, simplify)) or empty),
                              ("update_h1b", lambda: None), ("discover_step", lambda d: self.calls.append("discover")),
                              ("needs_visa_data", lambda s: False)):
            p = mock.patch.object(radar, target, value)
            p.start()
            self.addCleanup(p.stop)

    def boards_searched(self):
        companies, simplify = self.calls[0]
        return sum(len(v) for k, v in companies.items() if k in radar.FETCHERS), simplify

    def test_feed_replaces_the_search(self):
        self.store.update({"radar": {"feed_url": self.url},
                           "companies": {"added": {"lever": ["mycompany"]}}})
        self.assertEqual(radar.main([]), 0)
        self.assertEqual(self.boards_searched(), (1, False), "only the company the user added")
        self.assertNotIn("discover", self.calls)
        conn = sqlite3.connect(self.store.path_for("db"))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT company FROM jobs").fetchall(), [("Aco",)])
        self.assertEqual(conn.execute("SELECT boards, new FROM runs").fetchone(), (4, 1))

    def test_unusable_feed_means_searching_everything(self):
        self.store.update({"radar": {"feed_url": "https://127.0.0.1:1/nothing-here/"}})
        with mock.patch.object(feed, "pull", side_effect=feed.FeedError("down")):
            self.assertEqual(radar.main([]), 0)
        n, simplify = self.boards_searched()
        self.assertGreater(n, 100)
        self.assertTrue(simplify)
        self.assertIn("discover", self.calls)

    def test_settings_take_only_feed_addresses(self):
        for ok in ("https://feed.example.com/onward/", "http://127.0.0.1:8080/feed/", ""):
            self.assertEqual(self.store.update({"radar": {"feed_url": ok}})["radar"]["feed_url"], ok)
        with self.assertRaises(config.SettingsError):
            self.store.update({"radar": {"feed_url": "http://feed.example.com/"}})


if __name__ == "__main__":
    unittest.main()
