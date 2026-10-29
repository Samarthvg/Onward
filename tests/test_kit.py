"""The Apply kit and what the extension reads from the app (kit.py, /api/kit),
and the master resume's PDF the extension attaches.

Run:  python -m unittest tests.test_kit
"""

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import config
import kit
import resume_tex as rt
import server

MASTER = (Path(__file__).parent / "fixtures" / "history_master.tex").read_text(encoding="utf-8")
EXT = {"Origin": "moz-extension://1234-abcd", "X-Onward-Client": "autofill", "X-Onward-Version": "0.3.0"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-kit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = config.Store(self.tmp / "settings.json", self.tmp)
        config.use(self.store)
        self.addCleanup(config.use, None)


class StoreTests(Base):
    def test_the_newer_copy_wins(self):
        kit.save("answers", {"workAuth": "yes"}, 2000)
        self.assertEqual(kit.save("answers", {"workAuth": "no"}, 1000)["value"], {"workAuth": "yes"})
        self.assertEqual(kit.value("answers"), {"workAuth": "yes"})
        with self.assertRaises(ValueError):
            kit.save("memory", {"mem:x": "not an object"} | {f"mem:{i}": {} for i in range(kit.MAX_MEMORY + 1)}, 1)

    def test_old_answers_in_words_move_out_of_the_profile(self):
        kit.path().parent.mkdir(parents=True, exist_ok=True)
        kit.path().write_text(json.dumps({
            "profile": {"value": {"firstName": "Testy", "workAuth": "F-1 OPT (TEST)", "sponsorship": "Will need H-1B (TEST)"}, "at": 5},
            "answers": {"value": {"sponsorshipText": "Already set (TEST)"}, "at": 3}}), encoding="utf-8")
        st = kit.state()
        self.assertEqual(st["profile"]["value"], {"firstName": "Testy"})
        self.assertEqual(st["answers"]["value"], {"workAuthText": "F-1 OPT (TEST)", "sponsorshipText": "Already set (TEST)"})
        # Written back, so it happens once.
        self.assertNotIn("workAuth", json.loads(kit.path().read_text(encoding="utf-8"))["profile"]["value"])

    def test_memory_keeps_only_saved_answers(self):
        got = kit.save("memory", {"mem:when can you start": {"q": "When can you start?", "a": "June"},
                                  "use:when can you start": [{"company": "Mock Co"}], "settings": {"autoRun": True}}, 1)
        self.assertEqual(set(got["value"]), {"mem:when can you start", "use:when can you start"})


class EndpointTests(Base):
    def setUp(self):
        super().setUp()
        (self.tmp / "master.tex").write_text(MASTER, encoding="utf-8")
        self.out = self.tmp / "tailored"
        for name, value in (("STORE", self.store), ("DB_PATH", self.tmp / "jobs.db"), ("MASTER", self.tmp / "master.tex"),
                            ("OUT_DIR", self.out), ("LOG_PATH", self.tmp / "server.log"),
                            # No TeX in these tests.
                            ("warm_master_pdf", lambda path=None: None)):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.EXTENSION_SEEN.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        h = {"Host": f"{server.HOST}:{server.PORT}", "Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, raw

    def test_only_the_extension_reads_the_kit(self):
        kit.save("profile", {"firstName": "Testy", "lastName": "McTestface"}, 1)
        for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "moz-extension://1234-abcd"}):
            status, raw = self.request("GET", "/api/kit", headers=headers)
            self.assertEqual(status, 403, headers)
            self.assertNotIn(b"Testy", raw)
        status, raw = self.request("GET", "/api/kit", headers=EXT)
        self.assertEqual(status, 200, raw)
        got = json.loads(raw)
        self.assertEqual(got["profile"]["value"]["firstName"], "Testy")
        self.assertEqual(got["draft_backend"], "chatgpt")
        self.assertEqual(got["history"]["work"][0]["company"], rt.history(rt.parse(MASTER))["work"][0]["company"])
        self.assertIsNone(got["resume"], "not compiled yet")

    def test_the_extension_is_seen_and_may_back_up_its_answers(self):
        self.assertIsNone(server.app_payload()["extension"]["seen"])
        self.request("GET", "/api/kit", headers=EXT)
        ext = server.app_payload()["extension"]
        self.assertTrue(ext["seen"])
        self.assertEqual(ext["seen_version"], "0.3.0")
        status, _ = self.request("POST", "/api/state", {"key": "memory", "value": {"mem:x": {"q": "X?", "a": "y"}}, "at": 5}, EXT)
        self.assertEqual(status, 200)
        self.assertEqual(kit.value("memory"), {"mem:x": {"q": "X?", "a": "y"}})
        status, _ = self.request("POST", "/api/state", {"key": "memory", "value": {}, "at": 9},
                                 {"Origin": "https://evil.example", "X-Onward-Client": "autofill"})
        self.assertEqual(status, 403)

    def test_master_pdf_is_cached_by_its_contents(self):
        calls = []

        def compile_pdf(tex, out, **kw):
            calls.append(tex)
            Path(out).write_bytes(b"%PDF-1.4 master (TEST)")
            return 1
        with mock.patch.object(rt, "compile_pdf", compile_pdf):
            first = server.master_pdf(server.MASTER)
            self.assertEqual(server.master_pdf(server.MASTER), first)
            self.assertEqual(len(calls), 1)
            server.MASTER.write_text(MASTER.replace("Example", "Sample"), encoding="utf-8")
            second = server.master_pdf(server.MASTER)
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists(), "the old version's PDF is dropped")
        kit.save("profile", {"firstName": "Testy", "lastName": "McTestface"}, 1)
        status, raw = self.request("GET", "/api/kit", headers=EXT)
        self.assertEqual(json.loads(raw)["resume"]["name"], "Testy_McTestface_Resume.pdf")

    def test_drafts_use_the_apps_model_and_kit(self):
        self.store.update({"tailoring": {"draft_backend": "claude-code"}})
        kit.save("profile", {"firstName": "Testy", "school": "Example State University"}, 1)
        sent = {}
        with mock.patch.object(server, "submit_draft", lambda params: sent.update(params) or "abc123"):
            status, raw = self.request("POST", "/api/answers", {"questions": [{"id": "0", "question": "Why us?"}],
                                                                "profile": {"firstName": "Someone else"}}, EXT)
        self.assertEqual(status, 202, raw)
        self.assertEqual(sent["backend"], "claude-code")
        self.assertEqual(sent["profile"]["firstName"], "Testy", "the app's Apply kit, not what the page sent")


if __name__ == "__main__":
    unittest.main()
