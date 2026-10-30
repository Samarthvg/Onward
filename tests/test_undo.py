"""Undo for changes made from the dashboard (undo.py and POST /api/undo).

Each change answers with an undo id; undoing puts back exactly what was
there before, once.

Run:  python -m unittest tests.test_undo
"""

import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import ai
import server
from tests import test_ai
import undo
from tests.test_prep import Base

KEY = test_ai.KEY


class UndoTests(Base):
    """Through the real HTTP handler, on a spare port, in a temp data folder."""

    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = httpd.server_address[1]
        self.keyring = test_ai.FakeKeyring()
        ai.use_keyring(self.keyring)
        self.addCleanup(ai.use_keyring, ai._KEYRING)
        for p in (mock.patch.object(server, "PORT", self.port), mock.patch.object(server, "PREP", self.prep),
                  mock.patch.object(server, "ORIGINS", {f"http://127.0.0.1:{self.port}"}),
                  mock.patch.object(server, "RESUME_DIR", self.resume_dir),
                  mock.patch.object(server, "OUT_DIR", self.resume_dir / "tailored"),
                  mock.patch.object(server, "MASTER", self.resume_dir / "master.tex"),
                  mock.patch.object(server, "LOG_PATH", self.tmp / "server.log"),
                  mock.patch.object(server, "schedule_render", lambda: None),
                  mock.patch.object(ai, "verify_key", lambda provider, key: None),
                  mock.patch.dict("os.environ", {k: "" for k in ai.ENV_KEYS.values()})):
            p.start()
            self.addCleanup(p.stop)
        self.httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.keys = [r[0] for r in sqlite3.connect(self.db_path).execute("SELECT key FROM jobs ORDER BY key")]

    def call(self, path, body=None):
        h = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def undo(self, uid):
        return self.call("/api/undo", {"id": uid})

    def prep_row(self, key):
        return self.prep.snapshot([key])[key]

    # ------------------------------------------------------------ settings

    def test_settings_save_is_undone_and_only_once(self):
        before = self.store.load()
        status, r = self.call("/api/settings", {"settings": {"tailoring": {"strictness": "rewrite"}, "ui": {"advanced": True}}})
        self.assertEqual(status, 200, r)
        self.assertTrue(r["undo"])
        self.assertEqual(self.store.load()["tailoring"]["strictness"], "rewrite")
        status, u = self.undo(r["undo"])
        self.assertEqual(status, 200, u)
        after = self.store.load()
        self.assertEqual((after["tailoring"], after["ui"]), (before["tailoring"], before["ui"]))
        self.assertEqual(u["settings"]["settings"]["ui"], before["ui"], "the answer carries the restored settings")
        status, again = self.undo(r["undo"])
        self.assertEqual(status, 410, "a change is undone once")
        self.assertIn("can't be undone", again["error"])

    def test_undo_keeps_later_changes_to_other_sections(self):
        _, r = self.call("/api/settings", {"settings": {"tailoring": {"strictness": "rewrite"}}})
        self.call("/api/settings", {"settings": {"voice": {"tone": "formal"}}})
        self.undo(r["undo"])
        s = self.store.load()
        self.assertEqual(s["tailoring"]["strictness"], "reword")
        self.assertEqual(s["voice"]["tone"], "formal", "only what that save changed goes back")

    def test_nothing_changed_means_nothing_to_undo(self):
        cur = self.store.load()["tailoring"]["strictness"]
        _, r = self.call("/api/settings", {"settings": {"tailoring": {"strictness": cur}}})
        self.assertIsNone(r["undo"])
        _, r = self.call("/api/settings", {"settings": {"setup": {"step": "ai"}}})
        self.assertIsNone(r["undo"], "moving through the setup steps isn't a change to undo")

    def test_unknown_id(self):
        status, r = self.undo("nope")
        self.assertEqual(status, 410)

    def test_company_block_is_undone(self):
        status, r = self.call("/api/companies/block", {"name": "Acme"})
        self.assertEqual(status, 200, r)
        self.assertIn("Acme", r["blocked"])
        self.undo(r["undo"])
        self.assertNotIn("Acme", self.store.load()["companies"]["blocked"])

    def test_queue_settings_are_undone(self):
        before = self.store.load()["queue"]
        _, r = self.call("/api/prep/settings", {"threshold": 77, "daily_cap": 3})
        self.assertEqual(r["threshold"], 77)
        self.undo(r["undo"])
        self.assertEqual(self.store.load()["queue"], before)

    # ------------------------------------------------------------ files

    def test_about_me_is_undone(self):
        about = self.resume_dir / "about_me.txt"
        self.assertFalse(about.exists())
        _, r = self.call("/api/about", {"text": "I play guitar."})
        self.call("/api/about", {"text": "I play guitar."})          # same text again: nothing new
        _, r2 = self.call("/api/about", {"text": "I build synths."})
        self.undo(r2["undo"])
        self.assertEqual(about.read_text(encoding="utf-8").strip(), "I play guitar.")
        self.undo(r["undo"])
        self.assertFalse(about.exists(), "back to no notes at all")

    def test_files_outside_the_resume_folder_are_refused(self):
        conn = server.db()
        uid = undo.record(conn, "files", "x", {"files": {str(self.tmp / "settings.json"): None}})
        conn.close()
        status, r = self.undo(uid)
        self.assertEqual(status, 409, r)
        self.assertTrue((self.tmp / "settings.json").exists())

    # ------------------------------------------------------------ queue rows and marks

    def test_marks_are_undone_exactly(self):
        key = self.keys[0]
        conn = server.db()
        with conn:
            conn.execute("CREATE TABLE applications (job_key TEXT UNIQUE, status TEXT)")
        conn.close()
        self.assertIsNone(self.prep_row(key))
        _, r = self.call("/api/marks", {"marks": {key: "applied"}})
        self.assertEqual(self.prep_row(key)["state"], "applied")
        self.undo(r["undo"])
        self.assertIsNone(self.prep_row(key), "no row before, none after")
        apps = sqlite3.connect(self.db_path).execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        self.assertEqual(apps, 0, "the applications row the mark added is gone too")

    def test_mark_on_a_prepared_job_goes_back_to_its_old_state(self):
        key = self.keys[1]
        self.prep.mark_many({key: "dismissed"})
        conn = server.db()
        with conn:
            conn.execute("UPDATE prep SET state = 'queued', decision = 'tailor', reason = 'r', score = 81 WHERE job_key = ?", (key,))
        conn.close()
        before = self.prep_row(key)
        _, r = self.call("/api/marks", {"marks": {key: "applied"}})
        self.undo(r["undo"])
        self.assertEqual(self.prep_row(key), before)

    def test_bulk_mark_sync_records_no_undo(self):
        _, r = self.call("/api/marks", {"marks": {k: "dismissed" for k in self.keys[:1]} | {f"x{i}": "dismissed" for i in range(30)}})
        self.assertIsNone(r["undo"])

    def ready_row(self, key, **kw):
        self.prep.mark_many({key: "dismissed"})
        cols = {"state": "ready", "decision": "tailor", "tailored_id": "t1", "choice": "tailored", "reviewed": 0, "score": 80, **kw}
        conn = server.db()
        with conn:
            conn.execute(f"UPDATE prep SET {', '.join(k + ' = ?' for k in cols)} WHERE job_key = ?", (*cols.values(), key))
        conn.close()
        return self.prep_row(key)

    def test_choice_and_retailor_are_undone(self):
        key = self.keys[2]
        before = self.ready_row(key)
        _, r = self.call("/api/queue/choice", {"key": key, "choice": "master"})
        self.assertEqual(self.prep_row(key)["choice"], "master")
        self.undo(r["undo"])
        self.assertEqual(self.prep_row(key), before)
        conn = server.db()
        with conn:
            conn.execute("INSERT INTO tailored (id, job_key, created, backend, model, pdf, tex, result) VALUES "
                         "('v9', ?, ?, 'manual', 'm', 'a.pdf', 'a.tex', '{}')", (key, time.time()))
        conn.close()
        _, r = self.call("/api/tailored/use", {"key": key, "id": "v9"})
        self.assertEqual((self.prep_row(key)["tailored_id"], self.prep_row(key)["own"]), ("v9", 1))
        self.undo(r["undo"])
        self.assertEqual(self.prep_row(key), before)
        st, _ = self.call("/api/tailored/use", {"key": self.keys[0], "id": "v9"})
        self.assertEqual(st, 404, "another job's version")

    def test_undo_while_preparing_wins_over_the_late_result(self):
        """The preparer's final write only lands while the row is still working."""
        key = self.keys[3]
        before = self.ready_row(key)
        _, r = self.call("/api/queue/retry", {"key": key})
        conn = server.db()
        self.prep._update(conn, key, state="working")
        self.undo(r["undo"])
        self.prep._update(conn, key, when="working", state="ready", tailored_id="t2", choice="tailored")
        conn.close()
        self.assertEqual(self.prep_row(key), before)

    def test_new_tailored_version_is_taken_out(self):
        conn = server.db()
        with conn:
            conn.execute("INSERT INTO tailored (id, job_key, created, backend, model, pdf, tex, result) VALUES "
                         "('v2', ?, ?, 'manual', 'm', 'a.pdf', 'a.tex', '{}')", (self.keys[0], time.time()))
        uid = undo.record(conn, "tailored", "the new resume version", {"id": "v2"})
        conn.close()
        self.undo(uid)
        _, hist = self.call("/api/history?key=" + urllib.request.quote(self.keys[0]))
        self.assertEqual(hist, [])

    # ------------------------------------------------------------ keys

    def test_key_replacement_and_removal_are_undone(self):
        ai.save_key("openai", KEY)
        new = KEY.replace("SECRET", "NEWONE")
        status, r = self.call("/api/keys", {"provider": "openai", "key": new})
        self.assertEqual(status, 200, r)
        self.assertEqual(ai.get_key("openai"), new)
        self.undo(r["undo"])
        self.assertEqual(ai.get_key("openai"), KEY, "the earlier key is back")
        _, r = self.call("/api/keys", {"provider": "openai", "key": ""})
        self.assertIsNone(ai.get_key("openai"))
        status, u = self.undo(r["undo"])
        self.assertEqual(status, 200, u)
        self.assertEqual(ai.get_key("openai"), KEY)
        rows = sqlite3.connect(self.db_path).execute("SELECT payload FROM undo").fetchall()
        self.assertFalse(any(KEY in p or new in p for (p,) in rows), "keys are never written to the undo table")

    def test_key_undo_after_a_restart_says_what_to_do(self):
        ai.save_key("openai", KEY)
        _, r = self.call("/api/keys", {"provider": "openai", "key": ""})
        undo.SECRETS.clear()                                   # what a restart forgets
        status, u = self.undo(r["undo"])
        self.assertEqual(status, 409)
        self.assertIn("Paste it again", u["error"])


class LogTests(unittest.TestCase):
    def test_pruned_by_count(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(undo.SCHEMA)
        with mock.patch.object(undo, "KEEP_ROWS", 5):
            ids = [undo.record(conn, "settings", "x", {"before": {}}) for _ in range(8)]
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM undo").fetchone()[0], 5)
        with self.assertRaises(undo.Gone):
            undo.take(conn, ids[0])
        self.assertEqual(undo.take(conn, ids[-1])[0], "settings")


if __name__ == "__main__":
    unittest.main()
