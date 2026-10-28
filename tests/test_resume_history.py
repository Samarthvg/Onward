"""Work history and education read from a master resume for filling
application forms: the tabular entry headers ("Company - Location & Dates",
"Role | \\textit{tech}", "\\textit{Degree in Field} & GPA") and each job's
bullets, plus the /api/resume/entries endpoint that serves them.

Run:  python -m unittest tests.test_resume_history
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import resume_tex as rt

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "history_master.tex"


class History(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = rt.history(rt.parse(FIXTURE.read_text(encoding="utf-8")))

    def test_work_entries_in_resume_order(self):
        self.assertEqual([w["company"] for w in self.h["work"]],
                         ["Example Labs", "Example Solutions Pvt Ltd", "Example Startup"])

    def test_header_row_splits_company_location_and_dates(self):
        w = self.h["work"][0]
        self.assertEqual(w["location"], "Dallas, TX")
        self.assertEqual(w["start"], {"month": 7, "year": 2025})
        self.assertEqual(w["end"], {"month": 1, "year": 2026})
        self.assertFalse(w["current"])

    def test_role_comes_before_the_tech_line(self):
        self.assertEqual(self.h["work"][0]["title"], "Full Stack Developer Intern")
        self.assertEqual(self.h["work"][1]["title"], "Full stack Developer")

    def test_bullets_are_plain_text(self):
        b = self.h["work"][0]["bullets"]
        self.assertEqual(len(b), 3)
        self.assertEqual(b[0], "Built offline-first estimate screens in Vue.js backed by PouchDB sync, used by 40 field staff.")
        self.assertIn("Node.js & Express", b[1])
        self.assertEqual(b[2], "Completed an internal Generative AI certification course.")
        for line in b:
            self.assertNotIn("\\", line)
            self.assertNotIn("{", line)

    def test_hfill_header_and_present(self):
        w = self.h["work"][2]
        self.assertEqual(w["title"], "Software Engineer (part-time)")
        self.assertEqual(w["location"], "")
        self.assertTrue(w["current"])
        self.assertIsNone(w["end"])

    def test_projects_are_not_work_history(self):
        self.assertNotIn("Turbofan Life Estimation", [w["company"] for w in self.h["work"]])

    def test_education(self):
        e = self.h["education"]
        self.assertEqual(len(e), 2)
        self.assertEqual(e[0]["school"], "Example State University")
        self.assertEqual(e[0]["school_full"], "College of Computing, Example State University")
        self.assertEqual((e[0]["degree"], e[0]["field"]), ("Master of Science", "Computer Science"))
        self.assertEqual((e[0]["gpa"], e[0]["gpa_scale"]), ("3.8", "4.0"))
        self.assertEqual((e[0]["start"], e[0]["end"]), ({"month": 1, "year": 2024}, {"month": 5, "year": 2026}))
        self.assertEqual((e[1]["school"], e[1]["degree"], e[1]["field"]),
                         ("Example Institute of Technology", "Bachelor of Technology", "Computer Engineering"))

    def test_skills_in_order_without_duplicates(self):
        self.assertEqual(self.h["skills"], ["Python", "Java", "TypeScript", "JavaScript", "SQL",
                                            "Node.js", "Express.js", "React.js", "Vue.js", "Angular"])

    def test_skill_in_parentheses_counts_twice(self):
        tex = FIXTURE.read_text(encoding="utf-8").replace(
            r"\textbf{Web:} & Node.js,", r"\textbf{Web:} & CI/CD (GitHub Actions, Jenkins), Node.js,")
        skills = rt.skill_list(rt.parse(tex))
        self.assertEqual(skills[skills.index("CI/CD"):skills.index("CI/CD") + 3], ["CI/CD", "GitHub Actions", "Jenkins"])

    def test_certifications_section(self):
        tex = (FIXTURE.parent / "history_certs.tex").read_text(encoding="utf-8")
        self.assertEqual(rt.certifications(rt.parse(tex)), [
            {"name": "AWS Certified Developer", "issuer": "Amazon Web Services", "issued": {"month": 3, "year": 2025}},
            {"name": "Example Cloud Practitioner", "issuer": "", "issued": {"month": None, "year": 2024}}])
        # A certification mentioned in a job's bullet is not a Certifications entry.
        self.assertEqual(self.h["certifications"], [])

    def test_projects(self):
        p = self.h["projects"]
        self.assertEqual(len(p), 1)
        self.assertEqual((p[0]["name"], p[0]["tech"]), ("Turbofan Life Estimation", "Python, PyTorch"))
        self.assertEqual(p[0]["start"], {"month": 2, "year": 2025})
        self.assertEqual(p[0]["bullets"], ["Trained an LSTM on NASA C-MAPSS data to predict remaining useful life."])

    def test_dates_variants(self):
        self.assertEqual(rt._dates("Sept 2021 – Present"), {"start": {"month": 9, "year": 2021}, "end": None, "current": True})
        self.assertEqual(rt._dates("2019 - 2022")["end"], {"month": None, "year": 2022})
        self.assertEqual(rt._dates("no dates here"), {"start": None, "end": None, "current": False})


class Endpoint(unittest.TestCase):
    """GET /api/resume/entries: the extension and dashboard may read it, websites may not."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.port = 8798
        cls.patches = [mock.patch.object(server, "PORT", cls.port),
                       mock.patch.object(server, "ORIGINS", {f"http://127.0.0.1:{cls.port}"}),
                       mock.patch.object(server, "MASTER", FIXTURE)]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()

    def get(self, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/resume/entries?url=https://example.com/x",
                                     headers={"Host": f"127.0.0.1:{self.port}", **headers})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_extension_gets_entries(self):
        status, body = self.get({"Origin": "moz-extension://abc-123", "X-Onward-Client": "autofill"})
        self.assertEqual(status, 200)
        self.assertEqual(body["work"][0]["company"], "Example Labs")
        self.assertEqual(body["education"][1]["degree"], "Bachelor of Technology")

    def test_websites_are_refused(self):
        status, _ = self.get({"Origin": "https://evil.example", "X-Onward-Client": "autofill"})
        self.assertEqual(status, 403)
        status, _ = self.get({})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
