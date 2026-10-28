"""GET /api/job/keywords: the checklist the match score uses for the job whose
form the extension is filling, so autofill adds the skills that job asks for.

Run:  python -m unittest tests.test_job_keywords
"""

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server

WD = "https://mockco.wd5.myworkdayjobs.com/External/job/Boston-MA/Backend-Engineer_JR2002"
KW = [["Java", 1.5, "title"], ["REST APIs", 1.0, "req"], ["PostgreSQL", 0.5, "pref"]]


class JobKeywords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "jobs.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE jobs (key TEXT PRIMARY KEY, company TEXT, title TEXT, description TEXT, "
                     "apply_url TEXT, url TEXT, kw TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO jobs VALUES ('k1', 'Mock Co', 'Backend Engineer', 'desc', ?, ?, ?)", (WD, WD, json.dumps(KW)))
        conn.execute("INSERT INTO jobs VALUES ('k2', 'Mock Co', 'No Description', '', ?, ?, '')",
                     ("https://jobs.lever.co/mockco/x/apply", "https://jobs.lever.co/mockco/x"))
        conn.commit()
        conn.close()
        cls.port = 8798
        cls.patches = [mock.patch.object(server, "DB_PATH", db), mock.patch.object(server, "PORT", cls.port),
                       mock.patch.object(server, "ORIGINS", {f"http://127.0.0.1:{cls.port}"})]
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
        cls.tmp.cleanup()

    def call(self, page_url, origin="moz-extension://abc"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/job/keywords?url=" + urllib.parse.quote(page_url, safe=""),
                                     headers={"Origin": origin, "X-Onward-Client": "autofill"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_workday_application_step_finds_the_job(self):
        # The form's URL carries a locale and the apply step; the stored one has neither.
        status, body = self.call("https://mockco.wd5.myworkdayjobs.com/en-US/External/job/Boston-MA/Backend-Engineer_JR2002/apply/applyManually")
        self.assertEqual(status, 200)
        self.assertEqual((body["found"], body["key"], body["keywords"]), (True, "k1", KW))

    def test_unknown_job(self):
        self.assertEqual(self.call("https://jobs.lever.co/other/y/apply"), (200, {"found": False, "keywords": []}))

    def test_job_without_keywords(self):
        status, body = self.call("https://jobs.lever.co/mockco/x/apply")
        self.assertEqual((status, body["found"], body["keywords"]), (200, False, []))

    def test_extension_only(self):
        self.assertEqual(self.call(WD, origin="https://evil.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
