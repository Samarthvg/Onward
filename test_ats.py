"""Add a company (ats.py): ATS detection from URLs and pages, the live board
check, and the helper's settings and company endpoints.

Run:  python -m unittest test_ats
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import ats
import config
import prep
import radar
import server

URLS = {
    # Greenhouse
    "https://boards.greenhouse.io/airbnb": ("greenhouse", "airbnb"),
    "https://job-boards.greenhouse.io/databricks/jobs/4020305008": ("greenhouse", "databricks"),
    "https://job-boards.eu.greenhouse.io/deliveroo": ("greenhouse", "deliveroo"),
    "https://boards.greenhouse.io/embed/job_board?for=stripe&b=https%3A%2F%2Fstripe.com": ("greenhouse", "stripe"),
    "https://job-boards.greenhouse.io/embed/job_app?for=figma&token=123": ("greenhouse", "figma"),
    "https://boards-api.greenhouse.io/v1/boards/discord/jobs": ("greenhouse", "discord"),
    # Lever
    "https://jobs.lever.co/palantir": ("lever", "palantir"),
    "https://jobs.lever.co/palantir/6f6d5a9e-0000-4000-8000-000000000000/apply": ("lever", "palantir"),
    "https://api.lever.co/v0/postings/spotify?mode=json": ("lever", "spotify"),
    # Ashby
    "https://jobs.ashbyhq.com/openai": ("ashby", "openai"),
    "https://jobs.ashbyhq.com/Ramp%20Network/abc": ("ashby", "Ramp Network"),
    "https://api.ashbyhq.com/posting-api/job-board/notion": ("ashby", "notion"),
    # Workday
    "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite": ("workday", "nvidia.wd5/NVIDIAExternalCareerSite"),
    "https://Salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/California---San-Francisco/x_JR1": (
        "workday", "salesforce.wd12/External_Career_Site"),
    "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/jobs": ("workday", "adobe.wd5/external_experienced"),
    "https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/x": ("workday", "snapchat.wd1/snap"),
    # The rest
    "https://jobs.smartrecruiters.com/Intuitive/744000012345678": ("smartrecruiters", "Intuitive"),
    "https://careers.smartrecruiters.com/Dominos": ("smartrecruiters", "Dominos"),
    "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs": ("oracle", "jpmc.fa.oraclecloud.com/CX_1001"),
    "https://paypal.eightfold.ai/careers?query=engineer&domain=paypal.com": ("eightfold", "paypal.eightfold.ai|paypal.com"),
    "https://qualcomm.eightfold.ai/careers": ("eightfold", "qualcomm.eightfold.ai|qualcomm.com"),
    "https://apply.careers.microsoft.com/careers?query=x": ("eightfold", "apply.careers.microsoft.com|microsoft.com"),
    "https://careers-gdms.icims.com/jobs/12345/software-engineer/job": ("icims", "careers-gdms.icims.com"),
    "https://www.amazon.jobs/en/search?category%5B%5D=data-science": ("amazon", "data-science"),
    "https://www.amazon.jobs/en/jobs/2790000/sde": ("amazon", "software-development"),
}


class DetectTests(unittest.TestCase):
    def test_urls(self):
        for url, want in URLS.items():
            with self.subTest(url=url):
                b = ats.detect(url)
                self.assertIsNotNone(b)
                self.assertEqual((b.source, b.token), want)

    def test_tokens_fit_the_fetchers(self):
        """Every detected token has the shape its radar.py fetcher splits."""
        for url, (source, token) in URLS.items():
            self.assertIn(source, radar.FETCHERS)
            if source == "workday":
                host, site = token.split("/", 1)
                self.assertRegex(host, r"^[\w-]+\.wd\d+$")
            if source == "oracle":
                self.assertEqual(len(token.split("/", 1)), 2)
            if source == "eightfold":
                self.assertGreaterEqual(len(token.split("|")), 2)

    def test_not_a_board(self):
        for url in ["https://example.com/careers", "https://boards.greenhouse.io/", "not a url"]:
            self.assertIsNone(ats.detect(url), url)
        self.assertIn("LinkedIn", ats.unsupported("https://www.linkedin.com/jobs/view/1"))
        self.assertIn("automated", ats.unsupported("https://jobs.apple.com/en-us/search"))
        self.assertIsNone(ats.unsupported("https://boards.greenhouse.io/x"))

    def test_careers_page_embeds(self):
        page = """<html><head><script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script></head>
        <body><a href="https://www.linkedin.com/company/acme">LinkedIn</a>
        <a href="//boards.greenhouse.io/acmeco/jobs/1">Job</a><a href='https://boards.greenhouse.io/acmeco/jobs/2'>Job</a>
        <a href="https://jobs.lever.co/othercorp">old board</a></body></html>"""
        b = ats.find_in_page(page)
        self.assertEqual((b.source, b.token), ("greenhouse", "acmeco"))
        wd = 'window.location = "https:\\/\\/acme.wd1.myworkdayjobs.com\\/en-US\\/Careers"; &lt;a href=&quot;x&quot;&gt;'
        self.assertEqual(ats.find_in_page(wd).token, "acme.wd1/Careers", "URLs in inline JSON")
        wd = '<iframe src="https://acme.wd1.myworkdayjobs.com/en-US/Careers?q=1"></iframe>'
        self.assertEqual(ats.find_in_page(wd).token, "acme.wd1/Careers")
        self.assertIsNone(ats.find_in_page("<html>no jobs here</html>"))

    def test_lookup_reads_the_page_when_the_url_is_a_company_site(self):
        with mock.patch.object(radar, "get_text", lambda url: '<a href="https://jobs.ashbyhq.com/acme">Jobs</a>'), \
                mock.patch.object(ats, "check", lambda b: {"ok": True, "jobs": 12, "name": "", "error": ""}):
            r = ats.lookup("acme.example/careers")
        self.assertEqual((r["ok"], r["source"], r["token"], r["jobs"], r["name"]), (True, "ashby", "acme", 12, "Acme"))
        with mock.patch.object(radar, "get_text", lambda url: "<html></html>"):
            r = ats.lookup("https://acme.example/careers")
        self.assertFalse(r["ok"])
        self.assertIn("No supported job board", r["error"])


class FakeApi:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, *a, **k):
        self.calls.append(url)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, type) and issubclass(answer, Exception):
                    raise answer(url)
                return answer
        raise AssertionError(f"unexpected request {url}")


class CheckTests(unittest.TestCase):
    def check(self, source, token, routes, post=None):
        api = FakeApi(routes)
        with mock.patch.object(radar, "get_json", api), mock.patch.object(radar, "post_json", FakeApi(post or routes)), \
                mock.patch.object(radar, "get_text", api):
            return ats.check(ats.Board(source, token))

    def test_greenhouse(self):
        r = self.check("greenhouse", "figma", {
            "https://boards-api.greenhouse.io/v1/boards/figma/jobs": {"jobs": [{}, {}, {}]},
            "https://boards-api.greenhouse.io/v1/boards/figma": {"name": "Figma"}})
        self.assertEqual(r, {"ok": True, "jobs": 3, "name": "Figma", "error": ""})
        r = self.check("greenhouse", "nope", {"https://boards-api.greenhouse.io": radar.BoardMissing})
        self.assertFalse(r["ok"])
        self.assertIn("doesn't exist", r["error"])

    def test_workday(self):
        r = self.check("workday", "nvidia.wd5/NVIDIAExternalCareerSite", {}, post={
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs": {"total": 2000, "jobPostings": [{}]}})
        self.assertEqual((r["ok"], r["jobs"]), (True, 2000))
        r = self.check("workday", "x.wd1/Gone", {}, post={"https://x.wd1": {"errorCode": "NOT_FOUND"}})
        self.assertFalse(r["ok"])

    def test_others(self):
        self.assertEqual(self.check("lever", "palantir", {"https://api.lever.co/v0/postings/palantir": [{}, {}]})["jobs"], 2)
        self.assertEqual(self.check("ashby", "notion", {"https://api.ashbyhq.com": {"jobs": [{}, {"isListed": False}]}})["jobs"], 1)
        sr = self.check("smartrecruiters", "Intuitive", {radar.SR_API: {"totalFound": 40, "content": [{"company": {"name": "Intuitive"}}]}})
        self.assertEqual((sr["jobs"], sr["name"]), (40, "Intuitive"))
        self.assertFalse(self.check("smartrecruiters", "Nobody", {radar.SR_API: {"totalFound": 0, "content": []}})["ok"])
        o = self.check("oracle", "jpmc.fa.oraclecloud.com/CX_1001", {
            "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCESites": {"items": [
                {"SiteNumber": "CX_1001", "SiteURLName": "x", "StatusCode": "ORA_ACTIVE", "SiteName": "JPMC Careers"}]},
            "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions": {"items": [{"TotalJobsCount": 7}]}})
        self.assertEqual((o["ok"], o["jobs"], o["name"]), (True, 7, "JPMC Careers"))
        e = self.check("eightfold", "paypal.eightfold.ai|paypal.com", {"https://paypal.eightfold.ai/api/pcsx/search": {"data": {"positions": [], "count": 5}}})
        self.assertEqual(e["jobs"], 5)
        i = self.check("icims", "careers-x.icims.com", {"https://careers-x.icims.com/sitemap.xml":
                       "<loc>https://careers-x.icims.com/jobs/1/a/job</loc><loc>https://careers-x.icims.com/about</loc>"})
        self.assertEqual(i["jobs"], 1)
        t = self.check("lever", "busy", {"https://api.lever.co": radar.Throttled})
        self.assertIn("rate limiting", t["error"])


class EndpointTests(unittest.TestCase):
    """/api/settings and /api/companies on a spare port, with their own settings file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jr-ats-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = config.Store(self.tmp / "settings.json")
        config.use(self.store)
        self.addCleanup(config.use, None)
        q = prep.Preparer(lambda: None, self.tmp / "resume", self.tmp / "out", log=lambda m: None, store=self.store)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = httpd.server_address[1]
        self.checked = []
        fake_check = lambda b: self.checked.append(b.board) or (
            {"ok": True, "jobs": 3, "name": "", "error": ""} if b.token != "dead" else
            {"ok": False, "jobs": 0, "name": "", "error": "The ATS says this board doesn't exist."})
        for p in [mock.patch.object(server, "PORT", self.port), mock.patch.object(server, "STORE", self.store),
                  mock.patch.object(server, "PREP", q), mock.patch.object(server, "RESUME_DIR", self.tmp / "resume"),
                  mock.patch.object(server, "MASTER", self.tmp / "resume" / "master.tex"),
                  mock.patch.object(server, "ORIGINS", {f"http://127.0.0.1:{self.port}"}),
                  mock.patch.object(ats, "check", fake_check), mock.patch.object(server, "schedule_render", lambda: None)]:
            p.start()
            self.addCleanup(p.stop)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

    def call(self, path, body=None, origin=True):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if origin:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
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

    def test_settings_roundtrip(self):
        st, d = self.call("/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual(d["settings"], config.defaults())
        templates = {t["id"]: t["title_keywords"] for t in d["choices"]["templates"]}
        self.assertEqual(templates["blank"], [])
        self.assertIn({"code": "CA", "name": "Canada"}, d["choices"]["countries"])
        self.assertNotIn("ollama", d["choices"]["backends"] + d["choices"]["draft_backends"])
        data = {**d["new_role"], "id": "data", "name": "Data engineer, any US city", "title_keywords": templates["data"],
                "seniority": ["entry", "mid"], "visa": {"status": "authorized", "hide_no_sponsorship": False}}
        st, d = self.call("/api/settings", {"settings": {"roles": [d["settings"]["roles"][0], data]}})
        self.assertEqual(st, 200, d)
        self.assertEqual([r["id"] for r in self.store.load()["roles"]], ["software", "data"])
        st, d = self.call("/api/settings", {"settings": {"roles": [{**data, "title_keywords": []}]}})
        self.assertEqual(st, 400, "a role needs title keywords")
        self.assertEqual(len(self.store.load()["roles"]), 2)
        st, d = self.call("/api/settings", {"settings": {"queue": {"backend": "skynet"}, "radar": {"workers": "many"}}})
        self.assertEqual(st, 400)
        self.assertEqual(len(d["errors"]), 2)
        self.assertEqual(self.store.load()["queue"]["backend"], "chatgpt", "a rejected save changes nothing")
        st, _ = self.call("/api/settings", {"settings": {"ui": {"advanced": True}}}, origin=False)
        self.assertEqual(st, 403, "only the dashboard may change settings")

    def test_add_remove_and_block_companies(self):
        st, r = self.call("/api/companies/lookup", {"url": "https://boards.greenhouse.io/newco"})
        self.assertEqual((st, r["ok"], r["board"], r["tracked"]), (200, True, "greenhouse:newco", False))
        st, r = self.call("/api/companies/lookup", {"url": "https://boards.greenhouse.io/airbnb"})
        self.assertTrue(r["tracked"], "airbnb ships in companies.json")
        st, r = self.call("/api/companies/add", {"source": "greenhouse", "token": "newco", "name": "NewCo"})
        self.assertEqual(st, 200, r)
        c = self.store.load()["companies"]
        self.assertEqual((c["added"], c["display_names"]), ({"greenhouse": ["newco"]}, {"newco": "NewCo"}))
        self.assertIn({"source": "greenhouse", "token": "newco", "origin": "added", "removed": False, "name": "NewCo"}, r["boards"])
        st, r = self.call("/api/companies/add", {"source": "workday", "token": "acme.wd1/External", "name": "Acme"})
        self.assertEqual(self.store.load()["companies"]["display_names"]["acme"], "Acme", "Workday names go by tenant")
        st, r = self.call("/api/companies/add", {"source": "greenhouse", "token": "dead"})
        self.assertEqual(st, 422)
        st, r = self.call("/api/companies/add", {"source": "myspace", "token": "x"})
        self.assertEqual(st, 400)
        self.assertEqual(self.checked, ["greenhouse:newco", "greenhouse:airbnb", "greenhouse:newco",
                                        "workday:acme.wd1/External", "greenhouse:dead"])

        self.call("/api/companies/remove", {"source": "greenhouse", "token": "newco"})
        self.assertEqual(self.store.load()["companies"]["added"]["greenhouse"], [])
        self.call("/api/companies/remove", {"source": "greenhouse", "token": "airbnb"})
        self.assertEqual(self.store.load()["companies"]["removed"], ["greenhouse:airbnb"])
        self.assertNotIn("airbnb", radar.load_companies(self.store.load())["greenhouse"])
        self.call("/api/companies/restore", {"source": "greenhouse", "token": "airbnb"})
        self.assertEqual(self.store.load()["companies"]["removed"], [])

        st, r = self.call("/api/companies/block", {"name": "Palantir"})
        self.assertEqual((st, r["blocked"]), (200, ["Palantir"]))
        self.call("/api/companies/block", {"name": "palantir"})
        self.assertEqual(self.store.load()["companies"]["blocked"], ["palantir"], "one entry per company")
        self.call("/api/companies/unblock", {"name": "PALANTIR"})
        self.assertEqual(self.store.load()["companies"]["blocked"], [])


if __name__ == "__main__":
    unittest.main()
