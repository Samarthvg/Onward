"""The helper's match score (match.py) must equal the dashboard's (scoreJob
in dashboard_template.html). Runs the dashboard's own scoring code in Node on
the same fixtures and compares every number.

Run:  python -m unittest tests.test_match
"""

import json
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path

import match
import prep
import radar
import reqparse

ROOT = Path(__file__).resolve().parent.parent
NOW_MS = datetime(2026, 10, 20, 12, 0).timestamp() * 1000

RESUMES = {
    "plain": """Alex Example
alex@example.com | github.com/alex-example
Education
Example State University, Boston, MA
Bachelor of Science in Computer Science, GPA 3.8 Sep 2021 - May 2025
Experience
Acme Corp Jan 2025 - Present
Software Engineer
- Built REST APIs in Python and FastAPI on AWS Lambda, with PostgreSQL and Redis
- Wrote React and TypeScript dashboards; CI/CD with GitHub Actions and Docker
Widget Labs May 2023 - Aug 2023
Software Engineering Intern
- Shipped a Go service on Kubernetes; Terraform for infra
Projects
Tracer: a distributed tracing tool in Rust using gRPC and Kafka
Skills: Python, Java, TypeScript, SQL, Git, Linux, Snowflake, dbt
""",
    "senior-ish": """Sam Sample
Master of Science in Computer Science
Northeastern University  Sept 2024 - May 2026
Big Co  Jun 2019 - Aug 2022
Backend Engineer
Java, Spring Boot, Kafka, MySQL, microservices
Startup Inc  Mar 2018 - Jun 2020
Co-op Software Developer
C++, CUDA, PyTorch, machine learning pipelines
""",
    "tex-ish": match.tex_text(r"""\documentclass{article}
\begin{document}
\section*{Experience}
\textbf{Data Intern} \hfill Jun. 2024 -- Aug. 2024 \\
\textit{Analytics Co} \\
\begin{itemize}\item Built Airflow DAGs and Spark jobs in Scala; dashboards in Tableau \end{itemize}
% skills: Kafka
\section*{Skills}
Python, Pandas, NumPy, scikit-learn, \href{https://example.com}{example.com}
\end{document}
"""),
}

DESCRIPTIONS = [
    ("Software Engineer, New Grad", """About the role
You will build backend services.
Requirements:
- Experience with Python or Go
- Familiarity with Kubernetes, Docker and AWS
- Bachelor's degree in Computer Science or equivalent experience
- Experience with PostgreSQL and Redis
Nice to have:
- Rust or C++
- Experience with Kafka and gRPC
Benefits
- Health insurance
""", None, True),
    ("Frontend Engineer (React)", """What you'll need
- 2+ years of experience with React, TypeScript and Next.js
- Experience with GraphQL and Tailwind
- Strong problem-solving skills
Bonus
- Figma, Storybook
""", 2, False),
    ("Machine Learning Engineer", """Qualifications
- Master's degree or PhD in a related field
- Experience with PyTorch, CUDA and distributed training
- Proficiency in Python and C++
- Familiarity with Triton and Ray
""", 3, False),
    ("Data Engineer", """You have:
- Experience building pipelines with Airflow, Spark and dbt
- Strong SQL; Snowflake or BigQuery
- Experience with Scala or Java
""", None, False),
    ("Software Engineer", """We are a small team.
Requirements
- Experience with Elixir
""", 1, False),
    ("Backend Engineer, Java", """Minimum qualifications
- 1+ years with Java and Spring Boot
- Microservices, Kafka, MySQL
- Experience with Datadog and Terraform
""", 1, True),
    # Names a nice-to-have or two and requires nothing (a platform admin role).
    ("Platform Engineer- Communications Platform", """Responsibilities
- Configure and support the email marketing and customer feedback platforms.
- Troubleshoot integrations between business systems.
Preferred qualifications
- Familiarity with data structures and debugging.
- Basic SQL is a plus.
- Scripting experience is helpful but not required.
""", None, False),
]


