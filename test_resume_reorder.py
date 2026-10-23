"""Project entries are reordered by relevance inside their own section; work
experience keeps its date order; the result still compiles to one page.
Also: an applicant's instruction reaches the prompt of every backend.

Run:  python -m unittest test_resume_reorder
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import resume_tex as rt
import tailor

TEX = r"""\documentclass[letterpaper,10pt]{article}
\usepackage[margin=0.6in]{geometry}
\pagestyle{empty}
\begin{document}
\section*{Experience}
\textbf{First Corp} \hfill Jan 2025 - Present \\
\textit{Software Engineer}
\begin{itemize}
\item Built billing APIs in \textbf{Python} and FastAPI on AWS.
\item Cut p95 latency by 40\% with Redis caching.
\end{itemize}
\textbf{Second Corp} \hfill Jun 2022 - Aug 2023 \\
\textit{Software Engineer Intern}
\begin{itemize}
\item Wrote React dashboards for the ops team.
\item Added Jest tests to the checkout flow.
\end{itemize}
\section*{Personal Projects}
\textbf{Alpha Tool} | \textit{Go, gRPC}
\begin{itemize}
\item Wrote a gRPC load tester in Go.
\item Published it with docs and examples.
\end{itemize}
\textbf{Beta Site} | \textit{React, TypeScript}
\begin{itemize}
\item Built a recipe site in React and TypeScript.
\item Deployed it on Netlify with CI.
\end{itemize}
\textbf{Gamma ML} | \textit{PyTorch, Python}
\begin{itemize}
\item Trained a PyTorch model to classify bird songs.
\item Served it behind a FastAPI endpoint.
\end{itemize}
\section*{Research Projects}
\textbf{Delta Study} | \textit{Python, Pandas}
\begin{itemize}
\item Analyzed transit data with Pandas.
\item Wrote up the findings for a class.
\end{itemize}
\textbf{Epsilon Paper} | \textit{PyTorch}
\begin{itemize}
\item Reproduced a PyTorch paper on sequence models.
\item Compared three baselines.
\end{itemize}
\section*{Skills}
\textbf{Languages:} Python, Go, TypeScript \\
\end{document}
"""

JD = ("We are hiring a machine learning engineer to train PyTorch models and serve them with FastAPI. " * 5).strip()


def answer(priority, **extra):
    out = {"bullet_rewrites": [], "tech_lines": [], "skills_rows": [], "bullet_priority": priority,
           "drop_bullets": [], "drop_entries": [], "jd_keywords": [], "summary": "Put ML first."}
    out.update(extra)
    return json.dumps(out)


def order(tex, names):
    pos = {n: tex.index(n) for n in names}
    return sorted(names, key=pos.get)


class ReorderTests(unittest.TestCase):
    def setUp(self):
        self.res = rt.parse(TEX)
        self.b = {rt.plain(b.tex)[:12]: b.id for e in self.res.entries for b in e.bullets}

    def ids(self, *starts):
        return [self.b[s[:12]] for s in starts]

    def state(self, priority):
        _, _, state = rt.plan_edits(self.res, json.loads(answer(priority)), JD)
        return state

    def test_projects_move_within_their_section_only(self):
        # Most relevant first: Gamma, then Epsilon, then the second job's bullets.
        pri = self.ids("Trained a PyTorch", "Reproduced a PyTorch", "Wrote React dashboards", "Built billing APIs")
        log = []
        tex = rt.apply(TEX, rt.build_spans(self.res, self.state(pri), log))
        self.assertEqual(order(tex, ["Gamma ML", "Alpha Tool", "Beta Site"]), ["Gamma ML", "Alpha Tool", "Beta Site"])
        self.assertEqual(order(tex, ["Epsilon Paper", "Delta Study"]), ["Epsilon Paper", "Delta Study"])
        # Experience keeps its date order even though Second Corp ranks higher.
        self.assertEqual(order(tex, ["First Corp", "Second Corp"]), ["First Corp", "Second Corp"])
        # Sections stay where they were.
        self.assertLess(tex.index("Personal Projects"), tex.index("Gamma ML"))
        self.assertLess(tex.index("Beta Site"), tex.index("Research Projects"))
        moves = [c for c in log if c.kind == "reorder_entries"]
        self.assertEqual([c.label for c in moves], ["Personal Projects", "Research Projects"])
        self.assertTrue(moves[0].after.startswith("Gamma ML"))
        # Nothing lost or duplicated: same text, different order.
        self.assertEqual(sorted(rt.plain(tex).split()), sorted(rt.plain(TEX).split()))

    def test_entry_keeps_its_own_edits_when_moved(self):
        gamma = self.ids("Trained a PyTorch")[0]
        out = json.loads(answer([gamma], bullet_rewrites=[{"id": gamma, "text": "Trained a PyTorch classifier for bird songs."}],
                                tech_lines=[{"id": "E5", "text": "Python, PyTorch"}]))
        spans, changes, state = rt.plan_edits(self.res, out, JD)
        tex = rt.apply(TEX, rt.build_spans(self.res, state))
        self.assertIn("Trained a PyTorch classifier for bird songs.", tex)
        self.assertIn(r"\textbf{Gamma ML} | \textit{Python, PyTorch}", tex)
        self.assertLess(tex.index("Gamma ML"), tex.index("Alpha Tool"))

    def test_no_priority_no_move(self):
        log = []
        tex = rt.apply(TEX, rt.build_spans(self.res, self.state([]), log))
        self.assertEqual(tex, TEX)
        self.assertFalse([c for c in log if c.kind == "reorder_entries"])

    def test_dropped_project_leaves_the_rest_in_order(self):
        pri = self.ids("Trained a PyTorch", "Built a recipe")
        state = self.state(pri)
        state["drop_entries"].add("E3")       # Alpha Tool
        tex = rt.apply(TEX, rt.build_spans(self.res, state))
        self.assertNotIn("Alpha Tool", tex)
        self.assertEqual(order(tex, ["Gamma ML", "Beta Site"]), ["Gamma ML", "Beta Site"])

    @unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
    def test_tailor_compiles_to_one_page(self):
        pri = self.ids("Trained a PyTorch", "Reproduced a PyTorch")
        with tempfile.TemporaryDirectory() as tmp:
            r = tailor.tailor(TEX, {"company": "Mlco", "title": "ML Engineer"}, JD, "manual", Path(tmp),
                              manual_answer=answer(pri))
            tex = (Path(tmp) / r["tex"]).read_text(encoding="utf-8")
            self.assertTrue((Path(tmp) / r["pdf"]).exists())
        self.assertEqual(r["pages"], 1)
        self.assertLess(tex.index("Gamma ML"), tex.index("Alpha Tool"))
        self.assertIn("reorder_entries", [c["kind"] for c in r["changes"]])


class InstructionTests(unittest.TestCase):
    JOB = {"company": "Mlco", "title": "ML Engineer", "instruction": "Don't touch the   Beta Site project."}

    def test_reaches_every_backend(self):
        res = rt.parse(TEX)
        seen = {}
        fake = lambda name: (lambda *a, **k: seen.__setitem__(name, json.dumps(a, default=str) + json.dumps(k, default=str))
                             or ({}, name))
        tailor._codex_json, old_codex = fake("chatgpt"), tailor._codex_json
        tailor._claude_json, old_claude = fake("claude-code"), tailor._claude_json
        try:
            for backend in ("chatgpt", "claude-code"):
                tailor.ask(backend, *tailor.tailor_prompt(res, self.JOB, JD), tailor.SCHEMA, "tailor")
        finally:
            tailor._codex_json, tailor._claude_json = old_codex, old_claude
        seen["manual"] = tailor.build_manual_prompt(TEX, self.JOB, JD)
        for name, prompt in seen.items():
            self.assertIn("Don't touch the Beta Site project.", prompt.replace("\\u2019", "'"), name)
        # The API backends send the job block as the user message.
        self.assertIn("Beta Site project", tailor._job_block(self.JOB, JD))

    def test_absent_without_instruction(self):
        self.assertNotIn("REQUEST", tailor._job_block({"company": "x", "title": "y"}, JD))
        self.assertEqual(len(tailor.clean_instruction("x" * 2000)), tailor.MAX_INSTRUCTION)


class SkillsRowTests(unittest.TestCase):
    JD = ("Build Java services on Azure, with unit testing and JUnit. " * 6).strip()

    def plan(self, row, confirmed=()):
        res = rt.confirm(rt.parse(TEX), confirmed)
        _, changes, state = rt.plan_edits(res, json.loads(answer([], skills_rows=[{"id": "S1", "text": row}])), self.JD)
        return next((c for c in changes if c.kind == "skills"), None), state

    def test_a_category_name_is_left_out_and_the_tools_kept(self):
        c, state = self.plan("Python, Go, TypeScript, Testing")
        self.assertEqual((c, state["rows"]), (None, {}), "without Testing nothing changed")
        c, state = self.plan("Python, Go, TypeScript, Testing, Java", confirmed=["Java"])
        self.assertEqual((c.status, state["rows"]["S1"]), ("applied", "Python, Go, TypeScript, Java"))
        self.assertIn("left out Testing", c.reason)

    def test_skills_confirmed_under_review_skills_may_be_listed(self):
        c, _ = self.plan("Python, Go, TypeScript, Azure")
        self.assertEqual(c.status, "rejected")
        c, state = self.plan("Python, Go, TypeScript, Azure", confirmed=["Azure"])
        self.assertEqual((c.status, state["rows"]["S1"]), ("applied", "Python, Go, TypeScript, Azure"))
        self.assertIn("Azure", rt.describe(rt.confirm(rt.parse(TEX), ["Azure"])))

    def test_confirmed_skills_stay_out_of_bullets(self):
        res = rt.confirm(rt.parse(TEX), ["Azure"])
        _, changes, _ = rt.plan_edits(res, json.loads(answer([], bullet_rewrites=[
            {"id": "B1", "text": r"Built billing APIs in 	extbf{Python} and FastAPI on Azure."}])), self.JD)
        self.assertEqual(changes[0].status, "rejected")


if __name__ == "__main__":
    unittest.main()
