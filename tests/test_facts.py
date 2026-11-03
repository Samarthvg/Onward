"""The fact sheet: read from its outline, drawn into a resume in the resume's
own pattern, filled from a longer resume, and tailored from: one page in the
resume's layout, with no more bullets than an entry of its kind shows.

Run:  python -m unittest tests.test_facts
"""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import facts
import resume_import as ri
import resume_tex as rt
import tailor

TEMPLATE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[margin=0.5in]{geometry}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\setlist[itemize]{leftmargin=.6cm, topsep=0pt, itemsep=0pt}
\begin{document}
\section*{EDUCATION}
\textbf{Example State University} - Boston, USA \hfill Jan 2024 - May 2026 \\
Master of Science in Computer Science
\section*{TECHNICAL SKILLS}
\textbf{Languages:} Python, Java, SQL \\
\section*{WORK EXPERIENCE}
\begin{tabular*}{\textwidth}{@{}l@{\extracolsep{\fill}}r@{}}
\href{https://example.com/}{\textbf{Example Labs}} - Dallas, TX & Jul 2025 - Jan 2026\\
Full Stack Developer Intern | \textit{Vue.js, Node.js, REST}
\end{tabular*}
\begin{itemize}
    \item Built offline-first estimate screens in Vue.js used by 40 field staff.
    \item Extended REST endpoints in Node.js for quote approvals, with unit tests.
\end{itemize}
\vspace{0.08cm}
\begin{tabular*}{\textwidth}{@{}l@{\extracolsep{\fill}}r@{}}
\href{https://example.org/}{\textbf{Example Solutions}} - Gujarat, India & Jun 2022 - Aug 2023\\
Full Stack Developer | \textit{React.js, MongoDB}
\end{tabular*}
\begin{itemize}
    \item Shipped React.js dashboards for 3 client products.
    \item Wrote Node.js services over MongoDB with 85\% test coverage.
\end{itemize}
\section*{PROJECTS}
\href{https://example.com/rul}{\textbf{Turbofan Life Estimation}} | \textit{Python, PyTorch} \hfill Feb - Apr 2026
\begin{itemize}
  \item Trained an LSTM on NASA C-MAPSS data to predict remaining useful life.
  \item Compared Ridge Regression and XGBoost baselines on engineered features.
\end{itemize}
\vspace{0.08cm}
\href{https://example.com/lms}{\textbf{Kanbas -- Learning Management System}} | \textit{MERN Stack, AWS S3} \hfill May - Aug 2024
\begin{itemize}
  \item Shipped an LMS on the MERN stack with role-based authentication.
  \item Stored course files on AWS S3 behind signed URLs.
\end{itemize}
\end{document}
"""

SHEET = """# Projects
## Edge Detector | C++, OpenCV | Spring 2025 | github.com/testy/edges
- Wrote Sobel X/Y filters and gradient magnitude in C++ with OpenCV.
- Measured the filters on 200 images against OpenCV's own.
- Tuned the kernel sizes for noisy webcam frames.
# Work Experience
## Example Labs
- Debugged permission rules for the Finance and Resource Manager roles.
# Coursework
## Texture Retrieval | C++, Law's filters | Fall 2024
- Matched images by texture with Law's filter banks and histograms.
- Ranked results by histogram intersection over 1,000 images.
- Compared the filter bank against plain color histograms.
# Skills
Vision: OpenCV, Gabor filters
"""


class ParseTests(unittest.TestCase):
    def test_header_parts_are_told_apart(self):
        f = facts._header("Work Experience", "Acme - Boston, MA | Software Engineer | Go, SQL | May 2024 - Aug 2024 | acme.com")
        self.assertEqual((f.name, f.place, f.title, f.tools, f.dates, f.link),
                         ("Acme", "Boston, MA", "Software Engineer", "Go, SQL", "May 2024 - Aug 2024", "https://acme.com"))

    def test_a_projects_dash_is_part_of_its_name(self):
        self.assertEqual(facts._header("Projects", "Kanbas -- Course LMS | React, Node | 2024").name, "Kanbas -- Course LMS")

    def test_month_ranges_and_links_with_queries(self):
        f = facts._header("Projects", "Deepfakes | Python, PyTorch | Mar – Apr 2025 | https://drive.example.com/f?id=1&x=2")
        self.assertEqual((f.dates, f.link), ("Mar – Apr 2025", "https://drive.example.com/f?id=1&x=2"))

    def test_lines_it_cant_place_are_reported(self):
        sheet = facts.parse("A loose line\n# Projects\n- A bullet with no entry\n## Empty | 2024\n")
        self.assertEqual(sheet.entries, [])
        self.assertEqual(len(sheet.unused), 3)


class CombineTests(unittest.TestCase):
    def setUp(self):
        self.tex, self.report = facts.combine(TEMPLATE, SHEET)
        self.res = rt.parse(self.tex)
        self.by_name = {facts._name_of(self.res, e): e for e in self.res.entries}

    def block(self, name):
        e = self.by_name[name]
        return self.tex[e.span.start:e.span.end]

    def test_a_new_project_is_drawn_in_the_resumes_own_pattern(self):
        self.assertIn(r"\href{https://github.com/testy/edges}{\textbf{Edge Detector}} | \textit{C++, OpenCV} \hfill Spring 2025",
                      self.block("Edge Detector"))
        e = self.by_name["Edge Detector"]
        self.assertEqual((e.kind, e.fact), ("project", True))
        self.assertTrue(all(b.fact for b in e.bullets))
        self.assertNotIn("%", e.bullets[0].tex, "the mark stays out of the bullet's own text")

    def test_it_goes_after_the_sections_own_entries_with_their_spacing(self):
        lms = self.tex.index("Kanbas")
        edge = self.tex.index("Edge Detector")
        self.assertLess(lms, edge)
        self.assertIn("\\end{itemize}\n" + facts.MARK + "\n\\vspace{0.08cm}\n\\href{https://github.com/testy/edges}", self.tex,
                      "spaced from the entry before as the section's own entries are")

    def test_a_job_on_the_resume_gets_the_bullets(self):
        e = self.by_name["Example Labs"]
        self.assertFalse(e.fact)
        self.assertEqual([b.fact for b in e.bullets], [False, False, True])
        self.assertEqual(self.report["bullets"], {"Example Labs": 1})

    def test_the_resume_keeps_its_own_sections(self):
        self.assertNotIn("COURSEWORK", self.tex, "coursework goes with the projects")
        projects = self.tex.index("\\section*{PROJECTS}")
        self.assertGreater(self.tex.index("Texture Retrieval"), projects)
        self.assertEqual(self.by_name["Texture Retrieval"].kind, "project")
        self.assertEqual(len(rt.SECTION.findall(self.tex)), len(rt.SECTION.findall(TEMPLATE)))

    def test_a_resume_without_projects_gets_one_projects_section(self):
        no_projects = TEMPLATE[:TEMPLATE.index("\\section*{PROJECTS}")] + "\\end{document}\n"
        tex = facts.combine(no_projects, SHEET)[0]
        self.assertEqual(tex.count("\\section*{PROJECTS}"), 1, "the projects and the coursework together, in its heading style")
        self.assertLess(tex.index("Edge Detector"), tex.index("Texture Retrieval"))

    def test_skills_become_skills_the_rows_may_use(self):
        self.assertIn("OpenCV", self.res.inventory)
        self.assertIn("Gabor filters", self.tex)
        self.assertNotIn("Gabor filters", rt.plain(self.tex), "they don't print by themselves")

    def test_a_long_tool_list_is_cut_to_the_resumes_widest_line(self):
        sheet = "# Projects\n## A Project With A Long Name | " + ", ".join(f"Tool{i}" for i in range(30)) + " | 2024\n- Did it.\n- Again.\n- Done.\n"
        tex = facts.combine(TEMPLATE, sheet)[0]
        tools = re.search(r"\\textbf\{A Project With A Long Name\} \| \\textit\{([^}]*)\}", tex).group(1)
        self.assertTrue(tools.startswith("Tool0, Tool1"))
        self.assertLess(len(tools), 60)

    def test_a_short_new_entry_is_reported(self):
        tex, report = facts.combine(TEMPLATE, "# Projects\n## Thin | Go | 2024\n- One.\n- Two.\n"
                                              "# Work Experience\n## Example Labs\n- Just one more for a job it has.\n")
        self.assertEqual(report["short"], ["Thin"], "tailoring leaves it out; drawn in, it still counts as evidence")
        self.assertEqual(facts.combine(TEMPLATE, "# Projects\n## Thin | Go | 2024\n- One.\n- Two.\n", min_bullets=2)[1]["short"], [])
        self.assertIn("Just one more", tex, "bullets for an entry the resume has need no minimum")


    def test_text_is_escaped(self):
        tex = facts.combine(TEMPLATE, "# Projects\n## R&D 100% | C#, $x_1$ | 2024\n- Used \\input and {braces}.\n- B.\n- C.\n")[0]
        self.assertIn(r"R\&D 100\%", tex)
        self.assertNotIn("\\input and", tex)

    @unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
    def test_it_compiles(self):
        self.assertGreaterEqual(rt.compile_pdf(self.tex, None), 1)


class LimitTests(unittest.TestCase):
    """How many bullets an entry shows follows the resume it's tailored from."""

    def limits(self, job_bullets, project_bullets):
        entry = lambda name, n: f"\\textbf{{{name}}} \\hfill 2024\n\\begin{{itemize}}\n" + "".join(
            f"\\item Point {i}.\n" for i in range(n)) + "\\end{itemize}\n"
        tex = (f"\\documentclass{{article}}\\begin{{document}}\n\\section*{{Experience}}\n{entry('Job', job_bullets)}"
               f"\\section*{{Projects}}\n{entry('Thing', project_bullets)}\\end{{document}}\n")
        return tailor.bullet_limits(rt.parse(tex))

    def test_as_many_as_the_resume_shows(self):
        self.assertEqual(self.limits(5, 4), (tailor.min_bullets(), {"experience": 5, "project": 4}))

    def test_never_under_the_fewest_nor_over_the_most(self):
        self.assertEqual(self.limits(2, 2)[1], {"experience": tailor.min_bullets(), "project": tailor.min_bullets()})
        self.assertEqual(self.limits(9, 9)[1], {"experience": rt.MOST_BULLETS, "project": rt.MOST_BULLETS})


MACRO_TEMPLATE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[margin=0.6in]{geometry}
\usepackage[hidelinks]{hyperref}
\newcommand{\job}[3]{\textbf{#1} \hfill #2 \\ \textit{#3}}
\newcommand{\project}[3]{\textbf{#1} | \textit{#2} \hfill #3}
\newcommand{\smallgap}{\vspace{2pt}}
\pagestyle{empty}
\begin{document}
\section*{Experience}
\job{\href{https://acme.example}{Acme} -- Dallas, TX}{Jul 2025 -- Jan 2026}{Developer Intern \textbar{} Vue.js, Node.js}
\begin{itemize}
  \item Built estimate screens in Vue.js.
  \item Extended REST endpoints in Node.js.
  \item Fixed sync bugs in the offline store.
\end{itemize}
\section*{Projects}
\project{\href{https://example.com/rul}{Turbofan Life}}{Python, PyTorch}{Feb -- Apr 2026}
\begin{itemize}
  \item Trained an LSTM on NASA C-MAPSS data.
  \item Compared Ridge Regression and XGBoost.
  \item Plotted hidden states over time.
\end{itemize}
\smallgap
\project{Second Thing}{Go}{2024}
\begin{itemize}
  \item Wrote a Go service.
  \item Ran it on Fly.io.
  \item Added metrics.
\end{itemize}
\end{document}
"""


class MacroPatternTests(unittest.TestCase):
    """A resume whose entry headers are macros: fact sheet entries follow them."""

    def setUp(self):
        sheet = ("# Projects\n## Edge Detector | C++, OpenCV | Spring 2025\n- A.\n- B.\n- C.\n"
                 "# Work Experience\n## Beta Robotics - Boston, MA | Software Engineer | Python, ROS | May 2024 - Aug 2024 | beta.example\n"
                 "- D.\n- E.\n- F.\n## Plain Co | 2023\n- G.\n- H.\n- I.\n")
        self.tex = facts.combine(MACRO_TEMPLATE, sheet)[0]

    def test_a_project_follows_the_project_macro(self):
        self.assertIn(r"\project{{Edge Detector}}{C++, OpenCV}{Spring 2025}", self.tex, "the pattern's link goes, none given")
        self.assertIn("\\smallgap\n\\project{{Edge Detector}}", self.tex, "spaced as the section's entries are")

    def test_a_job_follows_the_job_macro(self):
        self.assertIn(r"\job{\href{https://beta.example}{Beta Robotics} -- Boston, MA}{May 2024 -- Aug 2024}"
                      r"{Software Engineer \textbar{} Python, ROS}", self.tex, "dates in the resume's dash")

    def test_fields_it_lacks_go_with_their_separators(self):
        self.assertIn(r"\job{{Plain Co}}{2023}{}", self.tex)

    @unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
    def test_it_compiles(self):
        self.assertGreaterEqual(rt.compile_pdf(self.tex, None), 1)


class ExtrasTests(unittest.TestCase):
    def test_only_what_the_resume_lacks(self):
        longer = """# Education
## Example State University | 2024
# Work Experience
## Example Labs - Dallas, TX | Full Stack Developer Intern | Jul 2025 - Jan 2026
- Built offline-first estimate screens in Vue.js for 40 field staff.
- Debugged permission rules for the Finance and Resource Manager roles.
# Projects
## Kanbas -- Learning Management System | MERN | 2024
- Shipped an LMS on the MERN stack with role-based authentication.
## Edge Detector | C++, OpenCV | 2025
- Wrote Sobel filters in C++.
# Skills
Languages: Python, Go, SQL
"""
        out = facts.extras(TEMPLATE, longer)
        self.assertNotIn("Example State University", out, "education stays out")
        self.assertNotIn("estimate screens", out, "a bullet the resume has, reworded, stays out")
        self.assertIn("Debugged permission rules", out)
        self.assertNotIn("Kanbas", out, "an entry with nothing new stays out")
        self.assertIn("## Edge Detector", out)
        self.assertIn("Languages: Go", out, "only the skills the resume doesn't list")


@unittest.skipUnless(shutil.which("pdflatex"), "LaTeX is needed")
class TailorTests(unittest.TestCase):
    """The template with a big fact sheet, tailored: one page in its layout."""

    def run_tailor(self, sheet, priority=lambda res: []):
        combined = facts.combine(TEMPLATE, sheet)[0]
        res = rt.parse(combined)
        with tempfile.TemporaryDirectory() as tmp:
            r = tailor.tailor(combined, {"company": "Imgco", "title": "Imaging Engineer"},
                              ("Image processing engineer: OpenCV, C++, PyTorch, Python, filters. " * 6).strip(),
                              "manual", Path(tmp), manual_answer=json.dumps({"bullet_priority": priority(res)}), level="none")
            tex = (Path(tmp) / r["tex"]).read_text(encoding="utf-8")
        return r, rt.parse(tex), tex

    def test_one_page_with_capped_bullets(self):
        filler = "Implemented and evaluated the method on a benchmark, comparing it against two baselines in Python."
        sheet = "# Work Experience\n## Example Labs\n" + "".join(f"- {filler} ({i})\n" for i in range(5)) + "# Projects\n" + "".join(
            f"## Vision Project {i} | C++, OpenCV | 2025\n- Wrote an OpenCV filter {i} in C++.\n" + f"- {filler}\n" * 3
            for i in range(10))
        hot = lambda res: [b.id for e in res.entries for b in e.bullets if "OpenCV" in b.tex]
        r, out, tex = self.run_tailor(sheet, hot)
        self.assertEqual(r["pages"], 1)
        _, most = tailor.bullet_limits(rt.parse(TEMPLATE))
        for e in out.entries:
            self.assertLessEqual(len(e.bullets), most[e.kind], e.title)
        self.assertIn("Vision Project", tex, "the fact sheet's relevant projects make the page")
        self.assertTrue(any(c["kind"] == "fact_entry" for c in r["changes"]))
        self.assertTrue(any(c["kind"] == "drop_entry" and c["fact"] for c in r["changes"]))
        self.assertTrue(any("most relevant" in c["reason"] for c in r["changes"] if c["kind"] == "drop_bullet"))

    def test_every_entry_keeps_three_bullets(self):
        sheet = "# Projects\n" + "".join(f"## Vision Project {i} | C++, OpenCV | 2025\n" + "".join(
            f"- Wrote OpenCV filter {i}.{j} in C++ and measured it on test images.\n" for j in range(3)) for i in range(8)) \
            + "## Thin Project | C++, OpenCV | 2025\n- Wrote one OpenCV filter in C++.\n- Measured it.\n"
        r, out, tex = self.run_tailor(sheet)
        self.assertEqual(r["pages"], 1)
        # The resume's own entries with fewer than three keep all they have.
        had = {facts._name_of(res, e): len(e.bullets) for res in [rt.parse(TEMPLATE)] for e in res.entries}
        for e in out.entries:
            name = facts._name_of(out, e)
            self.assertGreaterEqual(len(e.bullets), min(tailor.min_bullets(), had.get(name, 99)), name)
        self.assertNotIn("Thin Project", tex, "a fact sheet entry with fewer than the fewest stays out")

    def test_a_dropped_entry_leaves_no_paragraph_break(self):
        sheet = "# Projects\n" + "".join(f"## Filler {i} | Go | 2024\n- Wrote a Go service {i}.\n- Ran it.\n- Shipped it.\n" for i in range(12))
        # Rank the template's second project last, so it's what goes between two that stay.
        def pri(res):
            lms = [b.id for e in res.entries if "Kanbas" in e.title for b in e.bullets]
            return [b.id for e in res.entries for b in e.bullets if b.id not in lms] + lms
        r, out, tex = self.run_tailor(sheet, pri)
        body = tex[tex.index("\\section*{PROJECTS}"):]
        self.assertNotRegex(body, r"\n[ \t]*\n[ \t]*\\(?:href|textbf)", "no blank line before an entry: it would be indented")
        self.assertNotRegex(body, r"\\vspace\{[^}]*\}\s*\n%\s*\n\\vspace", "one gap where an entry went, not two")


class MacroHeaderTests(unittest.TestCase):
    def test_a_macro_headers_arguments_are_its_parts(self):
        cells = ri.header_cells("\\smallgap\n\n\\job{\\href{https://x.com}{Acme} -- Dallas, TX}{Jul 2025 -- Jan 2026}"
                                "{Engineer \\textbar{} Go, SQL}\n")
        self.assertEqual(cells, ["Acme – Dallas, TX", "Jul 2025 – Jan 2026", "Engineer | Go, SQL"])


if __name__ == "__main__":
    unittest.main()