def fixtures():
    descs = [d for _, d, _, _ in DESCRIPTIONS]
    # A learned-term vocabulary so non-taxonomy keywords are exercised too.
    term_w = {"Triton": 1.2, "Datadog": 0.9, "Next.js": 0.7, "Tailwind": 1.1, "Storybook": 1.3}
    term_rx = radar.term_patterns(term_w)
    skill_w = {}
    for d in descs:
        for k in radar.find_skills(d):
            skill_w.setdefault(k, round(0.25 + (len(k) % 7) * 0.2, 2))
    skill_w["Git"] = 0.3
    jobs = []
    for i, (title, desc, yoe, entry) in enumerate(DESCRIPTIONS):
        jobs.append({"key": f"j{i}", "title": title, "has_jd": True, "min_yoe": yoe, "entry_hint": entry,
                     "kw": reqparse.keywords(desc, title, radar.find_skills, term_rx),
                     "degree": reqparse.degree(desc)})
    jobs.append({"key": "nojd", "title": "SWE", "has_jd": False, "min_yoe": None, "entry_hint": False, "kw": [], "degree": None})
    resumes = []
    for name, text in RESUMES.items():
        resumes.append({"name": name, "text": text})
    # User corrections from the skills dialog.
    resumes.append({"name": "edited", "text": RESUMES["plain"], "off": ["Go"], "added": ["Elixir", "Docker"], "years": 0.5})
    return jobs, resumes, skill_w, term_w


def dashboard_js() -> str:
    src = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    start = src.index("// ---------------------------------------------------------------- skills")
    end = src.index("let scores = new Map();")
    return src[start:end]


NODE_RUNNER = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const RealDate = Date;
globalThis.Date = class extends RealDate {
  constructor(...a) { if (a.length) super(...a); else super(input.now); }
  static now() { return input.now; }
};
const DATA = { meta: { skills: input.skills, skill_w: input.skill_w, term_w: input.term_w,
  settings: input.scoring ? { scoring: input.scoring } : undefined } };
