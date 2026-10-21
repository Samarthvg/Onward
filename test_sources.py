"""Tests for the job sources and the HTTP layer under them.

Run:  python -m unittest test_sources
Parser tests read real responses saved in fixtures/ (trimmed to a few
records). HTTP tests run against a local server, so retry and robots.txt
handling is exercised over real sockets.
"""

import json
import sqlite3
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import radar

FIXTURES = Path(__file__).resolve().parent / "fixtures"
POSTING_KEYS = {"title", "location", "country", "remote", "url", "source", "posted_at", "posted_exact",
                "description", "apply_url", "verify_url", "posted_field", "posted_raw"}


def fixture(name: str):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class FakeWeb:
    """Stands in for get_json/get_text: the first route whose key is in the
    URL answers. A route value that is an exception class is raised."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, type) and issubclass(value, Exception):
                    raise value(url)
                return value(url) if callable(value) else value
        raise AssertionError(f"unexpected request {url}")


class SourceTestCase(unittest.TestCase):
    """Fresh caches per test, and a wide dashboard window so saved fixtures
    stay inside it however old they get."""

    def setUp(self) -> None:
        radar.DETAILS.clear()
        radar.NEW_DETAILS.clear()
        radar._ONCE.clear()
        patcher = mock.patch.object(radar, "DASHBOARD_DAYS", 100000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def web(self, routes: dict, text: dict | None = None) -> FakeWeb:
        fake = FakeWeb(routes)
        for name in ("get_json", "post_json"):
            p = mock.patch.object(radar, name, fake)
            p.start()
            self.addCleanup(p.stop)
        if text is not None:
            p = mock.patch.object(radar, "get_text", FakeWeb(text))
            p.start()
            self.addCleanup(p.stop)
        return fake

    def assertPosting(self, p: dict) -> None:
        self.assertEqual(set(p), POSTING_KEYS)


# --------------------------------------------------------------------------
# Parsers, from saved responses

class SmartRecruitersTests(SourceTestCase):
    def test_posting(self):
        p = radar._smartrecruiters_posting("NorthwesternMutual", fixture("smartrecruiters_posting.json"))
        self.assertPosting(p)
        self.assertEqual(p["title"], "Software Engineer")
        self.assertEqual(p["location"], "Franklin, WI, United States")
        self.assertEqual((p["posted_field"], p["posted_raw"], p["posted_exact"]),
                         ("releasedDate", "2017-08-28T04:03:11.000Z", True))
        self.assertEqual(p["verify_url"], f"{radar.SR_API}/NorthwesternMutual/postings/743999658679125")
        lines = p["description"].split("\n")
        self.assertIn("Job Description", lines)             # section headings kept on their own lines
        self.assertIn("About the company", lines)
        self.assertTrue(any(l.startswith("• ") for l in lines))

    def test_board_filters_and_details(self):
        detail = fixture("smartrecruiters_posting.json")
        web = self.web({"/postings?country=us&limit=100&offset=0": fixture("smartrecruiters_list.json"),
                        "/postings?country=us": {"totalFound": 429, "content": []},
                        "/postings/744000156619374": detail})
        jobs = radar.fetch_smartrecruiters("ServiceNow")
        # Of "Sr Software Engineer", "Software Engineer - Federal" and a designer,
        # only the entry-level engineering title costs a detail request.
        self.assertEqual([u for u in web.calls if "/postings/" in u],
                         [f"{radar.SR_API}/ServiceNow/postings/744000156619374"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "ServiceNow")
        self.assertEqual(jobs[0].source, "smartrecruiters")

    def test_unknown_company_is_missing(self):
        self.web({"/postings?country=us": {"totalFound": 0, "content": []},
                  "/postings?limit=1": {"totalFound": 0, "content": []}})
        with self.assertRaises(radar.BoardMissing):
            radar.fetch_smartrecruiters("Visa")


class OracleTests(SourceTestCase):
    def test_board(self):
        host = "edel.fa.us2.oraclecloud.com"
        sites = {"items": [{"SiteNumber": "CX_2001", "SiteURLName": None, "StatusCode": "ORA_ACTIVE"}]}
        web = self.web({"recruitingCESites": sites,
                        "recruitingCEJobRequisitions?": fixture("oracle_list.json"),
                        "recruitingCEJobRequisitionDetails": fixture("oracle_detail.json")})
        jobs = radar.fetch_oracle(f"{host}/CX_2001")
        self.assertEqual(sum("Details" in u for u in web.calls), 1)   # Singapore and senior Tel Aviv roles skipped
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j.title, "Early Careers: Software Development Engineer")
        self.assertEqual(j.location, "Sunnyvale, CA, United States")
        self.assertEqual((j.posted_field, j.posted_raw), ("ExternalPostedStartDate", "2026-10-16T21:18:15+00:00"))
        self.assertTrue(j.posted_exact)
        self.assertEqual(j.posted_at, radar.parse_iso("2026-10-16T21:18:15+00:00"))
        self.assertIn("recruitingCEJobRequisitionDetails", j.verify_url)
        self.assertIn("Id=%2224668%22", j.verify_url)
        self.assertEqual(j.url, f"https://{host}/hcmUI/CandidateExperience/en/sites/CX_2001/job/24668")
        self.assertIn("About the company", j.description.split("\n"))

    def test_site_by_number_or_url_name(self):
        sites = {"items": [{"SiteNumber": "CX_3002", "SiteURLName": "LateralHiring", "StatusCode": "ORA_ACTIVE"},
                           {"SiteNumber": "CX_1", "SiteURLName": None, "StatusCode": "ORA_INACTIVE"}]}
        web = self.web({"recruitingCESites": sites,
                        "recruitingCEJobRequisitions?": {"items": [{"TotalJobsCount": 0, "requisitionList": []}]}})
        self.assertEqual(radar.fetch_oracle("hdpc.fa.us2.oraclecloud.com/LateralHiring"), [])
        self.assertEqual(radar.fetch_oracle("hdpc.fa.us2.oraclecloud.com/CX_3002"), [])
        searches = [u for u in web.calls if "findReqs" in u]
        self.assertTrue(searches and all("siteNumber=CX_3002," in u for u in searches))
        for gone in ("CX_9999", "CX_1"):                    # unknown, and inactive
            with self.subTest(site=gone), self.assertRaises(radar.BoardMissing):
                radar.fetch_oracle(f"hdpc.fa.us2.oraclecloud.com/{gone}")


class EightfoldTests(SourceTestCase):
    def test_posting(self):
        self.web({"position_details": fixture("eightfold_position.json")})
        p = radar._eightfold_posting("apply.careers.microsoft.com", "1970393556982911")
        self.assertPosting(p)
        self.assertEqual(p["title"], "Software Engineer - CTJ - TS (Cleared)")
        self.assertEqual(p["location"], "United States, Virginia, Reston")
        self.assertEqual((p["posted_field"], p["posted_at"], p["posted_exact"]), ("postedTs", 1790110540.0, True))
        self.assertTrue(p["verify_url"].endswith("position_details?position_id=1970393556982911&hl=en"))
        self.assertGreater(len(p["description"]), 500)

    def test_midnight_utc_is_a_date(self):
        data = fixture("eightfold_position.json")
        data["data"]["postedTs"] = 1792368000              # 2026-10-19 00:00:00 UTC
        self.web({"position_details": data})
        p = radar._eightfold_posting("qualcomm.eightfold.ai", "1")
        self.assertFalse(p["posted_exact"])
        self.assertEqual(p["posted_at"], radar._day_start("2026-10-19"))

    def test_search_pages_and_filters(self):
        web = self.web({"start=0": fixture("eightfold_search.json"),
                        "/api/pcsx/search": {"data": {"positions": [], "count": 804}}})
        jobs = radar.fetch_eightfold("apply.careers.microsoft.com|microsoft.com|filter_profession=software engineering")
        self.assertEqual(jobs, [])         # architect, solution engineer, directors: none entry-level SWE
        self.assertIn("filter_profession=software+engineering", web.calls[0])
        self.assertIn("location=United+States", web.calls[0])
        self.assertFalse(any("position_details" in u for u in web.calls))


class AmazonTests(SourceTestCase):
    def test_search(self):
        self.web({"offset=0": fixture("amazon_search.json"), "search.json": {"hits": 1464, "jobs": []}})
        jobs = radar.fetch_amazon("software-development")
        # SDE I passes; the embedded role asks for 3+ years; "Engr II" is not entry level.
        self.assertEqual([j.title for j in jobs],
                         ["Software Development Engineer I – AI/ML Network Infrastructure, Annapurna Labs"])
        j = jobs[0]
        self.assertEqual(j.company, "Amazon")
        self.assertEqual((j.posted_field, j.posted_raw, j.posted_exact), ("posted_date", "October 21, 2026", False))
        self.assertEqual(j.posted_at, radar._day_start("2026-10-21"))
        self.assertEqual(j.verify_url, f"{radar.AMAZON_SEARCH}?base_query=10584797&result_limit=10")
        lines = j.description.split("\n")
        self.assertIn("Basic Qualifications", lines)
        self.assertIn("Preferred Qualifications", lines)

    def test_lookup_by_id(self):
        self.web({"search.json": fixture("amazon_search.json")})
        self.assertEqual(radar._amazon_by_id("10584826")["title"],
                         "Embedded Software Engineer, EC2 Nitro Networking , EC2 Nitro Networking")
        with self.assertRaises(radar.BoardMissing):
            radar._amazon_by_id("999")


class IcimsTests(SourceTestCase):
    def test_jibe_api(self):
        web = self.web({"page=1&": fixture("jibe_jobs.json"), "careers.amd.com/api/jobs": {"jobs": [], "totalCount": 647}})
        jobs = radar.fetch_icims("careers.amd.com")
        self.assertIn("country=United+States", web.calls[0])
        self.assertEqual([j.title for j in jobs], ["AI Scale-up Switch System Design Engineer"])
        j = jobs[0]
        self.assertEqual((j.posted_field, j.posted_raw, j.posted_exact),
                         ("posted_date", "2026-10-20T21:51:00+0000", True))
        self.assertEqual(j.posted_at, radar.parse_iso("2026-10-20T21:51:00+00:00"))
        self.assertIn("Qualifications", j.description.split("\n"))

    def test_portal_sitemap_and_jsonld(self):
        page = "https://careers-gdms.icims.com/jobs/75608/entry-level-software-engineer-%28spring-2027%29/job"
        self.web({}, text={"sitemap.xml": fixture("icims_sitemap.xml"),
                           "/jobs/75608/": fixture("icims_job.html")})
        jobs = radar.fetch_icims("careers-gdms.icims.com")
        self.assertEqual(len(jobs), 1)                     # machinist and intern slugs never fetched
        j = jobs[0]
        self.assertEqual(j.title, "Entry Level Software Engineer (Spring 2027)")
        self.assertEqual(j.url, page)
        self.assertEqual(j.verify_url, page)
        # "T04:00:00.000Z" is midnight Eastern: a date, not a time.
        self.assertEqual((j.posted_field, j.posted_exact), ("datePosted", False))
        self.assertEqual(j.posted_at, radar._day_start("2026-10-20"))
        self.assertIn(page, radar.NEW_DETAILS)

    def test_clock_time(self):
        cases = {"2026-02-27T05:00:00.000Z": False, "2026-10-05T12:40:00+0000": True,
                 "2026-10-05": False, "2026-10-05T12:00:07Z": True, "": False}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(radar._has_clock_time(raw), want)


# --------------------------------------------------------------------------
# Listing URLs -> posting records

class PostingUrlTests(SourceTestCase):
    def requested(self, url: str) -> str | None:
        """The first URL the loader for a listing URL asks for."""
        load = radar.posting_loader(url)
        if load is None:
            return None
        seen: list[str] = []

        def record(u, *a, **k):
            seen.append(u)
            raise radar.BoardMissing(u)
        with mock.patch.object(radar, "get_json", record), mock.patch.object(radar, "get_text", record):
            try:
                load()
            except radar.BoardMissing:
                pass
        return seen[0]

    def test_routes(self):
        cases = {
            "https://job-boards.greenhouse.io/alarmcom/jobs/8622530002":
                "https://boards-api.greenhouse.io/v1/boards/alarmcom/jobs/8622530002",
            "https://boards.greenhouse.io/embed/job_app?for=spacex&token=8436479002":
                "https://boards-api.greenhouse.io/v1/boards/spacex/jobs/8436479002",
            "https://job-boards.eu.greenhouse.io/thinkcellsoftware/jobs/4844280101":
                "https://boards-api.greenhouse.io/v1/boards/thinkcellsoftware/jobs/4844280101",
            "https://jobs.eu.lever.co/cirrus/61ae5eb1-94c8-4aba-9f2a-7968facbf18e/apply":
                "https://api.eu.lever.co/v0/postings/cirrus/61ae5eb1-94c8-4aba-9f2a-7968facbf18e",
            "https://jobs.ashbyhq.com/sciemo/208da924-4a08-4be7-a20c-a420f08e77d2/application?embed=true":
                "https://api.ashbyhq.com/posting-api/job-board/sciemo",
            "https://spgi.wd5.myworkdayjobs.com/en-US/SPGI_Careers/job/Colorado-US/Analyst_325807-1/apply":
                "https://spgi.wd5.myworkdayjobs.com/wday/cxs/spgi/SPGI_Careers/job/Colorado-US/Analyst_325807-1",
            "https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/AZ---Chandler/Engineer-I-Software_R322-26":
                "https://wd5.myworkdaysite.com/wday/cxs/microchiphr/External/job/AZ---Chandler/Engineer-I-Software_R322-26",
            "https://jobs.smartrecruiters.com/Wabtec/3743990014806166":
                f"{radar.SR_API}/Wabtec/postings/3743990014806166",
            "https://ibqbjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Honeywell/job/140055":
                "https://ibqbjb.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                "?expand=all&onlyData=true&finder=ById;Id=%22140055%22,siteNumber=Honeywell",
            "https://apply.careers.microsoft.com/careers/job/1970393556982911":
                "https://apply.careers.microsoft.com/api/pcsx/position_details?position_id=1970393556982911&hl=en",
            "https://amazon.jobs/en/jobs/10513110/software-development-engineer-amazon-leo-early-career-2026":
                f"{radar.AMAZON_SEARCH}?base_query=10513110&result_limit=10",
            "https://careers-peraton.icims.com/jobs/164003/job?mobile=true&needsRedirect=false":
                "https://careers-peraton.icims.com/jobs/164003/job?in_iframe=1",
            "https://careers.jhuapl.edu/jobs/59664?icims=1": "https://careers.jhuapl.edu/jobs/59664?icims=1",
            "https://careers.example.com/openings/123": "https://careers.example.com/openings/123",
            "https://jobs.apple.com/en-us/details/200681316": None,
            "https://www.google.com/about/careers/applications/jobs/results/84680705375642310": None,
            "https://www.metacareers.com/jobs/123": None,
            "mailto:jobs@example.com": None,
        }
        for listing, want in cases.items():
            with self.subTest(listing=listing):
                self.assertEqual(self.requested(listing), want)


class SimplifyTests(SourceTestCase):
    NOW = time.time()

    def listing(self, url: str, title: str = "Software Engineer", **extra) -> dict:
        return {"active": True, "is_visible": True, "date_posted": int(self.NOW) - 3600,
                "title": title, "locations": ["New York, NY"], "company_name": "Acme", "url": url,
                "sponsorship": "Offers Sponsorship", **extra}

    def test_enrich_keep_and_drop(self):
        gh = {"id": 1, "title": "Software Engineer, New Grad", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
              "location": {"name": "New York, NY"}, "first_published": "2026-10-20T15:04:05-04:00",
              "content": "&lt;h2&gt;Requirements&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"}
        feed = [self.listing("https://job-boards.greenhouse.io/acme/jobs/1"),
                self.listing("https://jobs.apple.com/en-us/details/200681316", "Software Engineer, Siri"),
                self.listing("https://jobs.lever.co/acme/61ae5eb1-94c8-4aba-9f2a-7968facbf18e", "Backend Engineer"),
                self.listing("https://example.com/x", "Account Executive")]
        web = self.web({radar.SIMPLIFY_URL: feed, "boards-api.greenhouse.io": gh,
                        "api.lever.co": radar.BoardMissing})
        with mock.patch.object(radar, "log"):
            jobs = {j.title: j for j in radar.fetch_simplify()}

        self.assertEqual(set(jobs), {"Software Engineer, New Grad", "Software Engineer, Siri"})
        enriched = jobs["Software Engineer, New Grad"]
        self.assertEqual(enriched.source, "greenhouse")
        self.assertEqual(enriched.company, "Acme")
        self.assertEqual((enriched.posted_field, enriched.posted_raw), ("first_published", gh["first_published"]))
        self.assertEqual(enriched.verify_url, "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1")
        self.assertIn("• Python", enriched.description)
        self.assertEqual(enriched.sponsorship, "yes")       # Simplify's label still fills the gap
        listing_only = jobs["Software Engineer, Siri"]
        self.assertEqual((listing_only.source, listing_only.description), ("simplify", ""))
        self.assertEqual(listing_only.verify_url, radar.SIMPLIFY_VERIFY_URL)
        # The closed Lever posting is dropped and remembered as closed.
        self.assertEqual(radar.NEW_DETAILS["https://jobs.lever.co/acme/61ae5eb1-94c8-4aba-9f2a-7968facbf18e"],
                         radar.GONE)
        # Non-SWE listings are filtered before any request.
        self.assertFalse(any("example.com" in u for u in web.calls))

        # A second run asks only for the feed: every posting is cached, closed ones included.
        web.calls.clear()
        with mock.patch.object(radar, "log"):
            radar.fetch_simplify()
        self.assertEqual(web.calls, [radar.SIMPLIFY_URL])


# --------------------------------------------------------------------------
# State kept between runs

class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        radar.migrate(self.conn)

    def test_workday_rows_seed_detail_cache(self):
        self.conn.execute("DROP TABLE details")
        row = dict(key="k", company="Nvidia", title="Software Engineer", location="Santa Clara, CA",
                   remote=0, url="https://nvidia.wd5.myworkdayjobs.com/X/job/a_JR1", source="workday",
                   posted_at=1.7e9, posted_exact=0, sponsorship="unknown", sponsor_note="", min_yoe=None,
                   entry_hint=0, first_seen=1.7e9, last_seen=1.7e9, apply_url="a", verify_url="v",
                   posted_field="startDate", posted_raw="2026-10-01", description="About\n• Go")
        self.conn.execute(f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          list(row.values()))
        radar.migrate(self.conn)
        (posting,), = self.conn.execute("SELECT posting FROM details").fetchall()
        posting = json.loads(posting)
        self.assertEqual(set(posting), POSTING_KEYS)
        job = radar.build_job(company="Nvidia", **posting)
        self.assertEqual((job.title, job.posted_raw, job.posted_exact, job.remote), ("Software Engineer", "2026-10-01", False, False))

    def job(self, **kw):
        base = dict(company="Acme", title="Software Engineer", location="New York, NY", country=None,
                    remote=False, url="u", source="simplify", posted_at=1.7e9, posted_exact=True,
                    description="", apply_url="u", verify_url="v", posted_field="date_posted", posted_raw="1")
        return radar.build_job(**{**base, **kw})

    def test_listing_row_moves_to_its_posting(self):
        listing = self.job()
        self.assertEqual(radar.store(self.conn, [listing], 1000.0), 1)
        posting = self.job(title="Software Engineer, New Grad", source="greenhouse", description="Requirements\n• Go")
        posting.listing_key = listing.key
        self.assertEqual(radar.store(self.conn, [posting], 2000.0), 0)       # the same job, not a new one
        rows = self.conn.execute("SELECT key, title, source, first_seen, last_seen FROM jobs").fetchall()
        self.assertEqual(rows, [(posting.key, "Software Engineer, New Grad", "greenhouse", 1000.0, 2000.0)])

    def test_listing_row_folds_into_stored_posting(self):
        listing = self.job()
        posting = self.job(title="Software Engineer I", source="greenhouse")
        radar.store(self.conn, [listing], 1000.0)
        radar.store(self.conn, [posting], 1500.0)                           # from the company's own board
        posting.listing_key = listing.key
        radar.store(self.conn, [posting], 2000.0)
        rows = self.conn.execute("SELECT key, first_seen FROM jobs").fetchall()
        self.assertEqual(rows, [(posting.key, 1000.0)])

    def test_throttle_cooldown_grows_then_clears(self):
        board = "workday:accenture.wd103/AccentureCareers"

        def run(throttled: bool, now: float) -> tuple | None:
            res = radar.FetchResult([], [], [f"{board} (Throttled: 429)"] if throttled else [],
                                    [board] if throttled else [], [board])
            radar.save_state(self.conn, res, now)
            return self.conn.execute("SELECT strikes, skip_until FROM board_state").fetchone()

        self.assertEqual(run(True, 1000.0), (1, 1000.0 + radar.COOLDOWN_BASE))
        self.assertEqual(run(True, 9000.0), (2, 9000.0 + 2 * radar.COOLDOWN_BASE))
        self.assertIsNone(run(False, 20000.0))


# --------------------------------------------------------------------------
# HTTP layer, against a local server

class RobotsTests(unittest.TestCase):
    def test_rules(self):
        r = radar.Robots("User-agent: *\nDisallow: /jobs/*login\nDisallow: /private\nAllow: /private/ok$\n"
                         "Crawl-delay: 5\n\nUser-agent: OtherBot\nDisallow: /\n")
        self.assertTrue(r.allowed("/jobs/123/job"))
        self.assertFalse(r.allowed("/jobs/123/login"))
        self.assertFalse(r.allowed("/private/x"))
        self.assertTrue(r.allowed("/private/ok"))
        self.assertFalse(r.allowed("/private/ok/more"))
        self.assertEqual(r.delay, 5.0)

    def test_own_group_wins(self):
        r = radar.Robots("User-agent: *\nDisallow: /\n\nUser-agent: jobradar\nAllow: /api/\nDisallow: /\n")
        self.assertTrue(r.allowed("/api/jobs"))
        self.assertFalse(r.allowed("/careers"))

    def test_empty_and_tie(self):
        self.assertTrue(radar.Robots("").allowed("/anything"))
        self.assertTrue(radar.Robots("User-agent: *\nDisallow:\n").allowed("/x"))
        self.assertTrue(radar.Robots("User-agent: *\nDisallow: /a\nAllow: /a\n").allowed("/a"))

    def test_retry_after(self):
        self.assertEqual(radar.retry_after("7"), 7.0)
        self.assertIsNone(radar.retry_after(None))
        self.assertIsNone(radar.retry_after("soon"))
        when = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 60))
        self.assertAlmostEqual(radar.retry_after(when), 60, delta=2)


class Handler(BaseHTTPRequestHandler):
    """Answers from the owning server's script: path -> list of (status, headers, body), one per hit."""

    def do_GET(self):
        self.server.hits.append(self.path)
        script = self.server.script.get(self.path.split("?")[0]) or [(404, {}, b"")]
        status, headers, body = script.pop(0) if len(script) > 1 else script[0]
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits, self.server.script = [], {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        p = mock.patch.object(radar, "DEFAULT_INTERVAL", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def script(self, path: str, *responses) -> None:
        self.server.script[path] = list(responses)

    def test_retry_after_pauses_then_succeeds(self):
        self.script("/robots.txt", (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"))
        self.script("/jobs", (429, {"Retry-After": "1"}, b""), (200, {}, b'{"ok": true}'))
        started = time.monotonic()
        self.assertEqual(radar.get_json(f"{self.base}/jobs"), {"ok": True})
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertEqual(self.server.hits, ["/robots.txt", "/jobs", "/jobs"])

    def test_long_retry_after_ends_the_host_for_the_run(self):
        self.script("/jobs", (429, {"Retry-After": "600"}, b""))
        with self.assertRaises(radar.Throttled):
            radar.get_json(f"{self.base}/jobs")
        with self.assertRaises(radar.Throttled):
            radar.get_json(f"{self.base}/other")       # same host: not asked at all
        self.assertEqual(self.server.hits, ["/robots.txt", "/jobs"])

    def test_persistent_429_is_throttled(self):
        self.script("/jobs", (429, {"Retry-After": "0"}, b""))
        with self.assertRaises(radar.Throttled):
            radar.get_json(f"{self.base}/jobs")
        self.assertEqual(self.server.hits.count("/jobs"), 3 + radar.THROTTLE_RETRIES)

    def test_server_errors_are_retried(self):
        self.script("/jobs", (503, {"Retry-After": "0"}, b""), (502, {}, b""), (200, {}, b"{}"))
        self.assertEqual(radar.get_json(f"{self.base}/jobs"), {})

    def test_robots_disallow_and_crawl_delay(self):
        self.script("/robots.txt", (200, {"Content-Type": "text/plain"},
                                    b"User-agent: *\nDisallow: /private\nCrawl-delay: 0.4\n"))
        self.script("/a", (200, {}, b"{}"))
        with self.assertRaises(radar.Disallowed):
            radar.get_json(f"{self.base}/private/x")
        started = time.monotonic()
        radar.get_json(f"{self.base}/a")
        radar.get_json(f"{self.base}/a")
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertNotIn("/private/x", self.server.hits)

    def test_html_robots_means_no_rules(self):
        self.script("/robots.txt", (200, {"Content-Type": "text/html"}, b"<html>Disallow: /</html>"))
        self.script("/a", (200, {}, b"[]"))
        self.assertEqual(radar.get_json(f"{self.base}/a"), [])

    def test_gone_posting(self):
        self.script("/job", (410, {}, b""))
        with self.assertRaises(radar.BoardMissing):
            radar.get_json(f"{self.base}/job")


if __name__ == "__main__":
    unittest.main()
