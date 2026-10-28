"""Starting the app: something shows at once, and the dashboard follows.

While a page that needs rebuilding (after an update) is rebuilt, the helper
serves the loading page and reports the rebuild's progress; the window shows
the same page as a splash until the helper answers.

Run: python -m unittest test_startup
"""

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import app
import radar
import server


class LoadingPageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onward-start-"))
        page = self.dir / "dashboard.html"
        page.write_text("<!doctype html><p>the dashboard</p>", encoding="utf-8")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = httpd.server_address[1]
        for p in (mock.patch.object(server, "PORT", self.port), mock.patch.object(server, "DASHBOARD", page),
                  mock.patch.dict(server.RENDER, {"stale": True, "phase": "Scoring postings", "done": 250, "total": 1000,
                                                  "error": ""})):
            p.start()
            self.addCleanup(p.stop)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)

    def get(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": f"127.0.0.1:{self.port}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode("utf-8")

    def test_the_loading_page_until_the_rebuild_finishes(self):
        self.assertIn("Starting Onward", self.get("/"))
        self.assertEqual(json.loads(self.get("/api/render")),
                         {"ready": False, "error": "", "phase": "Scoring postings", "done": 250, "total": 1000})
        server.RENDER["stale"] = False
        self.assertIn("the dashboard", self.get("/"))
        self.assertTrue(json.loads(self.get("/api/render"))["ready"])

    def test_the_window_waits_for_the_helper(self):
        url = f"http://127.0.0.1:{self.port}/"
        self.assertTrue(app.helper_answers(url))
        self.assertFalse(app.helper_answers("http://127.0.0.1:9/"), "nothing listening")


class PrepareTests(unittest.TestCase):
    def test_the_app_rebuilds_in_the_background(self):
        with mock.patch.object(radar, "page_current", return_value=False), \
                mock.patch.object(radar, "rerender") as rerender, \
                mock.patch.object(server, "schedule_render") as later, \
                mock.patch.dict(server.RENDER, {"stale": False}):
            server.prepare(render_now=False)
            rerender.assert_not_called()
            later.assert_called_once()
            self.assertTrue(server.RENDER["stale"], "the loading page is served meanwhile")
            server.prepare()                        # python server.py: before serving, as before
            rerender.assert_called_once()

    def test_the_rebuild_reports_its_progress(self):
        seen = []
        with mock.patch.object(radar, "corpus_stats", side_effect=lambda conn, progress: (
                progress("Reading postings", 0, 2), {"skill_w": {}, "term_w": {}})[1]), \
                mock.patch.object(radar, "load_for_dashboard", side_effect=lambda *a: (
                    a[-1]("Scoring postings", 1, 2), [])[1]), \
                mock.patch.object(radar, "save_scoring"), mock.patch.object(radar, "render"):
            radar.build_dashboard(None, radar.H1BIndex(), {k: None for k in (
                "started", "generated", "previous_run", "first_run", "boards", "failed", "missing", "new")},
                lambda *p: seen.append(p))
        self.assertEqual(seen, [("Reading postings", 0, 2), ("Scoring postings", 1, 2), ("Building the page", 0, 0)])


class LaunchTests(unittest.TestCase):
    def test_a_login_start_stays_in_the_tray(self):
        with mock.patch.object(app, "App") as App:
            app.main(["--login"])
            App.assert_called_with(headless=False, at_login=True)
            app.main([])
            App.assert_called_with(headless=False, at_login=False)


if __name__ == "__main__":
    unittest.main()