const state = { resume: null };
__CODE__
const out = [];
for (const r of input.resumes) {
  state.resume = { text: r.text, skills: detectSkills(r.text), off: r.off || [], added: r.added || [], years: r.years ?? null };
  const have = resumeSkills();
  const years = state.resume.years != null ? state.resume.years : resumeExperience(r.text).years;
  const cache = new Map();
  const ctx = { years, degree: resumeDegree(r.text),
    hasTerm: (t) => { if (!cache.has(t)) cache.set(t, termRegex(t).test(r.text)); return cache.get(t); } };
  out.push({ name: r.name, skills: [...have].sort(), years, degree: ctx.degree,
    scores: input.jobs.map((j) => { const s = scoreJob(j, have, ctx); if (s.pct != null) { s.unjudged = tooFewToJudge(s); s.noreq = noRequirements(s); } return s; }) });
}
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the dashboard's scoring code")
class ParityTests(unittest.TestCase):
    def test_python_matches_dashboard(self):
        self.compare(None)

    def test_custom_weights_match_too(self):
        """settings.json "scoring" changes both scores the same way."""
        weights = {"experience_weight": 0.7, "prior_keywords": 1.5, "prior_credit": 0.2, "years_penalty": 0.5,
                   "min_experience_fit": 0.3, "unstated_years_fit": 0.6, "unknown_keyword_weight": 1.7}
        self.compare(weights)
        jobs, resumes, skill_w, term_w = fixtures()
        res = match.Resume(resumes[0]["text"], now_ms=NOW_MS)
        self.assertNotEqual([match.score_job(j, res, skill_w, term_w)["pct"] for j in jobs],
                            [match.score_job(j, res, skill_w, term_w, weights)["pct"] for j in jobs])

    def compare(self, weights):
        jobs, resumes, skill_w, term_w = fixtures()
        skills = json.loads((ROOT / "skills.json").read_text(encoding="utf-8"))
        payload = json.dumps({"now": NOW_MS, "skills": skills, "skill_w": skill_w, "term_w": term_w,
                              "jobs": jobs, "resumes": resumes, "scoring": weights})
        proc = subprocess.run(["node", "-e", NODE_RUNNER.replace("__CODE__", dashboard_js())],
                              input=payload, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        js = json.loads(proc.stdout)
        self.assertEqual(len(js), len(resumes))
        for want, r in zip(js, resumes):
            res = match.Resume(r["text"], match.detect_skills(r["text"]) if "off" not in r else None,
                               r.get("off"), r.get("added"), r.get("years"), now_ms=NOW_MS)
            with self.subTest(resume=r["name"]):
                self.assertEqual(sorted(res.have), want["skills"])
                self.assertEqual(res.years, want["years"])
                self.assertEqual(res.degree, want["degree"])
            for job, w in zip(jobs, want["scores"]):
                got = match.score_job(job, res, skill_w, term_w, weights)
                with self.subTest(resume=r["name"], job=job["title"]):
                    self.assertEqual(got["pct"], w["pct"])
                    if w["pct"] is None:
                        continue
                    for k in ("cov", "thin", "req", "pref", "degree", "matched", "missing", "exp"):
                        self.assertEqual(got[k], w[k], k)
                    self.assertEqual(match.too_few_to_judge(got), w["unjudged"], "too few to judge")
                    self.assertEqual(match.no_requirements(got), w["noreq"], "no requirements")
                    self.assertEqual([(i["k"], i["ok"]) for i in got["items"]],
                                     [(i["k"], i["ok"]) for i in w["items"]])
                    for a, b in zip(got["items"], w["items"]):
                        self.assertAlmostEqual(a["weight"], b["weight"], places=9)

    def test_too_few_to_judge(self):
        item = lambda k, ok, kind="req": {"k": k, "ok": ok, "kind": kind, "weight": 1.0}
        sc = {"thin": True, "exp": {"v": 0.8, "label": "years not stated"},
              "items": [item("Java", True), item("Python", True, "pref"), item("Agile", False, "pref")]}
        self.assertTrue(match.too_few_to_judge(sc), "only nice-to-haves missing")
        self.assertFalse(match.too_few_to_judge({**sc, "items": sc["items"] + [item("Go", False)]}), "a required one missing")
        self.assertFalse(match.too_few_to_judge({**sc, "exp": {"v": 0.5, "ok": False}}), "asks for more years")
        self.assertFalse(match.too_few_to_judge({**sc, "thin": False}), "enough skills to judge by the score")

    def test_no_requirements(self):
        """A posting that names only a nice-to-have or two (a platform admin role
        listing "data structures" and "basic SQL or willingness to learn") scores
        high on what little it says, but requires nothing: not picked or queued."""
        item = lambda k, ok, kind="pref": {"k": k, "ok": ok, "kind": kind, "weight": 0.5}
        sc = {"thin": True, "pct": 70, "exp": {"v": 0.8, "label": "years not stated"},
              "items": [item("Data Structures & Algorithms", True), item("SQL", True)]}
        self.assertTrue(match.no_requirements(sc))
        self.assertFalse(match.too_few_to_judge(sc), "nothing required, so nothing the resume is known to meet")
        self.assertFalse(prep.Preparer.wanted(sc, {"threshold": 60}), "70% on two nice-to-haves isn't queued")
        req = {**sc, "items": sc["items"] + [item("Java", True, "req")]}
        self.assertFalse(match.no_requirements(req))
        self.assertTrue(prep.Preparer.wanted(req, {"threshold": 60}))
        self.assertFalse(match.no_requirements({**sc, "thin": False}), "enough skills named to go by the score")

    def test_fixtures_cover_the_interesting_cases(self):
        jobs, resumes, skill_w, term_w = fixtures()
        res = match.Resume(resumes[0]["text"], now_ms=NOW_MS)
        pcts = [match.score_job(j, res, skill_w, term_w)["pct"] for j in jobs]
        self.assertIsNone(pcts[-1])                       # no description, no score
        self.assertGreater(len(set(p for p in pcts if p is not None)), 3)
        kinds = {k[2] for j in jobs for k in j["kw"]}
        self.assertTrue({"title", "req", "pref"} <= kinds, kinds)
        self.assertTrue(any(k[0] in term_w for j in jobs for k in j["kw"]), "no learned term in fixtures")
        scored = [match.score_job(j, res, skill_w, term_w) for j in jobs]
        self.assertTrue(any(s["pct"] is not None and match.no_requirements(s) for s in scored), "no posting that requires nothing")
        # Internship counted half, education skipped: 1.8y at Acme + 4 months/2.
        self.assertGreater(res.years, 1.5)


class TexTextTests(unittest.TestCase):
    def test_comments_and_commands(self):
        t = match.tex_text("Built \\textbf{APIs} in Go % secret: Rust\n\\href{https://x.io}{site}")
        self.assertIn("APIs", t)
        self.assertNotIn("Rust", t)
        self.assertIn("https://x.io", t)


if __name__ == "__main__":
    unittest.main()
