"""Line fit: tailored edits that spill a few words onto a line of their own
are caught by measuring the compiled page, then fixed (lists drop what they
added, bullets are rewritten once) or put back to the master's wording.

Run: python -m unittest test_line_fit
"""

import shutil
import unittest
from unittest import mock

import resume_tex as rt
import tailor

TEX = r"""\documentclass[letterpaper,10pt]{article}
\usepackage[margin=0.75in]{geometry}
\pagestyle{empty}
\begin{document}
\section*{Experience}
\textbf{First Corp} \hfill Jan 2025 - Present \\
\textit{Software Engineer} | \textit{Python, FastAPI}
\begin{itemize}
\item Built billing APIs in Python and FastAPI on AWS for the payments team.
\item Cut p95 latency by 40\% with Redis caching. % skills: Redis
\end{itemize}
\section*{Skills}
\textbf{Languages:} Python, Go, TypeScript \\
\end{document}
"""

HAVE_TEX = shutil.which("pdflatex") or shutil.which("xelatex")


def fresh_state():
    return {"rewrites": {}, "tech": {}, "rows": {}, "drop_entries": set(), "drop_bullets": set(), "rank": {},
            "suggest_entries": [], "suggest_bullets": []}


class MarkTests(unittest.TestCase):
    def test_marks_wrap_the_text_and_leave_comments_after(self):
        self.assertEqual(rt.marked("B1", " Built APIs.\n"), r" \jrpos{B1:s}Built APIs.\jrpos{B1:e}" + "\n")
        got = rt.marked("B2", " Cut latency by 40\\%. % skills: Redis\n")
        self.assertEqual(got, r" \jrpos{B2:s}Cut latency by 40\%.\jrpos{B2:e} % skills: Redis" + "\n")
        self.assertEqual(rt.marked("B3", "  \n"), "  \n", "nothing to mark")

    def test_a_clean_build_is_the_master_and_a_measured_one_marks_everything(self):
        res = rt.parse(TEX)
        self.assertEqual(rt.apply(TEX, rt.build_spans(res, fresh_state())), TEX)
        marked = rt.with_marks(rt.apply(TEX, rt.build_spans(res, {**fresh_state(), "measure": True})))
        for mid in ("B1", "B2", "E1", "S1"):
            self.assertIn(rf"\jrpos{{{mid}:s}}", marked)
            self.assertIn(rf"\jrpos{{{mid}:e}}", marked)
        self.assertLess(marked.index(r"\providecommand\jrpos"), marked.index(r"\begin{document}"))

    def test_reading_the_log(self):
        log = ("JRPOS B1:s 100 5000000 400.0pt 12.0pt 1 -1.0pt\nJRPOS B1:e 300000 4213568 400.0pt 12.0pt 1 50.0pt\n"
               "JRPOS B2:s 100 5000000 400.0pt 12.0pt 1 -1.0pt\nJRPOS B2:e 200 5000000 400.0pt 12.0pt 2 50.0pt\n"
               # After a label: the lines below start at the paragraph's edge, 50pt in.
               "JRPOS S1:s 6553600 5000000 400.0pt 12.0pt 1 50.0pt\nJRPOS S1:e 5242880 4213568 400.0pt 12.0pt 1 50.0pt\n")
        layout = rt.read_layout(log)
        fit = rt.line_fit(layout, "B1")
        self.assertEqual((fit.lines, fit.lead), (2, 0))             # 786432sp down = one 12pt line
        self.assertAlmostEqual(fit.fill, (300000 - 100) / (400 * 65536), places=6)
        self.assertIsNone(rt.line_fit(layout, "B2"), "split across pages")
        self.assertIsNone(rt.line_fit(layout, "B9"))
        fit = rt.line_fit(layout, "S1")
        self.assertEqual(fit.lines, 2)
        self.assertAlmostEqual(fit.fill, 30 / 400)                  # 80pt - 50pt of 400pt
        self.assertAlmostEqual(fit.lead, 50 / 400)                  # the label: 100pt - 50pt

    def test_trimming_what_a_list_added(self):
        old, new = "Python, Go", "Go, Python, Kafka, Redis, CI/CD (GitHub Actions)"
        self.assertEqual(rt.trim_appended(old, new, 30), ("Go, Python, Kafka", ["Redis", "CI/CD (GitHub Actions)"]))
        self.assertEqual(rt.trim_appended(old, new, 5)[1], ["CI/CD (GitHub Actions)"])
        self.assertIsNone(rt.trim_appended(old, new, 80), "the original items stay")


class TargetTests(unittest.TestCase):
    def test_a_few_spilled_words_aim_one_line_shorter(self):
        lo, hi = tailor.refit_targets("x" * 110, rt.Fit(2, 0.1, 0))     # about 100 characters a line
        self.assertTrue(85 <= lo < hi <= 100, (lo, hi))
        lo, hi = tailor.refit_targets("x" * 150, rt.Fit(2, 0.5, 0))     # half a line over: fill it instead
        self.assertTrue(180 <= lo < hi <= 200, (lo, hi))
        lo, hi = tailor.refit_targets("x" * 100, rt.Fit(2, 0.2, 0.2))   # a label takes a fifth of the first line
        self.assertTrue(65 <= lo < hi <= 80, (lo, hi))


