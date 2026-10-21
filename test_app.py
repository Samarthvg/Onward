"""The app around the dashboard: timed searches (scheduler.py), the run lock,
start at login and the old scheduled tasks (autostart.py), the update check
(updates.py), TeX detection (texengine.py), PDF and text resumes
(resume_import.py), the setup migration, and the helper's /api/app endpoints.

Run:  python -m unittest test_app
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import app
import appinfo
import autostart
import config
import oslock
import resume_import
import resume_tex as rt
import scheduler
import texengine
import updates

ROOT = Path(__file__).resolve().parent


class TmpHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="igez-app-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = config.Store(self.home / "settings.json", self.home)
        config.use(self.store)
        self.addCleanup(config.use, None)


# --------------------------------------------------------------------------
# Scheduler

class FakeProc:
    def __init__(self, code=None):
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class SchedulerTests(TmpHome):
    def setUp(self):
        super().setUp()
        self.now = 1_000_000.0
        self.started: list[list[str]] = []
        self.proc = FakeProc()
        self.logs: list[str] = []

        def popen(cmd, **kw):
            self.started.append(cmd)
            self.env = kw["env"]
            self.proc = FakeProc()
            return self.proc

        self.sched = scheduler.Scheduler(self.store, self.logs.append, clock=lambda: self.now, popen=popen,
                                         command=lambda p: ["radar", "--status-file", str(p)])

    def finish(self, code=0, **status):
        self.sched.status_path.write_text(json.dumps({"phase": "done", **status}), encoding="utf-8")
        self.proc.code = code
        self.sched.tick()

    def setup_done(self):
        self.store.update({"setup": {"done": True}})

    def add_db_run(self, finished):
        conn = sqlite3.connect(self.store.path_for("db"))
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started REAL, finished REAL, "
                     "boards INTEGER, failed INTEGER, matched INTEGER, new INTEGER)")
        conn.execute("INSERT INTO runs (started, finished, boards, failed, matched, new) VALUES (?,?,?,?,?,?)",
                     (finished - 60, finished, 400, 2, 300, 7))
        conn.commit()
        conn.close()

    def test_waits_for_setup(self):
        self.now += 3600
        self.sched.tick()
        self.assertEqual(self.started, [])
        self.assertIsNone(self.sched.next_run())
        self.assertFalse(self.sched.status()["setup_done"])

    def test_first_run_waits_out_the_startup_delay(self):
        self.setup_done()
        self.assertEqual(self.sched.next_run(), self.now + scheduler.STARTUP_DELAY)
        self.sched.tick()
        self.assertEqual(self.started, [])
        self.now += scheduler.STARTUP_DELAY
        self.sched.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.env["JOBRADAR_HOME"], str(self.home), "the run uses this data folder")

    def test_interval_counts_from_the_last_finished_run(self):
        self.setup_done()
        self.add_db_run(self.now - 10 * 60)          # a run 10 minutes ago, from before the app started
        self.assertEqual(self.sched.next_run(), self.now + 20 * 60)
        self.store.update({"radar": {"interval_minutes": 60}})
        self.assertEqual(self.sched.next_run(), self.now + 50 * 60)

    def test_records_a_successful_run_and_schedules_the_next(self):
        self.setup_done()
        self.assertTrue(self.sched.run_now())
        self.assertFalse(self.sched.run_now(), "never two at once")
        self.sched.status_path.write_text(json.dumps({"phase": "fetching", "done": 5, "total": 40, "jobs": 12}))
        running = self.sched.status()["running"]
        self.assertEqual((running["phase"], running["done"], running["total"], running["label"]),
                         ("fetching", 5, 40, "Reading job boards"))
        self.now += 90
        self.finish(0, matched=310, new=4, failed=1, boards=40)
        st = self.sched.status()
        self.assertIsNone(st["running"])
        self.assertEqual((st["last"]["outcome"], st["last"]["matched"], st["last"]["new"]), ("ok", 310, 4))
        self.assertEqual(st["next_run"], self.now + 30 * 60)
        # Kept across restarts.
        again = scheduler.Scheduler(self.store, self.logs.append, clock=lambda: self.now)
        self.assertEqual(again.status()["last"]["matched"], 310)

    def test_a_failed_run_is_reported_and_retried_after_the_interval(self):
        self.setup_done()
        self.sched.run_now()
        self.sched.status_path.write_text(json.dumps({"phase": "error", "error": "OSError: disk full"}))
        self.proc.code = 1
        self.sched.tick()
        st = self.sched.status()
        self.assertEqual((st["last"]["outcome"], st["last"]["error"]), ("error", "OSError: disk full"))
        self.assertEqual(st["next_run"], self.now + 30 * 60)
        lines = app.status_lines(st, {"chatgpt": True, "claude_code": False}, {})
        self.assertTrue(lines[0].startswith("Last search failed"))
        self.assertIn("disk full", lines[1])
        self.assertEqual(lines[-1], "AI: ChatGPT ready")

    def test_a_run_skipped_for_another_is_tried_again_soon(self):
        self.setup_done()
        self.now += scheduler.STARTUP_DELAY
        self.sched.run_now()
        self.proc.code = scheduler.SKIPPED
        self.sched.tick()
        self.assertEqual(self.sched.status()["last"]["outcome"], "skipped")
        self.assertEqual(self.sched.next_run(), self.now + scheduler.RETRY_SKIPPED)

    def test_a_hung_run_is_stopped(self):
        self.setup_done()
        self.sched.run_now()
        proc = self.proc
        self.now += scheduler.RUN_TIMEOUT + 1
        self.sched.tick()
        self.assertTrue(proc.terminated)
        self.assertEqual(self.sched.status()["last"]["outcome"], "error")

    def test_stop_ends_the_run_in_progress(self):
        self.setup_done()
        self.sched.run_now()
        proc = self.proc
        self.sched.stop()
        self.assertTrue(proc.terminated)
        self.assertIsNone(self.sched.status()["running"])

    def test_not_automatic_never_starts_on_its_own(self):
        self.setup_done()
        self.sched.automatic = False
        self.now += 10 * 3600
        self.sched.tick()
        self.assertEqual(self.started, [])
        self.assertTrue(self.sched.run_now("dashboard"))

    def test_a_manual_run_elsewhere_counts(self):
        self.setup_done()
        self.sched.run_now()
        self.now += 60
        self.finish(0, matched=1)
        self.now += 15 * 60
        self.add_db_run(self.now)                    # someone ran radar.py by hand
        self.assertEqual(self.sched.next_run(), self.now + 30 * 60)
        self.assertEqual(self.sched.status()["last"]["matched"], 300)

    def test_status_lines(self):
        st = {"setup_done": True, "running": {"label": "Reading job boards", "done": 3, "total": 9},
              "last": None, "next_run": None}
        self.assertEqual(app.status_lines(st, {}, {}), ["Searching: 3 of 9 boards"])
        st = {"setup_done": False, "running": None, "last": None, "next_run": None}
        self.assertEqual(app.status_lines(st, {"chatgpt": False}, {"port_conflict": "x"}),
                         ["Dashboard can't open: port in use", "No search yet", "Searches start once setup is done",
                          "AI: not signed in (dashboard, Settings)"])


class RealRunTests(TmpHome):
    """The real command line: radar.py's status file and its lock."""

    def test_radar_skips_while_another_run_holds_the_lock(self):
        self.store.save(self.store.load())
        lock = oslock.FileLock(self.home / "radar.lock")
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        status = self.home / "status.json"
        proc = subprocess.run(scheduler.default_command(status), env={**os.environ, "JOBRADAR_HOME": str(self.home)},
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, scheduler.SKIPPED, proc.stderr)
        self.assertEqual(json.loads(status.read_text())["phase"], "skipped")

    def test_lock_is_released_when_its_process_ends(self):
        path = self.home / "x.lock"
        code = f"import oslock,sys; l=oslock.FileLock(r'{path}'); print(l.acquire()); sys.stdout.flush()"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "True")
        lock = oslock.FileLock(path)
        self.assertTrue(lock.acquire(), "a lock left by a finished process doesn't block")
        self.assertTrue(oslock.FileLock(path).held_elsewhere())
        lock.release()
        self.assertFalse(oslock.FileLock(path).held_elsewhere())


