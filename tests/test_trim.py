"""Fitting a long master resume to the page: what kind each section is, the
order things go in, page breaks and emptied sections, and a master of
several pages tailored down to one.

Run:  python -m unittest tests.test_trim
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import resume_tex as rt
import tailor

HEAD = r"""\documentclass[letterpaper,10pt]{article}
\usepackage[margin=0.6in]{geometry}
\pagestyle{empty}
\begin{document}
"""


def entry(name, bullets):
    items = "\n".join(rf"\item {b}" for b in bullets)
    return f"\\textbf{{{name}}} \\hfill 2024\n\\begin{{itemize}}\n{items}\n\\end{{itemize}}\n"


def state(res, priority=(), **extra):
    return {"rewrites": {}, "tech": {}, "rows": {}, "drop_entries": set(), "drop_bullets": set(),
            "rank": {bid: i for i, bid in enumerate(priority)}, "suggest_entries": [], "suggest_bullets": [],
            "style": None, **extra}


class SectionKindTests(unittest.TestCase):
    def test_coursework_is_projects_not_work(self):
        for title in ("PATTERN RECOGNITION \\& COMPUTER VISION COURSEWORK", "Relevant Courses", "Course Projects"):
            self.assertEqual(rt._section_kind(title), "project", title)

    def test_work_only_as_a_word(self):
        self.assertEqual(rt._section_kind("Work History"), "experience")
        self.assertEqual(rt._section_kind("Professional \\& Research Experience"), "experience")
        self.assertEqual(rt._section_kind("Networking and Frameworks"), "other")

    def test_jobs_and_projects_together_stay_experience(self):
        self.assertEqual(rt._section_kind("Experience \\& Projects"), "experience")


class TrimOrderTests(unittest.TestCase):
    TEX = HEAD + "\\section*{Experience}\n" + entry("Job", [
        "Trained PyTorch models.", "Served models with FastAPI.", "Wrote the on-call runbook.", "Ran the book club."]) \
        + "\\section*{Projects}\n" + entry("Recipes", ["Built a recipe site.", "Deployed it on Netlify."]) \
        + entry("Vision", ["Wrote an OpenCV edge detector.", "Benchmarked it on 1,000 images."]) + "\\end{document}\n"

    def setUp(self):
        self.res = rt.parse(self.TEX)
        self.ids = {rt.plain(b.tex): b.id for e in self.res.entries for b in e.bullets}
        self.ent = {e.title.split()[0]: e.id for e in self.res.entries}

    def order(self, *ranked):
        return tailor.trim_order(self.res, state(self.res, [self.ids[t] for t in ranked]))

    def test_an_unrelated_project_goes_before_a_related_job_bullet(self):
        got = self.order("Trained PyTorch models.", "Wrote an OpenCV edge detector.", "Served models with FastAPI.",
                         "Benchmarked it on 1,000 images.", "Wrote the on-call runbook.", "Ran the book club.",
                         "Built a recipe site.", "Deployed it on Netlify.")
        self.assertEqual(got[0], ("entry", self.ent["Recipes"]))
        self.assertLess(got.index(("bullet", self.ids["Ran the book club."])), got.index(("entry", self.ent["Vision"])))

    def test_jobs_keep_three_bullets_and_every_entry(self):
        got = self.order()
        self.assertEqual({x for k, x in got if k == "entry"}, {self.ent["Recipes"], self.ent["Vision"]})
        job_bullets = [x for k, x in got if k == "bullet"]
        self.assertEqual(len(job_bullets), 1, "four bullets down to three")

    def test_the_models_suggestions_go_first(self):
        st = state(self.res, suggest_bullets=[self.ids["Trained PyTorch models."]], suggest_entries=[self.ent["Vision"]])
        got = tailor.trim_order(self.res, st)
        self.assertEqual(set(got[:2]), {("bullet", self.ids["Trained PyTorch models."]), ("entry", self.ent["Vision"])})


class LayoutTests(unittest.TestCase):
    TEX = HEAD + "\\section*{Experience}\n" + entry("Job", ["Built APIs.", "Fixed bugs."]) + "\\clearpage\n" \
        + "\\section*{Coursework}\n" + entry("Course", ["Wrote filters.", "Matched images."]) \
        + "\\noindent\\textbf{Also studied:} optical flow.\n" \
        + "\\section*{Projects}\n" + entry("Tool", ["Wrote a CLI.", "Shipped it."]) + "\\end{document}\n"

    def build(self, drop=()):
        res = rt.parse(self.TEX)
        st = state(res, drop_entries={e.id for e in res.entries if e.title.split()[0] in drop})
        return rt.apply(self.TEX, rt.build_spans(res, st))

    def test_page_breaks_between_entries_go(self):
        self.assertNotIn("\\clearpage", self.build())

    def test_a_section_with_every_entry_dropped_goes_whole(self):
        tex = self.build({"Course"})
        self.assertNotIn("Coursework", tex)
        self.assertNotIn("optical flow", tex)
        self.assertIn("\\section*{Projects}", tex)

    def test_a_section_with_an_entry_left_keeps_its_heading_and_text(self):
        tex = self.build({"Tool"})
        self.assertIn("\\section*{Coursework}", tex)
        self.assertIn("optical flow", tex)
        self.assertNotIn("\\section*{Projects}", tex)


@unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
class LongMasterTests(unittest.TestCase):
    """A master of several pages, most of it coursework, with a page break of
    its own: it used to stay long, coursework being taken for work history."""

    def test_fits_one_page(self):
        filler = "Implemented and evaluated the method on a benchmark, comparing it against two baselines in Python."
        jobs = "".join(entry(f"Company{i}", [f"Built service {i} in Python and PyTorch."] + [filler] * 5) for i in range(3))
        course = "".join(entry(f"Course{i}", [f"Wrote an OpenCV filter {i}.", filler, filler]) for i in range(12))
        projects = "".join(entry(f"Project{i}", [f"Built a PHP site {i}.", filler, filler]) for i in range(8))
        tex = HEAD + "\\section*{Experience}\n" + jobs + "\\section*{Computer Vision Coursework}\n" + course \
            + "\\clearpage\n\\section*{Projects}\n" + projects + "\\end{document}\n"
        self.assertGreater(rt.compile_pdf(tex, None), 2)
        with tempfile.TemporaryDirectory() as tmp:
            r = tailor.tailor(tex, {"company": "Imgco", "title": "Imaging Engineer"},
                              ("Image processing engineer: OpenCV, PyTorch, Python, filters. " * 6).strip(), "manual",
                              Path(tmp), manual_answer=json.dumps({"bullet_priority": []}))
        self.assertEqual(r["pages"], 1)
        self.assertEqual(r["target_pages"], 1)
        dropped = {c["label"].split()[0] for c in r["changes"] if c["kind"] == "drop_entry"}
        self.assertFalse(any(d.startswith("Company") for d in dropped), "jobs are never dropped")
        self.assertTrue(any(d.startswith("Course") for d in dropped))


if __name__ == "__main__":
    unittest.main()
