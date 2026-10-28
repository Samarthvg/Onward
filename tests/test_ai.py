"""Paid API backends, cost control, writing voice and tailoring strictness.

Every model client here is a fake: no test makes a paid call or needs a key.

Run:  python -m unittest tests.test_ai
"""

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import ai
import config
import pricing
import resume_tex as rt
import server
import tailor
import voice

KEY = "sk-test-SECRET-0123456789abcdefghijklmnop"
TEX = r"""\documentclass{article}
\begin{document}
\section*{Experience}
\textbf{First Corp} \hfill Jan 2025 - Present \\
\textit{Software Engineer}
\begin{itemize}
\item Built billing APIs in \textbf{Python} and FastAPI on AWS.
\item Cut p95 latency by 40\% with Redis caching.
\end{itemize}
\section*{Skills}
\textbf{Languages:} Python, Go \\
\end{document}
"""
JD = ("We build payments on ZetaFlow. You will design billing APIs in Python, run services on Kubernetes "
      "and AWS, and care about latency. ") * 4


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def set_password(self, service, user, pw):
        self.data[(service, user)] = pw

    def get_password(self, service, user):
        return self.data.get((service, user))

    def delete_password(self, service, user):
        self.data.pop((service, user), None)


class FakeAnthropic:
    """Answers like the Anthropic SDK: count_tokens, a streamed message, models.list."""

    def __init__(self, answer: dict, input_tokens=1000, output_tokens=500, cache_read=0, stop="end_turn"):
        self.answer, self.stop = answer, stop
        self.usage = NS(input_tokens=input_tokens, output_tokens=output_tokens,
                        cache_read_input_tokens=cache_read, cache_creation_input_tokens=0)
        self.calls = []
        self.messages = NS(count_tokens=lambda **k: NS(input_tokens=input_tokens))
        self.models = NS(list=lambda: [])
        outer = self

        class Stream:
            def __init__(self, kwargs):
                outer.calls.append(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return NS(usage=outer.usage, stop_reason=outer.stop, model=outer.calls[-1]["model"],
                          content=[NS(type="text", text=json.dumps(outer.answer))])

        self.beta = NS(messages=NS(stream=lambda **k: Stream(k)))

    def with_options(self, **k):
        return self


class FakeOpenAI:
    def __init__(self, answer: dict, input_tokens=2000, cached=1500, output_tokens=800):
        self.calls = []
        self.answer = answer
        self.usage = NS(input_tokens=input_tokens, output_tokens=output_tokens,
                        input_tokens_details=NS(cached_tokens=cached))

        def create(**k):
            self.calls.append(k)
            return NS(usage=self.usage, status="completed", incomplete_details=None, model=k["model"],
                      output=[NS(type="message", content=[NS(type="output_text", text="")])],
                      output_text=json.dumps(self.answer))
        self.responses = NS(create=create, input_tokens=NS(count=lambda **k: NS(input_tokens=input_tokens)))
        self.models = NS(list=lambda: [])

    def with_options(self, **k):
        return self


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-ai-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = config.Store(self.tmp / "settings.json", self.tmp)
        config.use(self.store)
        self.addCleanup(config.use, None)
        self.keyring = FakeKeyring()
        ai.use_keyring(self.keyring)
        self.addCleanup(ai.use_keyring, ai._KEYRING)
        ai._COUNT_CACHE.clear()
        self.clients = dict(ai.CLIENTS)
        self.addCleanup(lambda: ai.CLIENTS.update(self.clients))
        for k in ai.ENV_KEYS.values():
            p = mock.patch.dict("os.environ", {k: ""})
            p.start()
            self.addCleanup(p.stop)

    def fake(self, provider, client):
        ai.CLIENTS[provider] = lambda key: client
        return client


# --------------------------------------------------------------------------

class KeyStorageTests(Base):
    def test_keychain_and_never_in_settings(self):
        self.assertEqual(ai.save_key("openai", "  " + KEY + "\n"), "keychain")
        self.assertEqual(ai.get_key("openai"), KEY)
        self.assertEqual(ai.key_status("openai"), {"saved": True, "where": "keychain"})
        self.store.update({"providers": {"openai": {"monthly_budget": 3}}})
        self.assertNotIn(KEY, self.store.path.read_text(encoding="utf-8"))
        self.assertFalse(ai.key_file().exists())
        ai.delete_key("openai")
        self.assertEqual(ai.key_status("openai"), {"saved": False, "where": None})

    def test_file_fallback_is_private_and_outside_the_code_folder(self):
        ai.use_keyring(None)
        self.assertEqual(ai.save_key("anthropic", KEY), "file")
        path = ai.key_file()
        # Resolved: Windows can give the temp folder its short 8.3 name (RUNNER~1).
        self.assertTrue(path.resolve().is_relative_to(self.tmp.resolve()))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"anthropic": KEY})
        self.assertEqual(ai.get_key("anthropic"), KEY)
        # An install whose data folder is the code folder keeps keys elsewhere.
        with mock.patch.object(config, "user_data_dir", lambda: self.tmp / "platform"):
            config.use(config.Store(config.CODE_DIR / "settings.json", config.CODE_DIR))
            self.assertEqual(ai.key_file().parent.parent, self.tmp / "platform")
            self.assertFalse(ai.key_file().is_relative_to(config.CODE_DIR))

    def test_settings_payload_and_unknown_fields_never_carry_a_key(self):
        ai.save_key("anthropic", KEY)
        with self.assertRaises(config.SettingsError):
            self.store.update({"providers": {"anthropic": {"api_key": KEY, "model": "not-a-model"}}})
        self.store.update({"providers": {"anthropic": {"api_key": KEY}}}, strict=False)
        self.assertNotIn(KEY, self.store.path.read_text(encoding="utf-8"))
        with mock.patch.object(server, "STORE", self.store), mock.patch.object(server, "MASTER", self.tmp / "none.tex"):
            payload = json.dumps(server.settings_payload())
        self.assertNotIn(KEY, payload)
        self.assertNotIn(KEY[-6:], payload)
        self.assertIn('"saved": true', payload)

    def test_key_saved_before_the_rename_moves_to_the_new_entry(self):
        self.keyring.set_password(ai.LEGACY_KEYRING_SERVICE, "openai", KEY)
        self.assertEqual(ai.key_status("openai"), {"saved": True, "where": "keychain"})
        self.assertEqual(ai.get_key("openai"), KEY)
        self.assertEqual(self.keyring.data, {(ai.KEYRING_SERVICE, "openai"): KEY})
        self.keyring.set_password(ai.LEGACY_KEYRING_SERVICE, "openai", KEY)
        ai.delete_key("openai")
        self.assertEqual(self.keyring.data, {}, "removing a key removes both entries")

    def test_bad_key_rejected(self):
        with self.assertRaises(ValueError):
            ai.save_key("openai", "not a key")
        with self.assertRaises(ValueError):
            ai.save_key("gemini", KEY)


