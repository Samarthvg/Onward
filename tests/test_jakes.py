"""Resumes whose lists and bullets are commands, as in Jake Gutierrez's widely
used template (tests/fixtures/jakes_resume.tex, made-up content): read,
tailored, filled from a fact sheet and turned into form fields like any other.

Run:  python -m unittest tests.test_jakes
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import facts
import resume_import as ri
import resume_tex as rt
import tailor

TEX = (Path(__file__).parent / "fixtures" / "jakes_resume.tex").read_text(encoding="utf-8")
JD = ("Backend engineer: Python, FastAPI, PostgreSQL, Redis, REST APIs, pytest, GitHub Actions. " * 5).strip()


def state(**extra):
    return {"rewrites": {}, "tech": {}, "rows": {}, "drop_entries": set(), "drop_bullets": set(), "rank": {},
            "suggest_entries": [], "suggest_bullets": [], "style": None, **extra}


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.res = rt.parse(TEX)

    def test_its_list_and_bullet_commands_are_learned(self):
        m = rt.list_macros(TEX)
        self.assertEqual(m.items, {"resumeItem", "resumeSubItem"})
        self.assertEqual(m.starts, {"resumeItemListStart", "resumeSubHeadingListStart"})
        self.assertEqual(m.ends, {"resumeItemListEnd", "resumeSubHeadingListEnd"})

    def test_entries_are_the_bullet_lists_not_the_list_of_entries(self):
        self.assertEqual([(e.kind, len(e.bullets)) for e in self.res.entries],
                         [("experience", 4), ("experience", 3), ("project", 3), ("project", 3)],
                         "education's headings have no bullets, so they're no entries")

    def test_a_bullet_is_the_commands_argument(self):
        b = self.res.entries[0].bullets[1]
        self.assertEqual(b.tex, r"Cut report generation time by 60\% by caching query results in Redis")
        self.assertEqual(TEX[b.whole.start:b.whole.end], r"\resumeItem{" + b.tex + "}")

    def test_an_entry_starts_at_its_own_heading(self):
        e = self.res.entries[0]
        self.assertTrue(TEX[e.span.start:].startswith("\\resumeSubheading"), "not the list of entries' opening")
        self.assertEqual(e.title, "Software Engineer Intern | June 2025 – Aug. 2025 | Example Labs | Dallas, TX")

    def test_a_projects_tools_and_the_skills_rows(self):
        self.assertEqual(self.res.entries[2].tech_tex, "React, Node.js, MongoDB")
        self.assertEqual([(r.label, r.tex) for r in self.res.skill_rows][0], ("Languages", "Python, C++, JavaScript, SQL"))

    def test_form_fields(self):
        h = rt.history(self.res)
        w = h["work"][0]
        self.assertEqual((w["company"], w["title"], w["location"]), ("Example Labs", "Software Engineer Intern", "Dallas, TX"))
        self.assertEqual((w["start"], w["end"]), ({"month": 6, "year": 2025}, {"month": 8, "year": 2025}))
        self.assertEqual([(s["school"], s["degree"], s["field"], s["location"]) for s in h["education"]],
                         [("Example State University", "Master of Science", "Computer Science", "Boston, MA"),
                          ("Example Institute of Technology", "Bachelor of Science", "Computer Engineering", "Austin, TX")])
        self.assertEqual([(p["name"], p["tech"]) for p in h["projects"]][0], ("Shelf Tracker", "React, Node.js, MongoDB"))

    def test_a_resume_without_lists_says_what_it_needs(self):
        with self.assertRaisesRegex(ValueError, "resumeItem"):
            rt.parse("\\documentclass{article}\\begin{document}\\section*{Experience}Acme\\end{document}")


class EditTests(unittest.TestCase):
    def setUp(self):
        self.res = rt.parse(TEX)

    def build(self, **st):
        return rt.tidy(rt.apply(TEX, rt.build_spans(self.res, state(**st))))

    def test_a_dropped_bullet_takes_its_command(self):
        b = self.res.entries[0].bullets[3]
        tex = self.build(drop_bullets={b.id})
        self.assertNotIn("Reviewed pull requests", tex)
        self.assertNotIn(r"\resumeItem{}", tex)
        self.assertEqual(tex.count(r"\resumeItem{"), TEX.count(r"\resumeItem{") - 1)

    def test_lists_stay_balanced_when_entries_and_a_section_go(self):
        projects = {e.id for e in self.res.entries if e.kind == "project"}
        for drop in ({self.res.entries[1].id}, projects):
            tex = self.build(drop_entries=drop)
            for a, b in (("resumeItemListStart", "resumeItemListEnd"), ("resumeSubHeadingListStart", "resumeSubHeadingListEnd")):
                self.assertEqual(tex.count("\\" + a), tex.count("\\" + b), (drop, a))
        self.assertNotIn("\\section{Projects}", tex, "a section with every entry dropped goes whole")

    @unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
    def test_it_compiles_and_measures(self):
        layout = {}
        tex = rt.with_marks(rt.tidy(rt.apply(TEX, rt.build_spans(self.res, state(measure=True)))))
        self.assertEqual(rt.compile_pdf(tex, None, layout=layout), 1)
        self.assertIsNotNone(rt.line_fit(layout, self.res.entries[0].bullets[0].id), "bullets inside \\resumeItem are measured")
        projects = {e.id for e in self.res.entries if e.kind == "project"}
        self.assertEqual(rt.compile_pdf(self.build(drop_entries=projects), None), 1)


@unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
class TailorTests(unittest.TestCase):
    def test_a_rewrite_lands_inside_the_command(self):
        res = rt.parse(TEX)
        api = res.entries[0].bullets[0].id
        answer = {"bullet_rewrites": [{"id": api, "text": "Built a REST API with FastAPI and PostgreSQL in Python "
                                                          "that serves 40 internal dashboards"}],
                  "bullet_priority": [b.id for e in res.entries for b in e.bullets]}
        with tempfile.TemporaryDirectory() as tmp:
            r = tailor.tailor(TEX, {"company": "Backco", "title": "Backend Engineer"}, JD, "manual", Path(tmp),
                              manual_answer=json.dumps(answer))
            tex = (Path(tmp) / r["tex"]).read_text(encoding="utf-8")
        self.assertEqual(r["pages"], 1)
        self.assertIn(r"\resumeItem{Built a REST API with FastAPI and PostgreSQL in Python", tex)


class FactSheetTests(unittest.TestCase):
    SHEET = ("# Projects\n## Side Project | Go, Redis | 2024 | github.com/testy/side\n- Wrote a Go service.\n"
             "- Added Redis caching.\n- Wrote its tests.\n"
             "# Experience\n## Example Corp - Austin, TX | Backend Intern | Go | May 2023 - Aug 2023\n"
             "- Wrote Go handlers.\n- Added Redis caching.\n- Wrote integration tests.\n"
             "## Example Labs\n- Moved the nightly jobs to a queue.\n")

    def setUp(self):
        self.tex = facts.combine(TEX, self.SHEET)[0]

    def test_a_job_is_drawn_as_the_resumes_own_heading(self):
        self.assertIn("\\resumeSubheading\n      {Backend Intern}{May 2023 -- Aug 2023}\n      {Example Corp}{Austin, TX}", self.tex)

    def test_a_project_is_drawn_as_its_project_heading(self):
        self.assertIn(r"{\textbf{Side Project} $|$ \emph{Go, Redis}}{2024}", self.tex)

    def test_bullets_are_the_resumes_own_command(self):
        self.assertIn(r"\resumeItem{Wrote Go handlers.} " + facts.MARK, self.tex)
        res = rt.parse(self.tex)
        labs = next(e for e in res.entries if "Example Labs" in e.title)
        self.assertEqual([b.fact for b in labs.bullets], [False] * 4 + [True], "a job it has gets the bullet")
        self.assertEqual(len(res.entries), 6)

    @unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
    def test_it_compiles(self):
        self.assertGreaterEqual(rt.compile_pdf(self.tex, None), 1)

    def test_an_outline_of_it_for_a_fact_sheet(self):
        outline = ri.tex_to_outline(TEX)
        self.assertIn("## Software Engineer Intern | Example Labs | Dallas, TX | June 2025 – Aug. 2025", outline)
        self.assertIn("- Cut report generation time by 60% by caching query results in Redis", outline)


if __name__ == "__main__":
    unittest.main()
