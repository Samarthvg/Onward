"""The apply queue end to end, with fake data and a fake model.

A radar run is simulated on a temporary database with radar.py's own
functions, then the queue (prep.py, as server.py runs it) prepares jobs:
tailored PDFs compiled with LaTeX, drafted answers, the skip-tailoring rule,
the daily cap, notifications, and the helper endpoints the extension and
dashboard use.

Run:  python -m unittest test_prep
"""

import base64
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import config
import notify
import prep
import questions
import radar
import resume_tex as rt
import server
import tailor

MASTER = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[margin=0.6in]{geometry}
\begin{document}
\begin{center}{\Large Testy McTestface} \\ testy@example.com\end{center}
\section*{Experience}
\textbf{Example Corp} \hfill Jan 2025 - Present \\
\textit{Software Engineer} | \textit{Python, FastAPI, PostgreSQL}
\begin{itemize}
\item Built REST APIs in Python and FastAPI on AWS, backed by PostgreSQL and Redis.
\item Wrote React and TypeScript dashboards for an internal billing tool.
\item Set up CI/CD with GitHub Actions and Docker for six services.
\end{itemize}
% skills: Kubernetes, Kafka
\section*{Projects}
\textbf{Tracer} | \textit{Rust, gRPC}
\begin{itemize}
\item Wrote a distributed tracing tool in Rust using gRPC.
\item Added tail-based sampling that cut stored spans by half.
\item Documented the wire format for other contributors.
\end{itemize}
\section*{Skills}
\textbf{Languages:} Python, TypeScript, Rust, SQL \\
\textbf{Tools:} Docker, AWS, PostgreSQL, Redis \\
\end{document}
"""

RESUME_TEXT = rt.plain(MASTER).replace(" Example Corp", "\nExample Corp\n").replace(" Projects", "\nProjects\n")

BODY = """About the role
You will build backend services for our platform team, ship features end to end and work with product.
Requirements:
- {req}
- Bachelor's degree in Computer Science or equivalent experience
Nice to have:
- {pref}
{extra}
Benefits
- Health insurance and a learning budget
"""


def job(company, title, location, req, pref="Experience with Rust", extra="", jid=1):
    return radar.build_job(
        company=company, title=title, location=location, country="US", remote=False,
        url=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{jid}", source="greenhouse",
        posted_at=time.time() - 600, posted_exact=True,
        description=BODY.format(req=req, pref=pref, extra=extra),
        apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={company.lower()}&token={jid}",
        verify_url="", posted_field="first_published", posted_raw="x")


JOBS = [
    # Kubernetes and Kafka are only in the master's skill tags: tailoring can add them.
    job("Tailorco", "Software Engineer, New Grad", "Boston, MA",
        "Experience with Python, Kubernetes, Kafka, PostgreSQL, Docker and AWS", jid=101),
    # Everything asked for is already on the resume: use the master.
    job("Coveredco", "Backend Engineer I", "Boston, MA",
        "Experience with Python, PostgreSQL, Redis, Docker and AWS", jid=102),
    # Poor match: never queued.
    job("Farco", "Software Engineer, New Grad", "Boston, MA",
        "Experience with Elixir, Haskell, OCaml, Erlang and Clojure", pref="Experience with Prolog", jid=103),
    # Good match, but the posting won't sponsor.
    job("Nosponsorco", "Backend Engineer I", "Boston, MA",
        "Experience with Python, PostgreSQL, Redis, Docker and AWS",
        extra="We are unable to sponsor visas for this role.", jid=104),
    # Good match, outside the user's locations.
    job("Farawayco", "Backend Engineer I", "Austin, TX",
        "Experience with Python, PostgreSQL, Redis, Docker and AWS", jid=105),
    # Second tailoring candidate, for the daily cap.
    job("Tailortwo", "Software Engineer, New Grad", "Remote - US",
        "Experience with Python, Kubernetes, Kafka, PostgreSQL, Docker and AWS", jid=106),
]

FORM_QUESTIONS = [
    {"id": "question_1", "question": "Why do you want to work here?", "kind": "textarea", "options": [], "max_chars": 0},
    {"id": "question_2", "question": "Are you open to working in the office 3 days a week?", "kind": "choice",
     "options": ["Yes", "No"], "max_chars": 0},
]


class H1BStub:
    def lookup(self, company):
        return None


class FakeModel:
    """Stands in for the codex CLI; answers by schema and counts calls."""

    def __init__(self):
        self.calls = []
        self.prompts = []

    def __call__(self, prompt, schema):
        self.calls.append(schema)
        self.prompts.append(prompt)
        if schema is tailor.SCHEMA:
            ids = re.findall(r"\[(B\d+)\]", prompt)
            return {"bullet_rewrites": [], "tech_lines": [],
                    "skills_rows": [{"id": "S2", "text": "Docker, AWS, PostgreSQL, Redis, Kubernetes, Kafka"}],
                    "bullet_priority": ids, "drop_bullets": [], "drop_entries": [],
                    "jd_keywords": [{"keyword": "Kubernetes", "status": "added", "note": ""},
                                    {"keyword": "Python", "status": "already_present", "note": ""}],
                    "summary": "Moved infrastructure skills forward."}, "fake"
        if schema is tailor.ANSWERS_SCHEMA:
            return {"answers": [{"id": "0", "answer": "I build backend tools like yours. (TEST)", "choices": []},
                                {"id": "1", "answer": "Yes", "choices": []}]}, "fake"
        raise AssertionError("unexpected schema")


def simulate_radar_run(db_path: Path, now: float) -> None:
    conn = sqlite3.connect(db_path)
    try:
        radar.migrate(conn)
        jobs = [j for j in JOBS if j]
        assert len(jobs) == len(JOBS), "a fixture job was filtered out by build_job"
        with conn:
            radar.store(conn, jobs, now)
            conn.execute("INSERT INTO runs (started, finished, boards, failed, matched, new) VALUES (?,?,?,?,?,?)",
                         (now, now, 1, 0, len(jobs), len(jobs)))
        stats = radar.corpus_stats(conn)
        rows = radar.load_for_dashboard(conn, now, None, H1BStub(), stats)
        radar.save_scoring(conn, rows, stats, now)
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-prep-"))
        self.resume_dir = self.tmp / "resume"
        self.resume_dir.mkdir()
        (self.resume_dir / "master.tex").write_text(MASTER, encoding="utf-8")
        self.db_path = self.tmp / "jobs.db"
        # Settings of its own, so tests never touch the real settings.json.
        self.store = config.Store(self.tmp / "settings.json")
        config.use(self.store)
        self.addCleanup(config.use, None)
        self.patches = [mock.patch.object(server, "DB_PATH", self.db_path)]
        for p in self.patches:
            p.start()
        self.toasts = []
        self.model = FakeModel()
        self.patches += [
            mock.patch.object(tailor, "_codex_json", self.model),
            mock.patch.object(questions, "fetch", lambda url: [dict(q) for q in FORM_QUESTIONS]),
            mock.patch.object(notify, "desktop", lambda *a: self.toasts.append(a) or True),
            mock.patch.object(notify, "ntfy", lambda *a: self.toasts.append(("ntfy",) + a) or True),
            mock.patch.object(server, "STORE", self.store),
            # Never quiet during these tests unless a test says so (kept last: a test stops it).
            mock.patch.object(notify, "quiet", lambda *a: False),
        ]
        for p in self.patches[1:]:
            p.start()
        self.prep = prep.Preparer(server.db, self.resume_dir, self.resume_dir / "tailored", log=lambda m: None,
                                  store=self.store)
        self.prep.save_settings({"threshold": 40, "locations": ["Boston"], "remote_ok": True})
        self.prep.save_scoring({"name": "resume.pdf", "text": RESUME_TEXT,
                                "profile": {"firstName": "Testy", "school": "Example State University"}})
        self.now = time.time()
        simulate_radar_run(self.db_path, self.now)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def items(self):
        return {i["company"]: i for i in self.prep.items(("ready", "working", "queued", "error", "applied", "dismissed"))}


@unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed to compile tailored resumes")
class QueueTests(Base):

    def test_radar_run_produces_prepared_jobs(self):
        self.prep.step()
        it = self.items()
        self.assertEqual(set(it), {"Tailorco", "Coveredco", "Tailortwo"}, "filters: score, sponsorship, location")
        t = it["Tailorco"]
        self.assertEqual((t["state"], t["decision"], t["choice"]), ("ready", "tailor", "tailored"))
        self.assertTrue((self.resume_dir / "tailored" / t["pdf"]).exists())
        self.assertIn("Kubernetes", t["changes"]["added"])
        self.assertEqual(t["answers"], 2)
        c = it["Coveredco"]
        self.assertEqual((c["state"], c["decision"], c["choice"], c["pdf"]), ("ready", "master", "master", ""))
        self.assertIn("every keyword", c["reason"])
        # Best match first.
        ready = [i["company"] for i in self.prep.items(("ready",))]
        self.assertEqual(ready, sorted(ready, key=lambda k: -it[k]["score"]))
        # 2 tailorings + 3 answer batches, never more than one model call per piece.
        self.assertEqual(len(self.model.calls), 5)
        # Prepared answers are stored with the form's own wording.
        p = self.prep.prepared(t["key"])
        self.assertEqual([a["question"] for a in p["answers"]], [q["question"] for q in FORM_QUESTIONS])
        self.assertTrue(p["pdf"].name.endswith(".pdf"))

    def test_scores_match_dashboard_inputs(self):
        """The queue's score is match.score_job on the stored checklist, which
        test_match proves equal to the dashboard's."""
        self.prep.step()
        conn = server.db()
        try:
            _, skill_w, term_w = self.prep.corpus(conn)
            row = conn.execute(self.prep._select(conn) + "WHERE company = 'Coveredco'").fetchone()
        finally:
            conn.close()
        import match
        want = match.score_job(self.prep._job(row), match.Resume(RESUME_TEXT), skill_w, term_w)["pct"]
        self.assertEqual(self.items()["Coveredco"]["score"], want)

    def test_rescored_when_the_resume_changes(self):
        self.prep.step()
        before = self.items()["Coveredco"]["score"]
        self.prep.save_scoring({"text": RESUME_TEXT, "off": ["Redis", "Docker"]})
        self.prep.step()
        after = self.items()["Coveredco"]
        self.assertEqual(after["state"], "ready", "prepared jobs stay")
        self.assertLess(after["score"], before)

    def test_daily_cap(self):
        # Drafts off, so the one run left goes to tailoring (runs go best match first).
        self.prep.save_settings({"daily_cap": 1, "draft_answers": False})
        self.prep.step()
        it = self.items()
        tailored = [c for c in ("Tailorco", "Tailortwo") if it[c]["state"] == "ready"]
        waiting = [c for c in ("Tailorco", "Tailortwo") if it[c]["state"] == "queued"]
        self.assertEqual(len(tailored), 1)
        self.assertEqual(len(waiting), 1)
        self.assertIn("model runs are used up", it[waiting[0]]["note"])
        # The master-resume job needs no run and gets ready anyway.
        self.assertEqual(it["Coveredco"]["state"], "ready")
        self.assertEqual(len(self.model.calls), 1)
        # Tomorrow it continues.
        with mock.patch.object(prep.Preparer, "today", staticmethod(lambda: "2999-01-01")):
            self.prep.step()
        self.assertEqual(self.items()[waiting[0]]["state"], "ready")

    def test_cap_leaves_drafts_for_fill_time(self):
        self.prep.save_settings({"daily_cap": 0})
        self.prep.step()
        c = self.items()["Coveredco"]
        self.assertEqual((c["state"], c["answers"]), ("ready", 0))
        self.assertIn("drafted when you open the form", c["note"])
        self.assertEqual(self.model.calls, [])

    def test_skip_tailoring_rule(self):
        res = rt.parse(MASTER)
        s = prep.clean_settings({})
        item = lambda k, ok, kind="req": {"k": k, "ok": ok, "kind": kind, "weight": 1.0}
        # Missing keyword the resume can't back: only reordering is possible.
        d, why = prep.decide({"items": [item("Python", True), item("Elixir", False)]}, res, s)
        self.assertEqual(d, "master")
        self.assertIn("only reorder", why)
        # Missing keyword in the skill tags: worth tailoring.
        d, _ = prep.decide({"items": [item("Python", True), item("Kubernetes", False)]}, res, s)
        self.assertEqual(d, "tailor")
        # Addable, but the master already covers enough of what's required.
        many = [item(f"K{i}", True) for i in range(9)] + [item("Kafka", False, "pref")]
        d, why = prep.decide({"items": many}, res, s)
        self.assertEqual(d, "master")
        self.assertIn("100%", why)

    def test_user_actions(self):
        self.prep.step()
        it = self.items()
        key = it["Tailorco"]["key"]
        self.assertTrue(self.prep.set_choice(key, "master"))
        self.assertIsNone(self.prep.prepared(key)["pdf"])
        self.assertFalse(self.prep.set_choice(it["Coveredco"]["key"], "tailored"), "nothing tailored to approve")
        self.prep.mark(key, "applied")
        self.assertEqual(self.items()["Tailorco"]["state"], "applied")
        self.assertNotEqual(self.prep.next_ready()["key"], key)
        self.prep.unmark(key)
        self.assertEqual(self.items()["Tailorco"]["state"], "ready")

    def test_tailor_anyway(self):
        self.prep.step()
        key = self.items()["Coveredco"]["key"]
        calls = len(self.model.calls)
        self.assertTrue(self.prep.force_tailor(key))
        self.prep.step()
        c = self.items()["Coveredco"]
        self.assertEqual((c["state"], c["choice"]), ("ready", "tailored"))
        self.assertEqual(c["answers"], 2, "earlier drafts kept")
        self.assertEqual(len(self.model.calls), calls + 1, "only the tailoring run")
        self.assertFalse(self.prep.force_tailor(key), "already tailored")

    def test_retailor_with_instruction(self):
        self.prep.step()
        t = self.items()["Tailorco"]
        self.prep.set_choice(t["key"], "tailored")
        calls = len(self.model.calls)
        self.assertTrue(self.prep.force_tailor(t["key"], "Emphasize   backend work more."))
        self.prep.step()
        t2 = self.items()["Tailorco"]
        self.assertEqual((t2["state"], t2["choice"], t2["reviewed"]), ("ready", "tailored", False))
        self.assertNotEqual(t2["tailored_id"], t["tailored_id"])
        self.assertEqual(len(self.model.calls), calls + 1)
        self.assertIn("Emphasize backend work more.", self.model.prompts[-1])
        conn = server.db()
        try:
            versions = conn.execute("SELECT id, result FROM tailored WHERE job_key = ? ORDER BY created", (t["key"],)).fetchall()
        finally:
            conn.close()
        self.assertEqual([v[0] for v in versions], [t["tailored_id"], t2["tailored_id"]], "earlier version kept")
        self.assertEqual(json.loads(versions[1][1])["instruction"], "Emphasize backend work more.")

    def test_applications_table_when_a_tracker_exists(self):
        conn = server.db()
        with conn:
            conn.execute("CREATE TABLE applications (job_key TEXT PRIMARY KEY, status TEXT, applied_at REAL)")
        conn.close()
        self.prep.step()
        key = self.items()["Coveredco"]["key"]
        self.prep.mark(key, "applied")
        conn = server.db()
        try:
            self.assertEqual(conn.execute("SELECT status FROM applications WHERE job_key = ?", (key,)).fetchone()[0], "applied")
        finally:
            conn.close()

    def test_notifications_deduped(self):
        self.prep.step()
        self.assertEqual(len(self.toasts), 1)
        title = self.toasts[0][0]
        self.assertEqual(title, "3 jobs ready to apply")
        self.prep.step()
        self.assertEqual(len(self.toasts), 1, "no repeat for the same jobs")

    def test_quiet_hours_hold_notifications(self):
        self.patches[-1].stop()
        self.patches.pop()
        self.prep.save_settings({"quiet_start": "00:00", "quiet_end": "23:59"})
        self.prep.step()
        self.assertEqual(self.toasts, [])
        self.prep.save_settings({"quiet_start": "22:00", "quiet_end": "08:00"})
        self.assertTrue(self.prep.maybe_notify(datetime(2026, 10, 20, 12, 0)))
        self.assertFalse(notify.quiet(datetime(2026, 10, 20, 12, 0), "22:00", "08:00"))
        self.assertTrue(notify.quiet(datetime(2026, 10, 20, 23, 0), "22:00", "08:00"))
        self.assertTrue(notify.quiet(datetime(2026, 10, 21, 7, 59), "22:00", "08:00"))

    def test_rescan_on_new_radar_run(self):
        self.prep.step()
        n = len(self.items())
        self.model.calls.clear()
        self.prep.step()                       # nothing new: no work, no model calls
        self.assertEqual(self.model.calls, [])
        conn = server.db()
        with conn:
            radar.store(conn, [job("Lateco", "Backend Engineer I", "Boston, MA",
                                   "Experience with Python, PostgreSQL, Redis, Docker and AWS", jid=107)], time.time())
        conn.close()
        simulate_radar_run(self.db_path, time.time() + 1)
        self.prep.step()
        self.assertEqual(len(self.items()), n + 1)


@unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed to compile tailored resumes")
class EndpointTests(Base):
    """The helper endpoints, on a spare port."""

    def setUp(self):
        super().setUp()
        self.prep.step()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = httpd.server_address[1]
        self.patches += [mock.patch.object(server, "PORT", self.port), mock.patch.object(server, "PREP", self.prep),
                         mock.patch.object(server, "ORIGINS", {f"http://127.0.0.1:{self.port}"})]
        for p in self.patches[-3:]:
            p.start()
        self.httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None, headers=None):
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
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

    EXT = {"X-JobRadar-Client": "autofill", "Origin": "moz-extension://1234-abcd"}

    def test_prepared_resume_for_the_form(self):
        url = "https://job-boards.greenhouse.io/tailorco/jobs/101"
        st, data = self.call("/api/prepared?url=" + urllib.request.quote(url, safe=""), headers=self.EXT)
        self.assertEqual(st, 200, data)
        self.assertEqual(data["choice"], "tailored")
        self.assertTrue(base64.b64decode(data["resume"]["data"]).startswith(b"%PDF"))
        self.assertEqual(len(data["answers"]), 2)
        # Master chosen: no PDF, so the extension attaches its own.
        url2 = "https://job-boards.greenhouse.io/embed/job_app?for=coveredco&token=102"
        st, data = self.call("/api/prepared?url=" + urllib.request.quote(url2, safe=""), headers=self.EXT)
        self.assertEqual((st, data["choice"], data["resume"]), (200, "master", None))

    def test_only_the_extension_gets_prepared_data(self):
        url = urllib.request.quote("https://job-boards.greenhouse.io/tailorco/jobs/101", safe="")
        st, _ = self.call("/api/prepared?url=" + url)
        self.assertEqual(st, 403)
        st, _ = self.call("/api/prepared?url=" + url, headers={"X-JobRadar-Client": "autofill", "Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.call("/api/applied", {"url": url}, headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)

    def test_confirmation_marks_applied_and_offers_next(self):
        url = "https://job-boards.greenhouse.io/tailorco/jobs/101/confirmation"
        st, data = self.call("/api/applied", {"url": url}, headers=self.EXT)
        self.assertEqual(st, 200, data)
        self.assertIsNotNone(data["next"])
        self.assertNotEqual(data["next"]["company"], "Tailorco")
        st, q = self.call("/api/queue")
        self.assertIn("Tailorco", [i["company"] for i in q["done"]])
        self.assertNotIn("Tailorco", [i["company"] for i in q["items"]])
        # Lever's thank-you page maps back to its posting too.
        self.assertIn("https://jobs.lever.co/x/0b1c/apply", server.apply_url_candidates("https://jobs.lever.co/x/0b1c/thanks"))

    def test_dashboard_retailor_counts_against_the_cap(self):
        dash = {"Origin": f"http://127.0.0.1:{self.port}"}
        key = self.items()["Tailorco"]["key"]
        pool = server.ThreadPoolExecutor(max_workers=1)
        with mock.patch.object(server, "MASTER", self.resume_dir / "master.tex"),                 mock.patch.object(server, "OUT_DIR", self.tmp / "out"), mock.patch.object(server, "EXECUTOR", pool):
            used = self.call("/api/queue")[1]["runs_today"]
            self.prep.save_settings({"daily_cap": used + 1})
            st, data = self.call("/api/tailor", {"key": key, "backend": "chatgpt", "instruction": "Keep the ML bullets."}, headers=dash)
            self.assertEqual(st, 202, data)
            self.assertEqual(self.call("/api/queue")[1]["runs_today"], used + 1)
            st, data = self.call("/api/tailor", {"key": key, "backend": "chatgpt", "instruction": "Again."}, headers=dash)
            self.assertEqual(st, 429, data)
            # Plain tailoring from the dashboard isn't capped.
            st, _ = self.call("/api/tailor", {"key": key, "backend": "chatgpt"}, headers=dash)
            self.assertEqual(st, 202)
            st, q = self.call("/api/queue/tailor", {"key": key, "instruction": "More backend."}, headers=dash)
            self.assertEqual(st, 200, q)
            pool.shutdown(wait=True)      # the tailoring runs use the fake model

    def test_dashboard_settings_and_marks(self):
        st, s = self.call("/api/prep/settings", {"threshold": 75, "daily_cap": 9999},
                          headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual((st, s["threshold"], s["daily_cap"]), (200, 75, 200))
        key = self.items()["Coveredco"]["key"]
        st, _ = self.call("/api/marks", {"marks": {key: "dismissed"}}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(self.items()["Coveredco"]["state"], "dismissed")
        st, _ = self.call("/api/prep/settings", {"threshold": 10})
        self.assertEqual(st, 403, "settings only from the dashboard")


class QuestionFilterTests(unittest.TestCase):
    def test_sensitive_questions_never_go_to_a_model(self):
        for q in ["Do you require visa sponsorship?", "Will you now or in the future require sponsorship?",
                  "Are you legally authorized to work in the US?", "What are your salary expectations?",
                  "Gender", "Are you a protected veteran?", "Please read the arbitration agreement below",
                  "I consent to the privacy policy", "How did you hear about us?", "Are you willing to relocate?",
                  "LinkedIn Profile", "First Name", "Do you have an active security clearance?"]:
            self.assertFalse(questions.keep(q), q)
        for q in ["Why Anthropic?", "When is the earliest you would want to start working with us?",
                  "What is the hardest technical challenge you've faced?"]:
            self.assertTrue(questions.keep(q), q)

    def test_lever_cards(self):
        card = {"fields": [{"type": "textarea", "text": "Why us?", "id": "a"},
                           {"type": "multiple-choice", "text": "Visa sponsorship needed?", "id": "b",
                            "options": [{"text": "Yes"}, {"text": "No"}]}]}
        page = ('<input type="hidden" value="' + json.dumps(card).replace('"', "&quot;") +
                '" name="cards[0b1c2d3e-0000-0000-0000-000000000000][baseTemplate]">')
        self.assertEqual([q["question"] for q in questions.lever(page)], ["Why us?", "Visa sponsorship needed?"])


def set_posted(db_path: Path, company: str, when: float) -> None:
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET posted_at = ? WHERE company = ?", (when, company))
    conn.close()


class QueueAgeTests(Base):
    """The queue only prepares recent postings, however new they are to the
    radar: a board read for the first time lists postings from months ago."""

    def test_old_postings_are_not_prepared(self):
        set_posted(self.db_path, "Coveredco", self.now - 10 * 86400)
        self.prep.scan(self.now)
        it = self.items()
        self.assertNotIn("Coveredco", it)
        self.assertIn("Tailorco", it)

    def test_queued_and_ready_jobs_leave_once_too_old(self):
        self.prep.scan(self.now)
        self.assertIn("Coveredco", self.items())
        set_posted(self.db_path, "Coveredco", self.now - 8 * 86400)
        self.prep.scan(self.now)
        self.assertNotIn("Coveredco", self.items())
        self.assertEqual([i["company"] for i in self.prep.items(("expired",))], ["Coveredco"])

    def test_too_few_skills_to_judge_still_queues(self):
        """A posting naming one skill the resume has scores near the middle; a
        high threshold doesn't leave it out."""
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                radar.store(conn, [job("Thinco", "Software Engineer, New Grad", "Boston, MA", "Experience with Python",
                                       pref="", jid=107)], self.now)
            stats = radar.corpus_stats(conn)
            radar.save_scoring(conn, radar.load_for_dashboard(conn, self.now, None, H1BStub(), stats), stats, self.now)
        finally:
            conn.close()
        self.prep.save_settings({"threshold": 95})
        self.prep.scan(self.now)
        it = self.items()
        self.assertIn("Thinco", it)
        self.assertLess(it["Thinco"]["score"], 95)
        self.assertNotIn("Coveredco", it, "a judged posting under the threshold stays out")

    def test_limit_is_a_setting(self):
        set_posted(self.db_path, "Coveredco", self.now - 10 * 86400)
        self.prep.save_settings({"max_age_days": 14})
        self.prep.scan(self.now)
        self.assertIn("Coveredco", self.items())


class DashboardAgeTests(unittest.TestCase):
    """What the dashboard shows: 30 days of postings, 60 for hiring programs,
    and a role taken down and put up again marked as reposted."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-age-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config.use(config.Store(self.tmp / "settings.json"))
        self.addCleanup(config.use, None)
        self.conn = sqlite3.connect(self.tmp / "jobs.db")
        self.addCleanup(self.conn.close)
        radar.migrate(self.conn)
        self.now = time.time()

    def put(self, title, days_ago, jid, seen_days_ago=0, company="Ageco", location="Boston, MA"):
        j = job(company, title, location, "Experience with Python and PostgreSQL", jid=jid)
        self.assertIsNotNone(j, title)
        j.posted_at = self.now - days_ago * 86400
        with self.conn:
            radar.store(self.conn, [j], self.now - seen_days_ago * 86400)

    def shown(self):
        return {d["title"] + " #" + d["url"].rsplit("/", 1)[-1]: d
                for d in radar.load_for_dashboard(self.conn, self.now, None, H1BStub())}

    def test_programs_stay_longer(self):
        self.put("Backend Engineer I", 20, 1)
        self.put("Backend Engineer I", 40, 2, location="Seattle, WA")
        self.put("Software Engineer, New Grad 2027", 40, 3)
        self.put("Software Engineer, Entry Level - University Graduate", 70, 4)
        got = self.shown()
        self.assertEqual(set(got), {"Backend Engineer I #1", "Software Engineer, New Grad 2027 #3"})
        self.assertTrue(got["Software Engineer, New Grad 2027 #3"]["program"])
        self.assertFalse(got["Backend Engineer I #1"]["program"])

    def test_repost_of_a_closed_posting(self):
        # Up 25 days ago, gone from the board for the last 5; back yesterday under a new ID.
        self.put("Backend Engineer I (R100234)", 25, 1, seen_days_ago=5)
        self.put("Backend Engineer I (R100977)", 1, 2)
        got = self.shown()
        self.assertEqual(list(got), ["Backend Engineer I (R100977) #2"])
        rep = got["Backend Engineer I (R100977) #2"]["repost"]
        self.assertEqual(rep["times"], 1)
        self.assertAlmostEqual(rep["first"], self.now - 25 * 86400, delta=1)

    def test_repost_under_the_same_title(self):
        # One role, one row: put up again, its posting date moves forward.
        self.put("Backend Engineer I", 30, 1, seen_days_ago=4)
        self.put("Backend Engineer I", 2, 2)
        got = self.shown()
        self.assertEqual(list(got), ["Backend Engineer I #2"])
        rep = got["Backend Engineer I #2"]["repost"]
        self.assertEqual(rep["times"], 1)
        self.assertAlmostEqual(rep["first"], self.now - 30 * 86400, delta=1)

    def test_seen_again_unchanged_is_not_a_repost(self):
        self.put("Backend Engineer I", 9, 1, seen_days_ago=1)
        self.put("Backend Engineer I", 9, 1)
        self.assertIsNone(self.shown()["Backend Engineer I #1"]["repost"])


class ProgramTitleTests(unittest.TestCase):
    def test_program_titles(self):
        import filters
        for t in ("Software Engineer, New Grad 2027", "SDE I, 2027 University Graduate", "Backend Engineer - Early Career",
                  "Software Engineer (Entry Level)", "New College Grad - Software"):
            self.assertTrue(filters.is_program(t), t)
        for t in ("Software Engineer", "Backend Engineer I", "Senior Software Engineer"):
            self.assertFalse(filters.is_program(t), t)

    def test_identity_ignores_requisition_numbers(self):
        import filters
        a = filters.posting_identity("DoorDash, Inc.", "Software Engineer (R123456)", "San Francisco, CA; Seattle, WA")
        b = filters.posting_identity("DoorDash Inc", "Software Engineer - R987654", "San Francisco, CA")
        self.assertEqual(a, b)
        self.assertNotEqual(a, filters.posting_identity("DoorDash", "Software Engineer", "Seattle, WA"))
        # A graduation year is part of the role, not an ID.
        self.assertNotEqual(filters.posting_identity("X", "New Grad 2026", ""), filters.posting_identity("X", "New Grad 2027", ""))


if __name__ == "__main__":
    unittest.main()