# --------------------------------------------------------------------------
# Start at login, old scheduled tasks

class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_macos_launch_agent(self):
        a = autostart.Autostart(platform="darwin", home=self.home)
        self.assertFalse(a.enabled())
        a.enable(["/Applications/Onward.app/Contents/MacOS/Onward"])
        import plistlib
        data = plistlib.loads(a.plist_path().read_bytes())
        self.assertEqual((data["Label"], data["RunAtLoad"]), (appinfo.BUNDLE_ID, True))
        self.assertTrue(a.enabled())
        self.assertIn("Onward.app", a.command())
        a.disable()
        self.assertFalse(a.enabled())

    def test_linux_autostart_entry(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            a = autostart.Autostart(platform="linux", home=self.home)
            a.enable(["/home/me/My Apps/Onward.AppImage"])
            self.assertEqual(a.desktop_path(), self.home / ".config" / "autostart" / "onward.desktop")
            text = a.desktop_path().read_text()
            self.assertIn("Exec='/home/me/My Apps/Onward.AppImage'", text)
            self.assertIn("Name=Onward", text)
            a.disable()
            self.assertFalse(a.enabled())

    def test_appimage_starts_the_appimage_not_its_mount(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/me/igez.AppImage"}):
            self.assertEqual(autostart.launch_command(), ["/home/me/igez.AppImage"])

    @unittest.skipUnless(sys.platform == "win32", "Windows registry")
    def test_windows_run_key(self):
        a = autostart.Autostart(name="Onward test " + str(os.getpid()))
        self.addCleanup(a.disable)
        self.assertFalse(a.enabled())
        a.enable([r"C:\Program Files\Onward\Onward.exe"])
        self.assertEqual(a.command(), r'"C:\Program Files\Onward\Onward.exe"')
        a.disable()
        self.assertFalse(a.enabled())
        a.disable()     # twice is fine

    def test_source_install_starts_app_py_without_a_console(self):
        cmd = autostart.launch_command()
        self.assertTrue(cmd[-1].endswith("app.py"))
        if sys.platform == "win32" and Path(sys.executable).with_name("pythonw.exe").exists():
            self.assertTrue(cmd[0].lower().endswith("pythonw.exe"))

    def fake_schtasks(self, code_dir: Path):
        calls = []
        present = {"JobRadar Helper": "server.py", "JobRadar": "radar.py"}

        def run(cmd, **kw):
            calls.append(cmd[1:4])
            name = cmd[3]
            if name not in present:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: The system cannot find the file specified.")
            out = ""
            if cmd[1] == "/Query":
                out = (f"<Task><Actions><Exec><Command>C:\\Python311\\pythonw.exe</Command>"
                       f"<Arguments>&quot;{code_dir / present[name]}&quot;</Arguments></Exec></Actions></Task>")
            if cmd[1] == "/Delete":
                del present[name]
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return run, calls

    @unittest.skipUnless(sys.platform == "win32", "scheduled tasks are Windows only")
    def test_legacy_tasks_for_this_data_folder_are_ended_then_deleted(self):
        (self.home / "jobs.db").write_bytes(b"")       # the old tasks ran code that kept its data in place
        run, calls = self.fake_schtasks(self.home)
        self.assertEqual(autostart.remove_legacy_tasks(self.home, run=run), ["JobRadar Helper", "JobRadar"])
        self.assertEqual(calls[:3], [["/Query", "/TN", "JobRadar Helper"], ["/End", "/TN", "JobRadar Helper"],
                                     ["/Delete", "/TN", "JobRadar Helper"]])
        calls.clear()
        self.assertEqual(autostart.remove_legacy_tasks(self.home, run=run), [], "nothing left the second time")
        self.assertEqual([c[0] for c in calls], ["/Query", "/Query"])

    @unittest.skipUnless(sys.platform == "win32", "scheduled tasks are Windows only")
    def test_legacy_tasks_for_another_data_folder_are_left_alone(self):
        other = self.home / "old-copy"
        other.mkdir()
        (other / "settings.json").write_text("{}")
        run, calls = self.fake_schtasks(other)
        self.assertEqual(autostart.remove_legacy_tasks(self.home, run=run), [])
        self.assertEqual({c[0] for c in calls}, {"/Query"})

    def test_task_script_from_xml(self):
        xml = "<Exec><Command>py.exe</Command><Arguments>&quot;C:\\Work dir\\radar.py&quot; --x</Arguments></Exec>"
        self.assertEqual(autostart.task_script(xml), Path("C:\\Work dir\\radar.py"))
        self.assertIsNone(autostart.task_script("<Exec><Command>notepad.exe</Command></Exec>"))


# --------------------------------------------------------------------------
# Updates

class UpdateTests(unittest.TestCase):
    RELEASE = {"tag_name": "v0.4.0", "html_url": "https://github.com/o/r/releases/tag/v0.4.0", "body": "Notes",
               "draft": False, "prerelease": False,
               "assets": [{"name": "onward-0.4.0-windows-setup.exe", "browser_download_url": "https://x/setup.exe",
                           "size": 5},
                          {"name": "onward-0.4.0-macos.dmg", "browser_download_url": "https://x/a.dmg", "size": 5},
                          {"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS", "size": 100}]}

    def get(self, release):
        return lambda url, *a: json.dumps(release).encode()

    def test_versions(self):
        self.assertTrue(updates.newer("v0.10.0", "0.9.9"))
        self.assertTrue(updates.newer("1.0", "0.9"))
        self.assertFalse(updates.newer("0.1.0", "0.1"))
        self.assertFalse(updates.newer("garbage", "0.1.0"))

    def test_newer_release_with_this_platforms_installer(self):
        up = updates.check("o/r", "0.3.2", "win32", get=self.get(self.RELEASE))
        self.assertEqual((up["version"], up["installer"]["url"]), ("0.4.0", "https://x/setup.exe"))
        up = updates.check("o/r", "0.3.2", "linux", get=self.get(self.RELEASE))
        self.assertIsNone(up["installer"], "no AppImage in this release: the page opens instead")
        self.assertIsNone(updates.check("o/r", "0.4.0", "win32", get=self.get(self.RELEASE)))
        self.assertIsNone(updates.check("o/r", "0.1.0", "win32", get=self.get({**self.RELEASE, "prerelease": True})))
        self.assertIsNone(updates.check("", "0.1.0", "win32", get=self.get(self.RELEASE)), "no repo set: no request")

    def test_download_checks_the_hash(self):
        body = b"installer bytes"
        good = hashlib.sha256(body).hexdigest()
        up = updates.check("o/r", "0.1.0", "win32", get=self.get(self.RELEASE))
        with tempfile.TemporaryDirectory() as d:
            def save(url, path):
                path.write_bytes(body)
                return hashlib.sha256(body).hexdigest()
            sums = f"{'0' * 64}  other.exe\n{good}  onward-0.4.0-windows-setup.exe\n".encode()
            path = updates.download(up, Path(d), get=lambda *a: sums, save=save)
            self.assertEqual(path.read_bytes(), body)
            bad = f"{'1' * 64}  onward-0.4.0-windows-setup.exe\n".encode()
            with self.assertRaises(updates.UpdateError):
                updates.download(up, Path(d), get=lambda *a: bad, save=save)
            self.assertFalse(path.exists(), "a mismatched download is removed")
            with self.assertRaises(updates.UpdateError):
                updates.download(up, Path(d), get=lambda *a: b"", save=save)

    def test_signature_check_is_off_until_a_publisher_is_set(self):
        updates.check_signature(Path("nowhere.exe"), publisher="")

    @unittest.skipUnless(sys.platform == "win32", "Authenticode")
    def test_unsigned_installer_is_refused_once_a_publisher_is_set(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"MZ not really")
            with self.assertRaises(updates.UpdateError):
                updates.check_signature(f, publisher="Some Publisher")


# --------------------------------------------------------------------------
# TeX and resumes

SAMPLE = """Jane Q. Doe
Boston, MA | jane.doe@example.com | (617) 555-0100 | linkedin.com/in/janedoe
EDUCATION
Northeastern University
Master of Science in Computer Science Sep 2023 \u2013 May 2025
EXPERIENCE
Software Engineer Intern May 2024 \u2013 Aug 2024
Acme Corp Remote
\u2022 Built a Kafka pipeline in Go that cut ingest latency by 40% across 12 services and
replaced a nightly batch job
\u2022 Wrote React & TypeScript dashboards used by 300 analysts
PROJECTS
Job Radar | Python, SQLite
\u2022 Fetches postings from 400 boards every 30 minutes
TECHNICAL SKILLS
Languages: Python, Go, C#
"""


class ResumeImportTests(TmpHome):
    def test_outline(self):
        o = resume_import.text_to_outline(SAMPLE)
        self.assertIn("# Experience", o)
        self.assertIn("## Software Engineer Intern | Acme Corp Remote | May 2024 \u2013 Aug 2024", o)
        self.assertIn("- Built a Kafka pipeline in Go that cut ingest latency by 40% across 12 services and "
                      "replaced a nightly batch job", o, "the wrapped line joins its bullet")
        self.assertIn("Languages: Python, Go, C#", o)

    def test_outline_becomes_a_tex_tailoring_can_edit(self):
        tex = resume_import.outline_to_tex(resume_import.text_to_outline(SAMPLE))
        res = rt.parse(tex)
        self.assertEqual([(e.kind, len(e.bullets)) for e in res.entries], [("experience", 2), ("project", 1)])
        self.assertEqual(res.entries[1].tech_tex, "Python, SQLite", "a project's tools are its tool list")
        self.assertEqual([r.label for r in res.skill_rows], ["Languages"])
        self.assertIn(r"40\%", tex)
        self.assertIn(r"C\#", tex)
        self.assertIn(r"React \& TypeScript", tex)
        self.assertIn(r"\href{mailto:jane.doe@example.com}", tex)

    def test_no_bullets_is_explained(self):
        with self.assertRaises(ValueError):
            resume_import.outline_to_tex("Jane\n# Experience\n## Intern\n")

    def test_word_files_are_the_documented_next_reader(self):
        self.assertIsNone(resume_import.READERS["docx"])
        self.assertEqual(resume_import.READERS["pdf"], "browser")

    @unittest.skipUnless(texengine.pick("pdflatex"), "needs a TeX engine")
    def test_converted_resume_compiles(self):
        tex = resume_import.outline_to_tex(resume_import.text_to_outline(SAMPLE))
        self.assertEqual(rt.compile_pdf(tex), 1)


class TexEngineTests(TmpHome):
    def test_prefers_installed_tex_then_tectonic(self):
        with mock.patch.object(texengine, "find_tex", return_value=None), \
                mock.patch.object(texengine.shutil, "which", return_value=None):
            self.assertIsNone(texengine.pick("xelatex"))
            self.assertIsNone(texengine.status()["active"])
            own = texengine.tools_dir() / texengine._exe("tectonic")
            own.parent.mkdir(parents=True)
            own.write_bytes(b"")
            self.assertEqual(texengine.pick("xelatex"), ("tectonic", str(own)))
        with mock.patch.object(texengine, "find_tex", return_value="/usr/bin/xelatex"):
            self.assertEqual(texengine.pick("xelatex"), ("tex", "/usr/bin/xelatex"))

    def test_no_engine_says_how_to_get_one(self):
        with mock.patch.object(texengine, "pick", return_value=None):
            with self.assertRaisesRegex(rt.CompileError, "Tectonic"):
                rt.compile_pdf("\\documentclass{article}\\begin{document}x\\end{document}")

    def test_every_platform_has_a_pinned_download(self):
        for (plat, machine), (name, sha, size) in texengine.TECTONIC_ASSETS.items():
            self.assertIn(texengine.TECTONIC_VERSION, name)
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertGreater(size, 1_000_000)
        self.assertIn(("win32", "amd64"), texengine.TECTONIC_ASSETS)

    def test_download_refuses_a_file_that_doesnt_match(self):
        class Resp:
            def __init__(self):
                self.chunks = [b"not tectonic"]

            def read(self, n):
                return self.chunks.pop() if self.chunks else b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        d = texengine.Download()
        with mock.patch.object(texengine, "tectonic_asset", return_value=("t.zip", "0" * 64, 12)):
            self.assertTrue(d.start(opener=lambda *a, **k: Resp()))
            d._thread.join(10)
        snap = d.snapshot()
        self.assertEqual(snap["state"], "error")
        self.assertIn("checksum", snap["error"])
        self.assertFalse((texengine.tools_dir() / texengine._exe("tectonic")).exists())

    @unittest.skipUnless(texengine.tectonic_path(), "Tectonic isn't installed")
    def test_tectonic_compiles_a_pdflatex_template(self):
        jake = textwrap.dedent(r"""
            \documentclass[letterpaper,11pt]{article}
            \usepackage[hidelinks]{hyperref}
            \input{glyphtounicode}
            \pdfgentounicode=1
            \begin{document}
            \section{Experience}
            \begin{itemize}\item Built things\end{itemize}
            \end{document}""")
        with mock.patch.object(texengine, "find_tex", return_value=None):
            self.assertEqual(rt.compile_pdf(jake), 1)


# --------------------------------------------------------------------------
# Settings: setup steps

class SetupMigrationTests(TmpHome):
    def test_new_install_starts_the_setup_steps(self):
        s = self.store.load()
        self.assertEqual(s["setup"], {"done": False, "step": "welcome"})
        self.assertEqual(s["radar"]["interval_minutes"], 30)
        self.assertTrue(s["app"]["check_updates"])

    def test_existing_settings_skip_them(self):
        (self.home / "settings.json").write_text(json.dumps({"version": 2, "radar": {"dashboard_days": 5}}))
        self.assertTrue(self.store.load()["setup"]["done"])

    def test_existing_jobs_skip_them(self):
        (self.home / "jobs.db").write_bytes(b"")
        self.assertTrue(self.store.load()["setup"]["done"], "an install from before settings.json")


# --------------------------------------------------------------------------
# The helper's /api/app, on a spare port with its own data folder

DRIVER = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["CODE"])
import server, scheduler
fake = os.path.join(os.environ["JOBRADAR_HOME"], "fake_radar.py")
open(fake, "w").write('''
import json, sys, time
p = sys.argv[sys.argv.index("--status-file") + 1]
for i in range(3):
    json.dump({"phase": "fetching", "done": i, "total": 3, "jobs": i * 2}, open(p, "w")); time.sleep(0.3)
json.dump({"phase": "done", "matched": 6, "new": 6, "failed": 0, "boards": 3}, open(p, "w"))
''')
server.SCHEDULER.command = lambda status: [sys.executable, fake, "--status-file", str(status)]
server.prepare()
httpd = server.bind()
server.start_background()
print("ready", flush=True)
httpd.serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="igez-helper-")
        cls.home = Path(cls.tmp.name)
        cls.port = free_port()
        env = {**os.environ, "JOBRADAR_HOME": str(cls.home), "JOBRADAR_PORT": str(cls.port), "CODE": str(ROOT)}
        cls.proc = subprocess.Popen([sys.executable, "-c", DRIVER], env=env, cwd=cls.home, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        line = cls.proc.stdout.readline()
        if "ready" not in line:
            cls.proc.kill()
            raise RuntimeError("helper didn't start: " + line + cls.proc.stderr.read())

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait(10)
        for f in (cls.proc.stdout, cls.proc.stderr):
            f.close()
        time.sleep(0.5)
        cls.tmp.cleanup()

    def call(self, path, body=None, origin=True):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_1_new_install_serves_the_setup_page(self):
        settings = json.loads((self.home / "settings.json").read_text())
        self.assertFalse(settings["setup"]["done"], "written before jobs.db existed, so it stays a new install")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=30) as r:
            page = r.read().decode()
        self.assertIn('"setup":{"done":false', page)
        code, data = self.call("/api/app")
        self.assertEqual(code, 200)
        self.assertEqual((data["name"], data["setup"]["done"], data["schedule"]["automatic"]),
                         ("Onward", False, False))
        self.assertIn("formats", data["tex"])

    def test_2_first_search_with_progress(self):
        self.assertEqual(self.call("/api/app/run", {}, origin=False)[0], 403, "only the dashboard may start one")
        code, data = self.call("/api/app/run", {})
        self.assertEqual(code, 202)
        self.assertEqual(self.call("/api/app/run", {})[0], 409)
        seen = set()
        for _ in range(100):
            st = self.call("/api/app")[1]["schedule"]
            if st["running"]:
                seen.add(st["running"].get("phase"))
            elif st["last"]:
                break
            time.sleep(0.1)
        self.assertIn("fetching", seen)
        self.assertEqual((st["last"]["outcome"], st["last"]["matched"]), ("ok", 6))

    def test_3_outline_and_master_from_a_pdf_resume(self):
        code, data = self.call("/api/master/outline", {"text": SAMPLE})
        self.assertEqual(code, 200)
        self.assertIn("# Experience", data["outline"])
        if not texengine.pick("pdflatex"):
            self.skipTest("needs a TeX engine")
        code, data = self.call("/api/master", {"outline": data["outline"], "name": "resume.pdf"})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["pages"], 1)
        self.assertTrue((self.home / "resume" / "master.tex").exists())

    def test_4_setup_progress_saves_without_rebuilding(self):
        code, data = self.call("/api/settings", {"settings": {"setup": {"step": "role"}}})
        self.assertEqual(code, 200)
        self.assertEqual(data["settings"]["setup"]["step"], "role")
        code, data = self.call("/api/settings", {"settings": {"setup": {"step": "nowhere"}}})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
