"""After applying, "next job" continues down the list the user clicked Apply
from in the dashboard, skipping jobs already applied to or dismissed, and
falls back to the Apply queue.

Run:  python -m unittest test_apply_next
"""

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server


class FakePrep:
    def __init__(self, ready=None):
        self.ready = ready

    def next_ready(self, exclude=""):
        return self.ready


class NextJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE jobs (key TEXT PRIMARY KEY, company TEXT, title TEXT, apply_url TEXT, url TEXT)")
        for i in range(1, 6):
            conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
                         (f"k{i}", f"Co {i}", f"Engineer {i}", f"https://jobs.lever.co/co{i}/x/apply", f"https://jobs.lever.co/co{i}/x"))
        conn.commit()
        conn.close()
        self.patches = [mock.patch.object(server, "DB_PATH", self.db),
                        mock.patch.object(server, "PREP", FakePrep({"key": "q1", "title": "Queued", "company": "Q Co",
                                                                     "apply_url": "https://q.example/apply", "score": 80}))]
        for p in self.patches:
            p.start()
        conn = server.db()   # creates the prep table
        conn.executemany("INSERT INTO prep (job_key, state, created, updated) VALUES (?, ?, 0, 0)",
                         [("k2", "applied"), ("k3", "dismissed")])
        conn.commit()
        conn.close()
        server.set_apply_list(["k1", "k2", "k3", "k4", "k5"])

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server.set_apply_list([])
        self.tmp.cleanup()

    def test_next_in_list_skips_applied_and_dismissed(self):
        n = server.next_job("k1")
        self.assertEqual((n["key"], n["company"], n["apply_url"], n["from"]), ("k4", "Co 4", "https://jobs.lever.co/co4/x/apply", "list"))

    def test_end_of_list_falls_back_to_queue(self):
        self.assertEqual(server.next_job("k5")["from"], "queue")

    def test_job_not_in_list_uses_queue(self):
        self.assertEqual(server.next_job("elsewhere")["key"], "q1")

    def test_nothing_anywhere(self):
        server.PREP.ready = None
        self.assertIsNone(server.next_job("k5"))


class Routes(unittest.TestCase):
    """POST /api/apply/list comes only from the dashboard; GET /api/apply/next only from the extension."""

    @classmethod
    def setUpClass(cls):
        cls.port = 8797
        cls.patches = [mock.patch.object(server, "PORT", cls.port),
                       mock.patch.object(server, "ORIGINS", {f"http://127.0.0.1:{cls.port}"}),
                       mock.patch.object(server, "PREP", FakePrep(None))]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        server.set_apply_list([])

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_dashboard_sets_the_list(self):
        status, body = self.call("POST", "/api/apply/list", {"keys": ["a", "b"]}, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual((status, body["count"]), (200, 2))
        self.assertEqual(server.APPLY_LIST["keys"], ["a", "b"])

    def test_websites_and_extension_cannot_set_the_list(self):
        for h in ({"Origin": "https://evil.example"}, {"Origin": "moz-extension://abc", "X-Onward-Client": "autofill"}):
            self.assertEqual(self.call("POST", "/api/apply/list", {"keys": ["x"]}, h)[0], 403)

    def test_older_extensions_header_still_accepted(self):
        status, _ = self.call("GET", "/api/apply/next?key=zzz", None, {"Origin": "moz-extension://abc", "X-JobRadar-Client": "autofill"})
        self.assertEqual(status, 200)

    def test_next_is_for_the_extension_only(self):
        self.assertEqual(self.call("GET", "/api/apply/next?key=a", None, {"Origin": "https://evil.example"})[0], 403)
        status, body = self.call("GET", "/api/apply/next?key=zzz", None, {"Origin": "moz-extension://abc", "X-Onward-Client": "autofill"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
