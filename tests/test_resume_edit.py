"""Editing a tailored resume by hand (resume_edit.py, /api/tailored/edit).

Run:  python -m unittest tests.test_resume_edit
"""

import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import config
import prep
import radar
import resume_edit as ed
import resume_tex as rt
import server

FIXTURES = Path(__file__).parent / "fixtures"
JAKE = (FIXTURES / "jakes_resume.tex").read_text(encoding="utf-8")
PLAIN = (FIXTURES / "history_master.tex").read_text(encoding="utf-8")
HAS_TEX = bool(shutil.which("pdflatex"))


def text(t, latex=False):
    return {"text": t, "latex": latex}


class TextTests(unittest.TestCase):
    def test_latex_reads_as_typed(self):
        self.assertEqual(ed.to_text(r"Cut costs 40\% for R\&D in \textbf{Go} and \textit{Rust}, 2023--2024"),
                         "Cut costs 40% for R&D in **Go** and *Rust*, 2023\u20132024")

    def test_typed_text_goes_back(self):
        self.assertEqual(ed.from_text("Cut costs 40% for R&D in **Go** and *Rust*, 2023\u20132024"),
                         r"Cut costs 40\% for R\&D in \textbf{Go} and \textit{Rust}, 2023--2024")
        self.assertEqual(ed.from_text("Ran C# and 2 * 3 jobs,\n  twice"), r"Ran C\# and 2 * 3 jobs, twice",
                         "a lone star stays a star; line breaks are spaces")

    def test_what_text_cant_hold_stays_latex(self):
        for tex in (r"Earned a \textbf{\href{https://x.test}{certificate}}", r"About~30 users", r"Used \LaTeX{} daily",
                    r"\emph{Led} the team"):
            self.assertEqual(ed.editable(tex), {"text": tex, "latex": True}, tex)
        self.assertEqual(ed.editable(r"Grew it 3\% in \textbf{Go}"), {"text": "Grew it 3% in **Go**", "latex": False})


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.p = ed.pieces(JAKE)
        self.e1, self.e2 = self.p["entries"][0], self.p["entries"][1]

    def test_pieces_are_named_by_place_and_role(self):
        self.assertEqual(self.e1["title"], "Example Labs \u00b7 Software Engineer Intern")
        self.assertEqual([s["label"] for s in self.p["skills"]][:2], ["Languages", "Frameworks"])

    def test_edit_remove_add_and_skills(self):
        b1, b2 = self.e1["bullets"][0], self.e1["bullets"][1]
        row = self.p["skills"][0]
        out, log = ed.apply(JAKE, {"bullets": {b1["id"]: text("Built **APIs** for R&D")}, "remove": [b2["id"]],
                                   "add": {self.e2["id"]: [text("Labeled 9% of the *calls*")]},
                                   "skills": {row["id"]: text(row["text"] + ", Rust")}})
        self.assertIn(r"\resumeItem{Built \textbf{APIs} for R\&D}", out)
        self.assertNotIn("Cut report generation time", out)
        self.assertIn(r"\resumeItem{Labeled 9\% of the \textit{calls}}", out)
        self.assertIn("Python, C++, JavaScript, SQL, Rust}", out)
        self.assertEqual([x.kind for x in log], ["remove", "bullet", "add", "skills"])
        # Only the edited pieces changed: everything else is the version's own text.
        new = rt.parse(out)
        self.assertEqual(len(new.entries[0].bullets), len(self.e1["bullets"]) - 1)
        self.assertEqual(len(new.entries[1].bullets), len(self.e2["bullets"]) + 1)
        self.assertEqual(out.count("\n"), JAKE.count("\n"), "one line out, one line in")

    def test_a_new_bullet_is_written_like_the_others(self):
        e = ed.pieces(PLAIN)["entries"][0]
        out, _ = ed.apply(PLAIN, {"add": {e["id"]: [text("Wrote docs")]}})
        last = PLAIN.splitlines()[[i for i, l in enumerate(PLAIN.splitlines()) if l.strip().startswith(r"\item")][len(e["bullets"]) - 1]]
        indent = last[:len(last) - len(last.lstrip())]
        self.assertIn("\n" + indent + r"\item Wrote docs" + "\n", out)

    def test_tool_line(self):
        e = ed.pieces(PLAIN)["entries"][0]
        out, log = ed.apply(PLAIN, {"tech": {e["id"]: text("Vue.js, Node.js, REST")}})
        self.assertIn(r"\textit{Vue.js, Node.js, REST}", out)
        self.assertEqual((log[0].kind, log[0].before), ("tech", "Vue.js, Node.js, PouchDB, REST"))

    def test_adding_after_a_removed_last_bullet(self):
        ids = [b["id"] for b in self.e1["bullets"]]
        out, _ = ed.apply(JAKE, {"remove": [ids[-1]], "add": {self.e1["id"]: [text("New last")]}})
        got = [b.tex for b in rt.parse(out).entries[0].bullets]
        self.assertEqual(got[-1], "New last")
        self.assertEqual(len(got), len(ids))

    def test_replacing_every_bullet(self):
        ids = [b["id"] for b in self.e1["bullets"]]
        out, _ = ed.apply(JAKE, {"remove": ids, "add": {self.e1["id"]: [text("Only one"), text("And two")]}})
        self.assertEqual([b.tex for b in rt.parse(out).entries[0].bullets], ["Only one", "And two"])

    def test_latex_pieces_pass_as_written(self):
        e = ed.pieces(PLAIN)["entries"][0]
        b = next(b for b in e["bullets"] if b["latex"])
        new = b["text"].replace("course", "program")
        out, log = ed.apply(PLAIN, {"bullets": {b["id"]: text(new, True)}})
        self.assertIn(new, out)
        self.assertEqual(log[0].kind, "bullet")

    def test_unchanged_text_is_no_edit(self):
        b = self.e1["bullets"][0]
        self.assertEqual(ed.apply(JAKE, {"bullets": {b["id"]: text(b["text"] + "  ")}}), (JAKE, []))

    def test_a_removed_fact_sheet_bullet_takes_its_mark(self):
        tex = PLAIN.replace(r"\item Wrote Node.js", r"\item From the sheet " + rt.FACT_MARK + "\n    \\item Wrote Node.js", 1)
        bid = next(b.id for e in rt.parse(tex).entries for b in e.bullets if b.tex == "From the sheet")
        out, _ = ed.apply(tex, {"remove": [bid]})
        self.assertEqual(out, PLAIN)

    def test_bad_edits(self):
        b = self.e1["bullets"][0]
        for edits, says in (({"bullets": {"B99": text("x")}}, "no bullet B99"),
                            ({"remove": ["B99"]}, "no bullet B99"),
                            ({"add": {"E99": [text("x")]}}, "no entry E99"),
                            ({"skills": {"S99": text("x")}}, "no skills row S99"),
                            ({"tech": {self.e1["id"]: text("x")}}, "no tool list"),
                            ({"bullets": {b["id"]: text("  ")}}, "empty. Remove it instead"),
                            ({"bullets": {b["id"]: text("Built {it")}}, "without its }"),
                            ({"bullets": {b["id"]: text("x" * (ed.MAX_TEXT + 1))}}, "longer than"),
                            ({"add": {self.e1["id"]: [text("x")] * (ed.MAX_ADDED + 1)}}, "up to"),
                            ({"bullets": {b["id"]: "plain string"}}, "expected text"),
                            ({"remove": "B1"}, "expected a list")):
            with self.assertRaisesRegex(ed.EditError, says, msg=edits):
                ed.apply(JAKE, edits)