@unittest.skipUnless(HAVE_TEX, "LaTeX is needed")
class CompiledTests(unittest.TestCase):
    """Against the real engine: the marks change nothing, and spills are fixed."""

    def setUp(self):
        self.res = rt.parse(TEX)
        self.state = fresh_state()
        self.layout = {}

    def measure(self):
        tex = rt.with_marks(rt.apply(TEX, rt.build_spans(self.res, {**self.state, "measure": True})))
        return rt.compile_pdf(tex, layout=self.layout)

    def per_line(self, mid, text):
        """Characters a full line of this piece holds, from a one-line measurement."""
        fit = rt.line_fit(self.layout, mid)
        self.assertEqual(fit.lines, 1)
        return len(rt.plain(text)) / (fit.fill - fit.lead)

    def test_measures_every_piece(self):
        self.assertEqual(self.measure(), 1)
        for mid in ("B1", "B2", "E1", "S1"):
            self.assertEqual(rt.line_fit(self.layout, mid)[0], 1, mid)

    def test_a_spilled_skills_row_drops_what_it_added(self):
        self.measure()
        old = self.res.skill_rows[0].tex
        room = self.per_line("S1", old)
        extra = ["Kafka", "Redis", "Docker", "Kubernetes", "Terraform", "GraphQL", "Postgres", "Rust", "Swift", "Scala"]
        new = old
        while len(rt.plain(new)) < room + 8:
            new += ", " + extra[len(new.split(",")) % len(extra)] + str(len(new))
        self.state["rows"]["S1"] = new
        changes = [rt.Change("skills", "S1", "Languages", rt.plain(old), rt.plain(new))]
        self.measure()
        self.assertEqual(rt.line_fit(self.layout, "S1")[0], 2, "the test row spills")
        tailor.fit_lines(self.res, self.state, changes, self.layout, self.measure, "manual", "")
        self.assertEqual(rt.line_fit(self.layout, "S1")[0], 1)
        self.assertTrue(self.state["rows"]["S1"].startswith(old.strip()))
        self.assertEqual(changes[0].status, "applied")
        self.assertIn("so it doesn't spill", changes[0].reason)

    def spill_bullet(self):
        self.measure()
        b = self.res.entries[0].bullets[0]
        room = self.per_line("B1", b.tex)
        words = "with the payments and billing teams across regions"
        new = b.tex.rstrip().rstrip(".")
        while len(rt.plain(new)) < room + 6:
            new += " " + words.split()[len(new) % 8]
        self.state["rewrites"]["B1"] = new + "."
        self.changes = [rt.Change("bullet", "B1", "First Corp", rt.plain(b.tex), rt.plain(new))]
        self.measure()
        fit = rt.line_fit(self.layout, "B1")
        self.assertEqual(fit.lines, 2, "the test bullet spills")
        self.assertLess(fit.fill, tailor.MIN_FILL)

    def test_a_spilled_bullet_is_refit_once(self):
        self.spill_bullet()
        short = "Built billing APIs in Python and FastAPI on AWS."
        with mock.patch.object(tailor, "ask", return_value=({"bullets": [{"id": "B1", "text": short}]}, "m", 0.01)) as ask:
            cost = tailor.fit_lines(self.res, self.state, self.changes, self.layout, self.measure, "claude-api", "")
        self.assertEqual(ask.call_count, 1)
        self.assertIn("TARGET", ask.call_args.args[2])
        self.assertEqual((self.state["rewrites"]["B1"], cost), (short, 0.01))
        self.assertEqual(rt.line_fit(self.layout, "B1")[0], 1)
        self.assertEqual((self.changes[0].status, self.changes[0].after), ("applied", short))

    def test_a_refit_that_adds_a_claim_or_still_spills_goes_back_to_the_master(self):
        self.spill_bullet()
        made_up = "Built billing APIs in Python and FastAPI on AWS, saving 75% of costs."
        with mock.patch.object(tailor, "ask", return_value=({"bullets": [{"id": "B1", "text": made_up}]}, "m", None)):
            tailor.fit_lines(self.res, self.state, self.changes, self.layout, self.measure, "chatgpt", "")
        self.assertNotIn("B1", self.state["rewrites"], "a new number fails the honesty check")
        self.assertEqual((self.changes[0].status, self.changes[0].reason), ("rejected", tailor.SPILLED))

    def test_manual_answers_go_back_to_the_master(self):
        self.spill_bullet()
        tailor.fit_lines(self.res, self.state, self.changes, self.layout, self.measure, "manual", "")
        self.assertNotIn("B1", self.state["rewrites"])
        self.assertEqual(rt.line_fit(self.layout, "B1")[0], 1)


if __name__ == "__main__":
    unittest.main()
