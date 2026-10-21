"""Settings store (config.py): defaults, validation, migrations, data folder.

Run:  python -m unittest test_settings
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import match
import prep
import radar


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jr-settings-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "settings.json"

    def store(self) -> config.Store:
        return config.Store(self.path, self.dir)

    def write(self, data) -> None:
        self.path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


class DefaultsTests(Tmp):
    """Defaults are how Job Radar behaved before settings existed."""

    def test_defaults_match_the_old_constants(self):
        d = config.defaults()
        self.assertEqual(d["version"], config.VERSION)
        self.assertEqual((d["radar"]["dashboard_days"], d["radar"]["stale_after_days"], d["radar"]["purge_after_days"],
                          d["radar"]["workers"], d["radar"]["timeout"]), (7, 2, 45, 24, 25))
        self.assertEqual(d["paths"], {"db": "jobs.db", "resume_dir": "resume", "data_dir": "data",
                                      "dashboard": "dashboard.html", "logs_dir": ""})
        self.assertEqual(d["queue"], {
            "enabled": True, "threshold": 60, "daily_cap": 25, "backend": "chatgpt", "skip_coverage": 85,
            "needs_sponsorship": True, "locations": [], "remote_ok": True, "lookback_hours": 48,
            "draft_answers": True, "notify_desktop": True, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
            "quiet_start": "22:00", "quiet_end": "08:00"})
        self.assertEqual(d["scoring"], {"experience_weight": 0.4, "prior_keywords": 3, "prior_credit": 0.5,
                                        "years_penalty": 0.35, "min_experience_fit": 0.15,
                                        "unstated_years_fit": 0.85, "unknown_keyword_weight": 0.8})
        self.assertEqual(d["tailoring"]["backend"], "chatgpt")
        self.assertEqual(d["tailoring"]["ollama_model"], "qwen3:8b")
        (p,) = d["profiles"]
        self.assertEqual((p["id"], p["roles"], p["seniority"], p["countries"], p["visa"]["status"]),
                         ("default", ["software"], ["entry"], ["US"], "opt"))
        self.assertEqual(d["active_profile"], "default")
        self.assertTrue(d["radar"]["simplify_feed"])

    def test_missing_file_gives_defaults_without_writing(self):
        s = self.store()
        self.assertEqual(s.load(), config.defaults())
        self.assertEqual(s.warnings, [])
        self.assertFalse(self.path.exists())

    def test_radar_configure_with_defaults_changes_nothing(self):
        before = (radar.DASHBOARD_DAYS, radar.STALE_AFTER_DAYS, radar.PURGE_AFTER_DAYS, radar.WORKERS, radar.TIMEOUT)
        saved = {k: getattr(radar, k) for k in ("DB_PATH", "DASHBOARD_PATH", "LOG_PATH", "H1B_DIR", "SEARCH", "SETTINGS",
                                                "DASHBOARD_DAYS", "ORACLE_DATE_FACET")}
        self.addCleanup(lambda: [setattr(radar, k, v) for k, v in saved.items()])
        radar.configure(config.Store(radar.ROOT / "no-such-settings.json", radar.ROOT))
        self.assertEqual((radar.DASHBOARD_DAYS, radar.STALE_AFTER_DAYS, radar.PURGE_AFTER_DAYS, radar.WORKERS,
                          radar.TIMEOUT), before)
        self.assertEqual(radar.DB_PATH, radar.ROOT / "jobs.db")
        self.assertEqual(radar.DASHBOARD_PATH, radar.ROOT / "dashboard.html")
        self.assertEqual(radar.LOG_PATH, radar.ROOT / "radar.log")
        self.assertEqual(radar.H1B_DIR, radar.ROOT / "data")
        self.assertEqual(radar.ORACLE_DATE_FACET, 7)

    def test_queue_defaults_are_prep_defaults(self):
        self.assertEqual(prep.clean_settings({}), config.defaults()["queue"])
        self.assertEqual(match.WEIGHTS, config.defaults()["scoring"])


class ValidationTests(Tmp):
    def test_bad_values_fall_back_and_are_reported(self):
        self.write({"version": 1, "radar": {"dashboard_days": "lots", "workers": 999, "simplify_feed": "yes"},
                    "queue": {"threshold": -5, "backend": "gpt-9", "quiet_start": "25:00"},
                    "tailoring": {"ollama_url": "not a url"}, "nonsense": 1})
        s = self.store().load()
        self.assertEqual(s["radar"]["dashboard_days"], 7)            # wrong type: default
        self.assertEqual(s["radar"]["workers"], 64)                  # out of range: clamped
        self.assertTrue(s["radar"]["simplify_feed"])
        self.assertEqual(s["queue"]["threshold"], 0)
        self.assertEqual(s["queue"]["backend"], "chatgpt")
        self.assertEqual(s["queue"]["quiet_start"], "22:00")
        self.assertEqual(s["tailoring"]["ollama_url"], "http://127.0.0.1:11434")
        self.assertNotIn("nonsense", s)
        st = self.store()
        st.load()
        for key in ("radar.dashboard_days", "radar.simplify_feed", "queue.backend", "queue.quiet_start", "tailoring.ollama_url"):
            self.assertTrue(any(w.startswith(key) for w in st.warnings), (key, st.warnings))

    def test_unreadable_file_uses_defaults(self):
        self.write("{ not json")
        st = self.store()
        self.assertEqual(st.load()["queue"], config.defaults()["queue"])
        self.assertTrue(st.warnings)

    def test_lists_from_text_and_dedupe(self):
        st = self.store()
        st.update({"queue": {"locations": "Boston, , Seattle, Boston"}})
        self.assertEqual(st.load()["queue"]["locations"], ["Boston", "Seattle"])

    def test_strict_update_rejects_and_keeps_the_file(self):
        st = self.store()
        st.update({"queue": {"threshold": 70}})
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(config.SettingsError) as cm:
            st.update({"queue": {"backend": "nope"}, "profiles": [{"id": "Bad Id!"}]})
        self.assertTrue(any("queue.backend" in e for e in cm.exception.errors))
        self.assertTrue(any("profiles[0].id" in e for e in cm.exception.errors))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_profiles_checked_together(self):
        st = self.store()
        p = config.PROFILE.default
        with self.assertRaises(config.SettingsError):
            st.update({"profiles": [p, {**p, "name": "Other"}]})        # duplicate ids
        with self.assertRaises(config.SettingsError):
            st.update({"profiles": [{**p, "roles": ["astronaut"]}]})   # unknown role family
        st.update({"role_families": [{"id": "firmware", "label": "Firmware", "include": ["firmware"], "exclude": []}],
                   "profiles": [{**p, "id": "fw", "name": "Firmware", "roles": ["firmware"]}]})
        s = st.load()
        self.assertEqual([x["id"] for x in s["profiles"]], ["fw"])
        self.assertEqual(s["active_profile"], "fw", "active profile follows when its profile is gone")
        s2, errors = config.validate({"profiles": []})
        self.assertEqual([x["id"] for x in s2["profiles"]], ["default"], "never zero profiles")
        bare, errors = config.validate({"profiles": [{"id": "x", "name": "", "roles": [], "seniority": [], "workplace": []}]})
        (x,) = bare["profiles"]
        self.assertEqual((x["roles"], x["seniority"], x["name"]), (["software"], ["entry"], "x"))
        self.assertEqual(len(errors), 3)

    def test_write_is_atomic_and_readable(self):
        st = self.store()
        st.update({"ui": {"advanced": True}})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["ui"], {"advanced": True})
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_external_change_is_picked_up(self):
        st = self.store()
        st.update({"queue": {"threshold": 70}})
        self.assertEqual(st.section("queue")["threshold"], 70)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["queue"]["threshold"] = 33
        data["queue"]["daily_cap"] = 3      # another process (the helper) saved; size changes too
        self.path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        self.assertEqual(st.section("queue")["threshold"], 33)


class MigrationTests(Tmp):
    def test_v0_install_imports_the_old_queue_settings(self):
        (self.dir / "resume").mkdir()
        (self.dir / "resume" / "prep.json").write_text(json.dumps(
            {"threshold": 72, "daily_cap": 9, "locations": ["Boston"], "unknown": 1}), encoding="utf-8")
        st = self.store()
        q = st.load()["queue"]
        self.assertEqual((q["threshold"], q["daily_cap"], q["locations"]), (72, 9, ["Boston"]))
        self.assertFalse(self.path.exists(), "no file is created just by reading")

    def test_versionless_file_is_migrated_once(self):
        (self.dir / "resume").mkdir()
        (self.dir / "resume" / "prep.json").write_text(json.dumps({"threshold": 72}), encoding="utf-8")
        self.write({"radar": {"dashboard_days": 3}})
        s = self.store().load()
        self.assertEqual((s["version"], s["radar"]["dashboard_days"], s["queue"]["threshold"]), (config.VERSION, 3, 72))
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["version"], config.VERSION)
        self.assertEqual(on_disk["queue"]["threshold"], 72)

    def test_migrations_run_in_order(self):
        seen = []

        def v2(data, home):
            seen.append(data["version"])
            data["radar"] = {**data.get("radar", {}), "dashboard_days": data.get("radar", {}).get("dashboard_days", 7) * 2}
            return data
        self.write({"version": 1, "radar": {"dashboard_days": 5}})
        with mock.patch.object(config, "VERSION", 2), mock.patch.dict(config.MIGRATIONS, {2: v2}):
            s = self.store().load()
        self.assertEqual(seen, [1])
        self.assertEqual((s["version"], s["radar"]["dashboard_days"]), (2, 10))

    def test_newer_file_is_read_only(self):
        self.write({"version": config.VERSION + 5, "radar": {"dashboard_days": 3}, "future_field": True})
        st = self.store()
        s = st.load()
        self.assertEqual(s["radar"]["dashboard_days"], 3)
        self.assertTrue(any("newer" in w for w in st.warnings))
        with self.assertRaises(config.SettingsError):
            st.update({"radar": {"dashboard_days": 4}})
        self.assertIn("future_field", json.loads(self.path.read_text(encoding="utf-8")))


class PathTests(Tmp):
    def test_paths_resolve_inside_the_data_folder(self):
        st = self.store()
        self.assertEqual(st.path_for("db"), self.dir / "jobs.db")
        self.assertEqual(st.path_for("logs_dir"), self.dir)
        other = self.dir / "elsewhere" / "radar.db"
        st.update({"paths": {"db": str(other), "resume_dir": "cv"}})
        self.assertEqual(st.path_for("db"), other)
        self.assertEqual(st.path_for("resume_dir"), self.dir / "cv")
        self.assertEqual(st.resume_for(), self.dir / "cv" / "master.tex")

    def test_profile_resume(self):
        st = self.store()
        st.update({"profiles": [{**config.PROFILE.default, "resume": "master-data.tex"}]})
        self.assertEqual(st.resume_for("default"), self.dir / "resume" / "master-data.tex")
        with self.assertRaises(config.SettingsError):
            st.update({"profiles": [{**config.PROFILE.default, "resume": "../../etc/passwd"}]})

    def test_data_folder_choice(self):
        with mock.patch.dict(os.environ, {"JOBRADAR_HOME": str(self.dir / "home")}):
            self.assertEqual(config.default_home(), self.dir / "home")
        env = {k: v for k, v in os.environ.items() if k != "JOBRADAR_HOME"}
        code = self.dir / "code"
        code.mkdir()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(config, "CODE_DIR", code):
            self.assertEqual(config.default_home(), config.platform_data_dir(), "a fresh install uses the user folder")
            (code / "jobs.db").write_bytes(b"")
            self.assertEqual(config.default_home(), code, "an existing install keeps its data where it is")

    def test_platform_folders(self):
        with mock.patch.object(config.sys, "platform", "win32"), mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.dir)}):
            self.assertEqual(config.platform_data_dir(), self.dir / "JobRadar")
        with mock.patch.object(config.sys, "platform", "darwin"):
            self.assertEqual(config.platform_data_dir().parts[-3:], ("Library", "Application Support", "JobRadar"))
        with mock.patch.object(config.sys, "platform", "linux"), mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.dir)}):
            self.assertEqual(config.platform_data_dir(), self.dir / "jobradar")


class QueueStoreTests(Tmp):
    def test_queue_settings_live_in_the_store(self):
        st = self.store()
        q = prep.Preparer(lambda: None, self.dir / "resume", self.dir / "out", log=lambda m: None, store=st)
        s = q.save_settings({"threshold": 75, "daily_cap": 9999, "locations": "NYC, Boston"})
        self.assertEqual((s["threshold"], s["daily_cap"], s["locations"]), (75, 200, ["NYC", "Boston"]))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["queue"]["threshold"], 75)
        self.assertEqual(q.settings(), s)


if __name__ == "__main__":
    unittest.main()