@unittest.skipUnless(HAS_TEX, "LaTeX is needed")
class MeasureTests(unittest.TestCase):
    def test_edited_pieces_are_measured(self):
        p = ed.pieces(JAKE)
        e = p["entries"][0]
        long = e["bullets"][0]["text"] + " and kept the on-call rotation quiet"
        out, log = ed.apply(JAKE, {"bullets": {e["bullets"][0]["id"]: text(long)},
                                   "skills": {p["skills"][0]["id"]: text("Python, Go")}})
        marked, names = ed.measured(out, log)
        layout: dict = {}
        rt.compile_pdf(marked, layout=layout)
        self.assertEqual(set(names), {"B1", "S1"})
        self.assertTrue(names["B1"].startswith("The bullet \u201cBuilt a REST API in Python"))
        self.assertEqual(rt.line_fit(layout, "B1").lines, 2)
        self.assertEqual(rt.line_fit(layout, "S1").lines, 1)


@unittest.skipUnless(HAS_TEX, "LaTeX is needed")
class EndpointTests(unittest.TestCase):
    """/api/tailored/edit through the real handler, on a version in a temporary data folder."""

    KEY = "acme|engineer|remote"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-edit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = config.Store(self.tmp / "settings.json", self.tmp)
        config.use(self.store)
        self.addCleanup(config.use, None)
        self.out = self.tmp / "tailored"
        self.out.mkdir()
        (self.tmp / "master.tex").write_text(JAKE, encoding="utf-8")
        for name, value in (("STORE", self.store), ("DB_PATH", self.tmp / "jobs.db"), ("MASTER", self.tmp / "master.tex"),
                            ("OUT_DIR", self.out), ("LOG_PATH", self.tmp / "server.log"),
                            ("PREP", prep.Preparer(server.db, self.tmp, self.out, lambda m: None, store=self.store))):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.out / "Acme_Engineer_20260101-000000.tex").write_text(JAKE, encoding="utf-8")
        result = {"model": "chatgpt (codex)", "pages": 1, "tex": "Acme_Engineer_20260101-000000.tex",
                  "pdf": "Acme_Engineer_20260101-000000.pdf", "changes": [], "keywords": [], "summary": "s"}
        conn = server.db()
        radar.migrate(conn)         # the jobs table the app always has
        with conn:
            conn.execute("INSERT INTO tailored (id, job_key, created, backend, model, pdf, tex, result) VALUES (?,?,?,?,?,?,?,?)",
                         ("v1", self.KEY, 1.0, "chatgpt", "chatgpt (codex)", result["pdf"], result["tex"], json.dumps(result)))
        conn.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Host": f"{server.HOST}:{server.PORT}", "Content-Type": "application/json",
                      "Origin": f"http://{server.HOST}:{server.PORT}"})
        r = conn.getresponse()
        got = json.loads(r.read().decode("utf-8"))
        conn.close()
        return r.status, got

    def save(self, body):
        status, got = self.request("POST", "/api/tailored/edit", {"id": "v1", **body})
        self.assertEqual(status, 202, got)
        for _ in range(200):
            _, s = self.request("GET", "/api/tailor/" + got["id"])
            if s["state"] in ("done", "error"):
                return s
            time.sleep(0.1)
        self.fail("the edit never finished")

    def test_pieces_then_a_new_version(self):
        status, d = self.request("GET", "/api/tailored/edit?id=v1")
        self.assertEqual(status, 200)
        self.assertEqual((d["key"], d["source"]), (self.KEY, JAKE))
        b = d["entries"][0]["bullets"][1]
        s = self.save({"edits": {"remove": [b["id"]]}})
        self.assertEqual(s["state"], "done", s.get("error"))
        r = s["result"]
        self.assertEqual((r["model"], r["edited_from"], r["pages"]), ("edited by you", "v1", 1))
        self.assertTrue(r["tex"].startswith("Acme_Engineer_2") and r["tex"] != "Acme_Engineer_20260101-000000.tex")
        self.assertTrue((self.out / r["pdf"]).is_file())
        self.assertNotIn(b["text"], (self.out / r["tex"]).read_text(encoding="utf-8"))
        self.assertEqual((self.out / "Acme_Engineer_20260101-000000.tex").read_text(encoding="utf-8"), JAKE,
                         "the version edited stays as it was")
        self.assertEqual(server.PREP.prepared(self.KEY)["tailored_id"], r["id"], "the edited version is the one sent")
        _, hist = self.request("GET", "/api/history?key=" + self.KEY.replace("|", "%7C"))
        self.assertEqual([h["id"] for h in hist], [r["id"], "v1"])
        self.assertEqual(hist[1]["result"]["id"], "v1")
        # Undo takes the new version out again.
        status, _ = self.request("POST", "/api/undo", {"id": s["undo"]})
        self.assertEqual(status, 200)
        _, hist = self.request("GET", "/api/history?key=" + self.KEY.replace("|", "%7C"))
        self.assertEqual([h["id"] for h in hist], ["v1"])

    def test_problems_come_back_as_errors(self):
        self.assertEqual(self.save({"edits": {}})["error"], "Nothing changed.")
        self.assertIn("doesn't compile", self.save({"source": JAKE.replace(r"\end{document}", "\\nosuchmacro\n\\end{document}")})["error"])
        status, got = self.request("POST", "/api/tailored/edit", {"id": "v1", "source": "not a document"})
        self.assertEqual(status, 400)
        status, got = self.request("POST", "/api/tailored/edit", {"id": "nope", "edits": {}})
        self.assertEqual(status, 404)

    def test_latex_source(self):
        s = self.save({"source": JAKE.replace("Example Labs", "Sample Labs")})
        self.assertEqual(s["state"], "done", s.get("error"))
        self.assertEqual([x["kind"] for x in s["result"]["edits"]], ["source"])
        self.assertIn("Sample Labs", (self.out / s["result"]["tex"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