class KeyEndpointTests(Base):
    """POST /api/keys through the real HTTP handler."""

    def setUp(self):
        super().setUp()
        self.patches = [mock.patch.object(server, n, v) for n, v in (
            ("STORE", self.store), ("DB_PATH", self.tmp / "jobs.db"), ("MASTER", self.tmp / "master.tex"),
            ("LOG_PATH", self.tmp / "server.log"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, origin=f"http://{server.HOST}:{server.PORT}"):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        headers = {"Host": f"{server.HOST}:{server.PORT}", "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        raw = r.read().decode("utf-8")
        conn.close()
        return r.status, raw

    def test_save_check_and_remove(self):
        self.fake("openai", FakeOpenAI({}))
        status, raw = self.request("POST", "/api/keys", {"provider": "openai", "key": KEY})
        self.assertEqual(status, 200, raw)
        self.assertNotIn(KEY, raw)
        self.assertEqual(json.loads(raw)["key"], {"saved": True, "where": "keychain"})
        for path in ("/api/settings", "/api/status", "/api/usage"):
            status, raw = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertNotIn(KEY, raw, path)
        status, raw = self.request("POST", "/api/keys", {"provider": "openai", "key": ""})
        self.assertEqual(json.loads(raw)["key"]["saved"], False)
        log = (self.tmp / "server.log").read_text(encoding="utf-8") if (self.tmp / "server.log").exists() else ""
        self.assertNotIn(KEY, log)

    def test_rejected_key_is_not_saved(self):
        class Rejecting:
            class AuthenticationError(Exception):
                pass

            def __init__(self):
                def boom():
                    raise self.AuthenticationError("bad key " + KEY)
                self.models = NS(list=boom)

            def with_options(self, **k):
                return self
        self.fake("anthropic", Rejecting())
        status, raw = self.request("POST", "/api/keys", {"provider": "anthropic", "key": KEY})
        self.assertEqual(status, 422)
        self.assertNotIn(KEY, raw)
        self.assertFalse(ai.key_status("anthropic")["saved"])

    def test_saved_resume_goes_back_only_to_the_dashboard(self):
        resume = {"name": "me.pdf", "text": "Python and SQL. " * 20, "off": ["Go"], "added": [], "years": 1.5}
        with mock.patch.object(server.PREP, "scoring_path", self.tmp / "scoring.json"):
            status, _ = self.request("GET", "/api/scoring", origin=None)
            self.assertEqual(status, 404)
            status, raw = self.request("POST", "/api/scoring", resume)
            self.assertEqual(status, 200, raw)
            status, raw = self.request("GET", "/api/scoring", origin=None)
            self.assertEqual(status, 200, raw)
            got = json.loads(raw)
            self.assertEqual((got["name"], got["text"], got["off"], got["years"]), ("me.pdf", resume["text"], ["Go"], 1.5))
            self.assertNotIn("profile", got)
            status, raw = self.request("GET", "/api/scoring", origin="https://evil.example")
            self.assertEqual(status, 403)
            self.assertNotIn("Python", raw)

    def test_dashboard_state_is_shared_and_the_newer_copy_wins(self):
        kit = {"firstName": "Testy", "email": "testy@example.com"}
        status, raw = self.request("POST", "/api/state", {"key": "profile", "value": kit, "at": 2000})
        self.assertEqual((status, json.loads(raw)["value"]), (200, kit))
        # An older copy (a window that hadn't seen the change) doesn't overwrite it.
        status, raw = self.request("POST", "/api/state", {"key": "profile", "value": {"firstName": "Old"}, "at": 1000})
        self.assertEqual(json.loads(raw), {"value": kit, "at": 2000})
        self.request("POST", "/api/state", {"key": "marks", "value": {"https://x/1": "applied", "https://x/2": "bogus"},
                                            "at": 5})
        status, raw = self.request("GET", "/api/state", origin=None)
        self.assertEqual(json.loads(raw), {"profile": {"value": kit, "at": 2000},
                                           "marks": {"value": {"https://x/1": "applied"}, "at": 5}})
        # The extension may read it; other sites may not, and only the dashboard writes.
        ext = {"Origin": "moz-extension://1234-abcd", "X-Onward-Client": "autofill"}
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/state", headers={"Host": f"{server.HOST}:{server.PORT}", **ext})
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["profile"]["value"]), (200, kit))
        conn.close()
        status, raw = self.request("GET", "/api/state", origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertNotIn("Testy", raw)
        status, _ = self.request("POST", "/api/state", {"key": "profile", "value": {}, "at": 9999},
                                 origin="moz-extension://1234-abcd")
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/state", {"key": "resume", "value": {}, "at": 1})
        self.assertEqual(status, 400, "only the keys the dashboard shares")

    def test_only_the_dashboard_may_save_keys(self):
        status, _ = self.request("POST", "/api/keys", {"provider": "openai", "key": KEY}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertFalse(ai.key_status("openai")["saved"])


# --------------------------------------------------------------------------

class BudgetTests(Base):
    ANSWER = {"answer": "I built the billing service at First Corp and liked making it fast."}

    def setUp(self):
        super().setUp()
        ai.save_key("anthropic", KEY)
        ai.save_key("openai", KEY)

    def test_records_actual_usage_and_cost(self):
        c = self.fake("anthropic", FakeAnthropic(self.ANSWER, input_tokens=1000, output_tokens=500, cache_read=2000))
        data, model, info = ai.call_json("anthropic", "rules", "material", tailor.DRAFT_SCHEMA, "draft")
        self.assertEqual(data, self.ANSWER)
        self.assertEqual(model, "claude-opus-5-5")
        price = pricing.find("anthropic", "claude-opus-5-5")
        self.assertAlmostEqual(info["cost"], round((1000 * price.input + 2000 * price.cache_read + 500 * price.output) / 1e6, 4))
        self.assertAlmostEqual(ai.spent("anthropic"), info["cost"], places=4)
        u = ai.usage_summary()
        self.assertEqual(u["calls"][0]["state"], "done")
        self.assertEqual((u["months"][0]["provider"], u["months"][0]["calls"]), ("anthropic", 1))
        self.assertEqual(len(c.calls), 1)

    def test_openai_cached_tokens_are_priced_as_cache_reads(self):
        self.fake("openai", FakeOpenAI(self.ANSWER, input_tokens=2000, cached=1500, output_tokens=800))
        _, model, info = ai.call_json("openai", "rules", "material", tailor.DRAFT_SCHEMA, "draft")
        p = pricing.find("openai", model)
        self.assertAlmostEqual(info["cost"], round((500 * p.input + 1500 * p.cache_read + 800 * p.output) / 1e6, 4))

    def test_cap_blocks_further_calls(self):
        c = self.fake("anthropic", FakeAnthropic(self.ANSWER, input_tokens=1000, output_tokens=2000))
        # One draft estimates at about 4 cents and really costs about 4.4: a 5 cent budget fits one.
        self.store.update({"providers": {"anthropic": {"monthly_budget": 0.05}}})
        ai.call_json("anthropic", "rules", "material", tailor.DRAFT_SCHEMA, "draft")
        with self.assertRaises(ai.BudgetError) as cm:
            ai.call_json("anthropic", "rules", "material", tailor.DRAFT_SCHEMA, "draft")
        self.assertIn("Anthropic API budget", str(cm.exception))
        self.assertIn("Settings", str(cm.exception))
        self.assertEqual(len(c.calls), 1, "the blocked call never reached the API")
        # The other provider has its own budget.
        self.fake("openai", FakeOpenAI(self.ANSWER))
        ai.call_json("openai", "rules", "material", tailor.DRAFT_SCHEMA, "draft")

    def test_zero_budget_blocks_everything(self):
        c = self.fake("openai", FakeOpenAI(self.ANSWER))
        self.store.update({"providers": {"openai": {"monthly_budget": 0}}})
        with self.assertRaises(ai.BudgetError):
            tailor.draft_answer(TEX, {"company": "Acme", "title": "SWE"}, "Why Acme?", "", JD, "openai-api")
        self.assertEqual(c.calls, [])

    def test_reservations_count_before_the_call_returns(self):
        self.store.update({"providers": {"anthropic": {"monthly_budget": 0.10}}})
        ai.reserve("anthropic", "claude-opus-5-5", "draft", 0.06)
        with self.assertRaises(ai.BudgetError):
            ai.reserve("anthropic", "claude-opus-5-5", "draft", 0.06)

    def test_failed_call_costs_nothing_and_frees_the_reservation(self):
        class Down(FakeAnthropic):
            def __init__(self):
                super().__init__({})
                def stream(**k):
                    raise ai.BackendError("Couldn't reach the Anthropic API.")
                self.beta = NS(messages=NS(stream=stream))
        self.fake("anthropic", Down())
        with self.assertRaises(ai.BackendError):
            ai.call_json("anthropic", "rules", "material", tailor.DRAFT_SCHEMA, "draft")
        self.assertEqual(ai.spent("anthropic"), 0)
        self.assertEqual(ai.usage_summary()["calls"][0]["state"], "failed")

    def test_cut_off_answer_is_still_billed(self):
        self.fake("anthropic", FakeAnthropic(self.ANSWER, stop="max_tokens"))
        with self.assertRaises(ai.BackendError):
            ai.call_json("anthropic", "rules", "material", tailor.DRAFT_SCHEMA, "draft")
        self.assertGreater(ai.spent("anthropic"), 0)

    def test_estimate_before_running(self):
        self.fake("anthropic", FakeAnthropic({}, input_tokens=4321))
        est = tailor.estimate_tailor(TEX, {"company": "Acme", "title": "SWE"}, JD, "claude-api")
        self.assertEqual((est["input_tokens"], est["exact_input"]), (4321, True))
        self.assertEqual(est["output_tokens"], ai.OUTPUT_TOKENS["tailor"])
        self.assertGreater(est["cost"], 0)
        self.assertIsNone(tailor.estimate_tailor(TEX, {}, JD, "chatgpt"), "subscriptions show no cost")
        self.assertIsNone(tailor.estimate_tailor(TEX, {}, JD, "manual"))
        rough = tailor.estimate_tailor(TEX, {}, JD, "openai-api", exact=False)
        self.assertFalse(rough["exact_input"])

    def test_subscription_backends_record_nothing(self):
        with mock.patch.object(tailor, "_codex_json", lambda prompt, schema: (self.ANSWER, "chatgpt (codex)")):
            out = tailor.draft_answer(TEX, {"company": "Acme", "title": "SWE"}, "Why Acme?", "", JD, "chatgpt")
        self.assertIsNone(out["cost"])
        self.assertEqual(ai.usage_summary()["calls"], [])

    def test_queue_pauses_on_budget(self):
        import prep
        self.assertTrue(prep.FATAL.search(ai.budget_message("openai", 5, 4.9, 0.3)))
        self.assertTrue(prep.FATAL.search("No OpenAI API key. Add one in Settings, under AI models."))


# --------------------------------------------------------------------------

class VoiceTests(Base):
    def test_defaults_are_the_old_voice(self):
        v = self.store.section("voice")
        self.assertEqual((v["tone"], v["samples"]), ("casual", []))
        self.assertEqual(tuple(v["banned_phrases"]), voice.DEFAULT_BANNED)
        self.assertEqual(tailor.voice_rules(), tailor.VOICE)

    def test_settings_reach_every_prompt(self):
        self.store.update({"voice": {"tone": "formal", "samples": ["I keep my notes short and I end with a question?"],
                                     "banned_phrases": ["synerg*", "game changer"]}})
        job = {"company": "Acme", "title": "SWE"}
        instructions, material = tailor.draft_prompt(TEX, job, "Why Acme?", "", JD, 0)
        a_instr, a_material = tailor.answers_prompt(TEX, job, {}, [{"id": "1", "kind": "textarea", "question": "Why?"}], JD)
        for text in (instructions, a_instr, tailor.rewrite_instructions()):
            self.assertIn("polished, professional register", text)
            self.assertIn('"game changer"', text)
            self.assertNotIn("new-grad student", text)
        for text in (material, a_material):
            self.assertIn("WRITING SAMPLES", text)
            self.assertIn("end with a question?", text)
        self.assertIn("uses the stock phrase \"synergizing\"", tailor.style_problems("We love synergizing.", ""))
        self.assertEqual(tailor.style_problems("This aligns with my interests.", ""), [],
                         "phrases taken off the list aren't flagged")

    def test_prompts_reach_the_paid_api(self):
        ai.save_key("anthropic", KEY)
        self.store.update({"voice": {"samples": ["Honestly I just like fixing slow pages."]}})
        c = self.fake("anthropic", FakeAnthropic({"answer": "I like making slow pages fast, so this fits me."}))
        out = tailor.draft_answer(TEX, {"company": "Acme", "title": "SWE"}, "Why Acme?", "", JD, "claude-api")
        self.assertIn("fixing slow pages", c.calls[0]["messages"][0]["content"])
        self.assertIn("How to write it", c.calls[0]["system"][0]["text"])
        self.assertIsNotNone(out["cost"])

    def test_phrase_syntax(self):
        rx = voice.compile_phrases(("cutting-edge", "not only ... but also", "I'd bring", "resonat*"))
        hits = lambda t: [r.pattern for r in rx if r.search(t)]
        self.assertEqual(len(hits("cutting edge work")), 1)
        self.assertEqual(len(hits("not only fast but also cheap")), 1)
        self.assertEqual(len(hits("I’d bring skills")), 1)
        self.assertEqual(len(hits("it resonated")), 1)
        self.assertEqual(hits("a resonant voice, edgeless"), [], "no stems beyond what * allows")

    def test_invalid_voice_settings_rejected(self):
        with self.assertRaises(config.SettingsError):
            self.store.update({"voice": {"tone": "shouty"}})
        with self.assertRaises(config.SettingsError):
            self.store.update({"voice": {"samples": ["a", "b", "c", "d"]}})


# --------------------------------------------------------------------------

class StrictnessTests(unittest.TestCase):
    def setUp(self):
        self.res = rt.parse(TEX)
        self.b1 = self.res.bullet("B1")[1]
        self.base = len(rt.plain(self.b1.tex))

    def plan(self, text, level):
        _, changes, state = rt.plan_edits(self.res, {"bullet_rewrites": [{"id": "B1", "text": text}]}, JD, level)
        return changes[0]

    def test_reword_allows_a_longer_rephrase_that_strict_rejects(self):
        text = ("Designed and built the billing APIs that charge every customer, in \\textbf{Python} and FastAPI, "
                "running them on AWS")
        n = len(rt.plain(text))
        self.assertTrue(self.base * 1.3 + 40 < n <= self.base * 1.5 + 40, n)
        strict = self.plan(text, "strict")
        self.assertEqual((strict.status, strict.reason), ("rejected", "makes the bullet much longer"))
        self.assertEqual(self.plan(text, "reword").status, "applied")

    def test_honesty_checks_hold_at_every_level(self):
        cases = {
            "adds Kubernetes": "Built billing APIs in \\textbf{Python} and FastAPI on Kubernetes and AWS.",
            "introduces numbers": "Built 12 billing APIs in \\textbf{Python} and FastAPI on AWS.",
            "from the job description": "Built ZetaFlow billing APIs in \\textbf{Python} and FastAPI on AWS.",
        }
        for level in config.STRICTNESS:
            for why, text in cases.items():
                c = self.plan(text, level)
                self.assertEqual(c.status, "rejected", (level, why))
                self.assertIn(why, c.reason, (level, why))

    def test_levels_change_the_rules_not_the_truthfulness(self):
        strict, reword = tailor.instructions("strict"), tailor.instructions("reword")
        self.assertEqual(strict, tailor.INSTRUCTIONS)
        self.assertIn("rephrase a bullet freely", reword)
        self.assertNotIn("rephrase a bullet freely", strict)
        for rule in ("1. Truthfulness comes first.", "2. Never add or change numbers", "6. tech_lines and skills_rows"):
            self.assertIn(rule, reword)

    def test_setting_reaches_the_prompt(self):
        tmp = Path(tempfile.mkdtemp(prefix="jr-strict-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        st = config.Store(tmp / "settings.json", tmp)
        config.use(st)
        self.addCleanup(config.use, None)
        self.assertIn("about the same length as the original", tailor.build_manual_prompt(TEX, {}, JD))
        st.update({"tailoring": {"strictness": "reword"}})
        self.assertIn("rephrase a bullet freely", tailor.build_manual_prompt(TEX, {}, JD))
        with self.assertRaises(config.SettingsError):
            st.update({"tailoring": {"strictness": "anything goes"}})


class MigrationTests(unittest.TestCase):
    def test_claude_model_moves_to_providers(self):
        tmp = Path(tempfile.mkdtemp(prefix="jr-mig-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "settings.json").write_text(json.dumps({"version": 3, "tailoring": {"claude_model": "claude-sonnet-5-5"}}))
        s = config.Store(tmp / "settings.json", tmp).load()
        self.assertEqual(s["providers"]["anthropic"]["model"], "claude-sonnet-5-5")
        self.assertNotIn("claude_model", s["tailoring"])
        (tmp / "settings.json").write_text(json.dumps({"version": 3, "tailoring": {"claude_model": "claude-old-1"}}))
        s = config.Store(tmp / "settings.json", tmp).load()
        self.assertEqual(s["providers"]["anthropic"]["model"], pricing.default_model("anthropic"))
        self.assertIn("claude-old-1", s["notices"][0])


if __name__ == "__main__":
    unittest.main()
