"""The ChatGPT and Claude logins' model and thinking effort: the catalog Codex lists,
checking a choice against it, and the flags a run passes to each CLI.

No test runs Codex or Claude: subprocess.run is a fake throughout.

Run:  python -m unittest tests.test_login_models
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import config
import tailor


def level(e):
    return {"effort": e, "description": ""}


CATALOG = json.dumps({"models": [
    {"slug": "gpt-luna", "display_name": "GPT Luna", "visibility": "list", "priority": 3,
     "default_reasoning_level": "medium", "supported_reasoning_levels": [level("low"), level("medium")]},
    {"slug": "gpt-sol", "display_name": "GPT Sol", "visibility": "list", "priority": 1,
     "default_reasoning_level": "low",
     "supported_reasoning_levels": [level(e) for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"slug": "gpt-internal", "display_name": "Hidden", "visibility": "hide", "priority": 2,
     "supported_reasoning_levels": [level("low")]},
    {"slug": "bad slug; rm", "display_name": "Odd", "visibility": "list", "priority": 4},
]})


class Fake:
    """subprocess.run: answers `codex debug models` with CATALOG and an
    exec run with a written answer and Codex's usual header on stderr."""

    def __init__(self, catalog=CATALOG, returncode=0):
        self.catalog, self.returncode, self.runs = catalog, returncode, []

    def __call__(self, cmd, **kw):
        if cmd[1:3] == ["debug", "models"]:
            return NS(returncode=self.returncode, stdout=self.catalog, stderr="")
        self.runs.append(cmd)
        model = cmd[cmd.index("-m") + 1] if "-m" in cmd else "gpt-sol"
        effort = next((c.split("=", 1)[1].strip('"') for c in cmd if c.startswith("model_reasoning_effort=")), "low")
        Path(cmd[cmd.index("-o") + 1]).write_text('{"ok": true}', encoding="utf-8")
        return NS(returncode=0, stdout="", stderr=f"OpenAI Codex\nmodel: {model}\nreasoning effort: {effort}\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-gpt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = config.Store(self.tmp / "settings.json", self.tmp)
        config.use(self.store)
        self.addCleanup(config.use, None)
        self.fake = Fake()
        for target, value in (("subprocess.run", self.fake), ("shutil.which", lambda name: "codex"),
                              ("tailor.runner_dir", lambda: self.tmp / "runner"),
                              ("tailor._catalog", (0.0, []))):
            p = mock.patch(target, value) if target != "tailor._catalog" else mock.patch.object(tailor, "_catalog", value)
            p.start()
            self.addCleanup(p.stop)

    def choose(self, model, effort):
        self.store.update({"chatgpt": {"model": model, "effort": effort}})


class CatalogTests(Base):
    def test_listed_models_in_codex_order(self):
        got = tailor.codex_models()
        self.assertEqual([m["id"] for m in got], ["gpt-sol", "gpt-luna"], "hidden and malformed ones are left out")
        self.assertEqual(got[0]["efforts"], ["low", "medium", "high", "xhigh", "max"], "no ultra")
        self.assertEqual((got[1]["label"], got[1]["default_effort"]), ("GPT Luna", "medium"))

    def test_kept_for_a_while(self):
        tailor.codex_models()
        self.fake.catalog = "{}"
        self.assertEqual(len(tailor.codex_models()), 2)

    def test_unreadable_catalog_is_empty(self):
        for fake in (Fake(returncode=1), Fake(catalog="not json"), Fake(catalog="[]")):
            with mock.patch("subprocess.run", fake), mock.patch.object(tailor, "_catalog", (0.0, [])):
                self.assertEqual(tailor.codex_models(), [])


class ChoiceTests(Base):
    def test_nothing_chosen_leaves_it_to_codex(self):
        self.assertEqual(tailor.codex_choice(), ("", ""))

    def test_a_listed_model_and_effort(self):
        self.choose("gpt-sol", "xhigh")
        self.assertEqual(tailor.codex_choice(), ("gpt-sol", "xhigh"))

    def test_a_model_codex_dropped(self):
        self.choose("gpt-old", "")
        with self.assertRaisesRegex(tailor.BackendError, "doesn't offer gpt-old"):
            tailor.codex_choice()

    def test_an_effort_the_model_doesnt_take(self):
        self.choose("gpt-luna", "max")
        with self.assertRaisesRegex(tailor.BackendError, "GPT Luna doesn't take max"):
            tailor.codex_choice()
        with self.assertRaisesRegex(tailor.BackendError, "Unknown thinking effort"):
            tailor.codex_choice("gpt-sol", "ultra")

    def test_without_a_catalog_the_choice_passes_through(self):
        self.choose("gpt-new", "high")
        with mock.patch("subprocess.run", Fake(returncode=1)):
            self.assertEqual(tailor.codex_choice(), ("gpt-new", "high"))


class RunTests(Base):
    def test_default_run_passes_no_model(self):
        raw, model = tailor._codex_json("prompt", {"type": "object"})
        cmd = self.fake.runs[-1]
        self.assertNotIn("-m", cmd)
        self.assertFalse(any(c.startswith("model_reasoning_effort") for c in cmd))
        self.assertEqual(cmd[-1], "-", "the prompt still comes on stdin")
        self.assertEqual((raw, model), ({"ok": True}, "chatgpt (gpt-sol, low)"), "what Codex says it ran")

    def test_chosen_model_and_effort_reach_codex(self):
        self.choose("gpt-luna", "low")
        _, model = tailor._codex_json("prompt", {"type": "object"})
        cmd = self.fake.runs[-1]
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-luna")
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="low"')
        self.assertEqual(cmd[-1], "-")
        self.assertEqual(model, "chatgpt (gpt-luna, low)")

    def test_a_bad_choice_stops_before_running(self):
        self.choose("gpt-old", "")
        with self.assertRaises(tailor.BackendError):
            tailor._codex_json("prompt", {"type": "object"})
        self.assertEqual(self.fake.runs, [])

    def test_header_without_a_model_line(self):
        self.assertEqual(tailor._codex_ran("something else"), "chatgpt (codex)")


class ClaudeTests(Base):
    """The Claude login: aliases and effort passed to the claude CLI."""

    def setUp(self):
        super().setUp()
        self.runs, self.answer = [], {"is_error": False, "structured_output": {"ok": True},
                                      "modelUsage": {"claude-sonnet-5": {}}}
        p = mock.patch("subprocess.run", self.claude_cli)
        p.start()
        self.addCleanup(p.stop)

    def claude_cli(self, cmd, **kw):
        self.runs.append(cmd)
        return NS(returncode=0, stdout=json.dumps(self.answer), stderr="")

    def test_default_run_passes_neither(self):
        self.assertEqual(tailor._claude_json("prompt", {}), ({"ok": True}, "claude-sonnet-5"))
        self.assertNotIn("--model", self.runs[-1])
        self.assertNotIn("--effort", self.runs[-1])

    def test_chosen_model_and_effort_reach_claude(self):
        self.store.update({"claude": {"model": "opus", "effort": "xhigh"}})
        tailor._claude_json("prompt", {})
        cmd = self.runs[-1]
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")

    def test_a_model_the_plan_cant_use(self):
        self.store.update({"claude": {"model": "fable"}})
        self.answer = {"is_error": True, "result": "There's an issue with the selected model (fable). It may not exist."}
        with self.assertRaisesRegex(tailor.BackendError, "can't use the Fable model. Pick another in Settings"):
            tailor._claude_json("prompt", {})

    def test_settings_offer_the_same_models_and_efforts(self):
        sec = config.SCHEMA.fields["claude"].fields
        self.assertEqual(set(sec["model"].choices) - {""}, {m["id"] for m in tailor.CLAUDE_MODELS})
        self.assertEqual(set(sec["effort"].choices) - {""}, set(tailor.EFFORTS))
        with self.assertRaises(config.SettingsError):
            self.store.update({"claude": {"model": "claude-anything"}})


class SettingsTests(Base):
    def test_bad_values_are_rejected(self):
        with self.assertRaises(config.SettingsError):
            self.store.update({"chatgpt": {"model": "gpt sol; rm -rf"}})
        with self.assertRaises(config.SettingsError):
            self.store.update({"chatgpt": {"effort": "High!"}})
        self.assertEqual(self.store.load()["chatgpt"], {"model": "", "effort": ""})

    def test_saving_checks_against_the_catalog(self):
        """POST /api/settings through the real handler."""
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import server
        for n, v in (("STORE", self.store), ("DB_PATH", self.tmp / "jobs.db"), ("MASTER", self.tmp / "master.tex"),
                     ("LOG_PATH", self.tmp / "server.log"), ("schedule_render", lambda: None)):
            p = mock.patch.object(server, n, v)
            p.start()
            self.addCleanup(p.stop)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def post(chatgpt):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            conn.request("POST", "/api/settings", json.dumps({"settings": {"chatgpt": chatgpt}}),
                         {"Host": f"{server.HOST}:{server.PORT}", "Content-Type": "application/json",
                          "Origin": f"http://{server.HOST}:{server.PORT}"})
            r = conn.getresponse()
            body = json.loads(r.read().decode("utf-8"))
            conn.close()
            return r.status, body

        status, body = post({"model": "gpt-luna", "effort": "max"})
        self.assertEqual(status, 400)
        self.assertIn("doesn't take max", body["error"])
        self.assertEqual(self.store.load()["chatgpt"]["model"], "", "nothing saved")
        status, body = post({"model": "gpt-sol", "effort": "high"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.load()["chatgpt"], {"model": "gpt-sol", "effort": "high"})
        self.assertEqual([m["id"] for m in body["ai"]["chatgpt_models"]], ["gpt-sol", "gpt-luna"])


if __name__ == "__main__":
    unittest.main()
