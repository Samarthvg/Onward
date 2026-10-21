"""Search profiles (filters.py) and how radar.py uses them.

Run:  python -m unittest test_profiles
"""

import json
import re
import sqlite3
import time
import unittest
from unittest import mock

import config
import filters
import radar


def legacy_keep(title: str, location: str, country, description: str) -> bool:
    """build_job's filter before profiles existed, as it was written."""
    if not (radar.is_swe_title(title) and radar.is_entry_title(title)):
        return False
    if not radar.us_location(location, country):
        return False
    yoe = radar.min_years(description)
    return not (yoe is not None and yoe >= 3 and not radar.ENTRY_HINT.search(title))


REQ = "Requirements\n• Python and SQL\n"
# (company, title, location, country, description)
POSTINGS = [
    ("Acme", "Software Engineer, New Grad", "New York, NY", None, REQ),
    ("Acme", "Software Engineer", "San Francisco, CA", None, REQ + "• 1+ years of experience"),
    ("Acme", "Software Engineer II", "Seattle, WA", None, REQ + "• 3+ years of experience"),
    ("Acme", "Senior Software Engineer", "Remote - US", None, REQ + "• 6+ years of experience"),
    ("Acme", "Staff Backend Engineer", "Austin, TX", None, REQ),
    ("Acme", "Software Engineer Intern", "Boston, MA", None, REQ),
    ("Acme", "Backend Engineer", "Toronto, Ontario", None, REQ),
    ("Acme", "Backend Engineer", "London, UK", None, REQ),
    ("Acme", "Frontend Engineer", "Remote", None, REQ),
    ("Acme", "Data Engineer", "Chicago, IL", None, REQ + "Pay range: $110,000 - $140,000 per year"),
    ("Acme", "Data Engineer", "Denver, CO", None, REQ + "• 4+ years of experience building pipelines"),
    ("Acme", "Analytics Engineer", "Atlanta, GA", None, REQ),
    ("Acme", "Data Analyst", "Dallas, TX", None, REQ),
    ("Acme", "Data Scientist, New Grad", "Boston, MA", None, REQ),
    ("Acme", "Machine Learning Engineer", "Mountain View, CA", None, REQ + "$150K–$190K"),
    ("Acme", "iOS Engineer", "Hybrid - Seattle, WA", None, REQ),
    ("Acme", "Site Reliability Engineer", "Remote (US)", None, REQ + "requires an active secret clearance"),
    ("Acme", "Sales Engineer", "New York, NY", None, REQ),
    ("Acme", "Mechanical Engineer", "Detroit, MI", None, REQ),
    ("Acme", "Contract Software Engineer", "Remote", None, REQ),
    ("Acme", "Software Engineer", "Conway", "US", REQ),
    ("Acme", "Software Engineer", "Bangalore", "IN", REQ),
    ("Acme", "Software Engineer", "Remote", "CA", REQ),
    ("Globex", "Software Engineer I", "Pittsburgh, PA", None, REQ + "We pay $45/hour"),
    ("Globex", "Firmware Engineer", "San Jose, CA", None, REQ),
    ("Initech", "Software Development Engineer", "Seattle, WA / Vancouver, BC", None, REQ),
]


def facts(c, t, loc, country, desc) -> filters.Facts:
    yoe = radar.min_years(desc)
    return filters.Facts(c, t, loc, country, bool(radar.REMOTE.search(loc)), desc, yoe,
                         bool(radar.ENTRY_HINT.search(t)), filters.pay_range(desc))


def profile(**kw) -> dict:
    return config.PROFILE.clean({**config.PROFILE.default, **kw}, "p", [])


def picked(search: filters.Search, pid: str = None) -> list[tuple[str, str]]:
    out = []
    for c, t, loc, country, desc in POSTINGS:
        ids = search.matching(facts(c, t, loc, country, desc))
        if (pid in ids) if pid else ids:
            out.append((t, loc))
    return out


class DefaultProfileTests(unittest.TestCase):
    def test_default_profile_is_the_old_filter(self):
        search = filters.Search()       # what radar uses without settings
        for c, t, loc, country, desc in POSTINGS:
            with self.subTest(title=t, location=loc):
                self.assertEqual(bool(search.matching(facts(c, t, loc, country, desc))),
                                 legacy_keep(t, loc, country, desc))
        self.assertEqual(filters.Search([config.PROFILE.default]).profiles[0].seniority, {"entry"})

    def test_default_settings_profile_is_the_old_filter_too(self):
        s = config.defaults()
        search = filters.Search(s["profiles"], s["role_families"], s["companies"]["blocked"])
        self.assertEqual(picked(search), picked(filters.Search()))

    def test_prefilters_agree_with_old_ones(self):
        search = filters.Search()
        for _, t, loc, country, _ in POSTINGS:
            self.assertEqual(search.title_wanted(t), radar.is_swe_title(t) and radar.is_entry_title(t), t)
            self.assertEqual(search.location_wanted(loc, country), radar.us_location(loc, country), loc)
        self.assertEqual(search.only_country(), "US")
        self.assertFalse(search.wants_level("intern"))


class ProfileTests(unittest.TestCase):
    def test_data_engineer_any_us_city_no_visa_filters(self):
        data = profile(id="data", name="Data engineer, any US city", roles=["data"], seniority=["entry", "mid"],
                       visa={"status": "authorized", "hide_no_sponsorship": False})
        search = filters.Search([config.PROFILE.default, data])
        default, de = picked(search, "default"), picked(search, "data")
        self.assertNotEqual(default, de)
        self.assertIn(("Data Engineer", "Chicago, IL"), de)
        self.assertIn(("Analytics Engineer", "Atlanta, GA"), de)
        self.assertIn(("Data Analyst", "Dallas, TX"), de)
        self.assertIn(("Data Engineer", "Denver, CO"), de, "asks 4 years: mid level")
        self.assertNotIn(("Data Engineer", "Denver, CO"), default, "asks 4 years: not entry")
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), de)
        self.assertNotIn(("Data Analyst", "Dallas, TX"), default)

    def test_levels(self):
        cases = {
            "Software Engineer, New Grad": {"entry"},
            "Software Engineer": {"entry", "mid"},
            "Software Engineer II": {"mid"},
            "Senior Software Engineer": {"senior"},
            "Staff Backend Engineer": {"senior"},
            "Software Engineer Intern": {"intern"},
            "New Grad or Intern, Software Engineer": {"intern", "entry"},
            "Contract Software Engineer": set(),
            "Software Engineer III": {"senior"},
        }
        for title, want in cases.items():
            self.assertEqual(filters.title_levels(title), want, title)
        self.assertEqual(filters.levels("Software Engineer", 3, False), {"mid"})
        self.assertEqual(filters.levels("Software Engineer", 5, False), {"mid", "senior"})
        self.assertEqual(filters.levels("Software Engineer", 8, False), {"senior"})
        self.assertEqual(filters.levels("Software Engineer, New Grad", 3, True), {"entry"})

    def test_senior_and_intern_profiles(self):
        senior = filters.Search([profile(id="sr", seniority=["senior"])])
        got = picked(senior)
        self.assertIn(("Senior Software Engineer", "Remote - US"), got)
        self.assertIn(("Staff Backend Engineer", "Austin, TX"), got)
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), got)
        intern = filters.Search([profile(id="in", seniority=["intern"])])
        self.assertEqual(picked(intern), [("Software Engineer Intern", "Boston, MA")])
        self.assertTrue(intern.wants_level("intern"))

    def test_countries_cities_and_work_setting(self):
        ca = filters.Search([profile(id="ca", countries=["CA"], seniority=["entry", "mid"], roles=["software"])])
        got = picked(ca)
        self.assertIn(("Backend Engineer", "Toronto, Ontario"), got)
        self.assertIn(("Software Engineer", "Remote"), got, "country field CA, remote")
        self.assertIn(("Software Development Engineer", "Seattle, WA / Vancouver, BC"), got)
        self.assertIn(("Frontend Engineer", "Remote"), got, "bare Remote counts for any country")
        self.assertNotIn(("Backend Engineer", "London, UK"), got)
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), got)
        self.assertIsNone(filters.Search([config.PROFILE.default, profile(id="ca", countries=["CA"])]).only_country())
        anywhere = filters.Search([profile(id="any", countries=[])])
        self.assertIn(("Backend Engineer", "London, UK"), picked(anywhere))
        self.assertIsNone(anywhere.only_country())

        seattle = filters.Search([profile(id="sea", cities=["Seattle"], roles=["software", "mobile"])])
        got = picked(seattle)
        self.assertIn(("iOS Engineer", "Hybrid - Seattle, WA"), got)
        self.assertIn(("Frontend Engineer", "Remote"), got, "remote jobs count for every city")
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), got)
        office = filters.Search([profile(id="sea", cities=["Seattle"], roles=["software", "mobile"], workplace=["onsite", "hybrid"])])
        self.assertNotIn(("Frontend Engineer", "Remote"), picked(office))
        remote = filters.Search([profile(id="r", workplace=["remote"])])
        self.assertEqual({loc for _, loc in picked(remote)} - {"Remote", "Remote - US", "Remote (US)"}, set())
        self.assertEqual(filters.workplace("Remote / New York, NY", False), {"remote", "onsite"})
        self.assertEqual(filters.workplace("Hybrid - Seattle, WA", False), {"hybrid"})
        self.assertEqual(filters.workplace("Portland, OR", False), {"onsite"})

    def test_keywords_pay_and_companies(self):
        must = filters.Search([profile(id="m", roles=["data", "software"], seniority=["entry", "mid"], must_have=["pay range"])])
        self.assertEqual(picked(must), [("Data Engineer", "Chicago, IL")])
        excl = filters.Search([profile(id="x", roles=["infra", "software"], seniority=["entry", "mid"], exclude=["clearance"])])
        self.assertNotIn(("Site Reliability Engineer", "Remote (US)"), picked(excl))
        title = filters.Search([profile(id="t", exclude_title=["new grad"])])
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), picked(title))
        rich = filters.Search([profile(id="p", roles=["data", "ml", "software"], seniority=["entry", "mid"], salary_min=145000)])
        got = picked(rich)
        self.assertNotIn(("Data Engineer", "Chicago, IL"), got, "pays at most 140k")
        self.assertIn(("Machine Learning Engineer", "Mountain View, CA"), got)
        self.assertIn(("Software Engineer, New Grad", "New York, NY"), got, "no pay stated: kept")
        self.assertNotIn(("Software Engineer I", "Pittsburgh, PA"), got, "$45/hour is about 94k a year")
        only = filters.Search([profile(id="o", companies_allow=["globex"])])
        self.assertEqual(picked(only), [("Software Engineer I", "Pittsburgh, PA")])
        block = filters.Search([profile(id="b", companies_block=["Acme"])])
        self.assertEqual(picked(block), [("Software Engineer I", "Pittsburgh, PA"), ("Software Development Engineer", "Seattle, WA / Vancouver, BC")])
        everywhere = filters.Search([config.PROFILE.default], blocked=["ACME"])
        self.assertTrue(all(c != "Acme" for c, *_ in POSTINGS if everywhere.matching(facts(c, *_))))
        self.assertFalse(everywhere.company_wanted("Acme"))

    def test_role_families_and_custom_roles(self):
        fam = filters.BUILTIN_FAMILIES
        self.assertTrue(fam["ml"].test("Machine Learning Engineer"))
        self.assertTrue(fam["ml"].test("Applied Scientist"))
        self.assertTrue(fam["mobile"].test("Android Developer"))
        self.assertTrue(fam["infra"].test("Site Reliability Engineer"))
        self.assertTrue(fam["infra"].test("DevOps Engineer"))
        self.assertFalse(fam["infra"].test("Solutions Engineer, Cloud"))
        self.assertTrue(fam["data"].test("Business Intelligence Analyst"))
        self.assertFalse(fam["data"].test("Data Center Technician"))
        self.assertFalse(fam["data"].test("Sales Engineer"))
        custom = [{"id": "firmware", "label": "Firmware", "include": ["firmware", "embedded"], "exclude": ["hardware"]}]
        search = filters.Search([profile(id="fw", roles=["firmware"])], custom)
        self.assertEqual(picked(search), [("Firmware Engineer", "San Jose, CA")])
        self.assertFalse(search.families["firmware"].test("Firmware Hardware Engineer"))

    def test_pay_parsing(self):
        cases = {
            "The base salary range is $120,000 - $150,000.": (120000, 150000),
            "Pay: $120K–$160K plus equity": (120000, 160000),
            "USD 95,000 to 110,000 per year": (95000, 110000),
            "$30 - $40 per hour": (62400, 83200),
            "We raised $50M from investors": None,
            "a $500 learning stipend": None,
            "£55,000 - £65,000": (55000, 65000),
            "No pay here": None,
            "$150,000.00 - $180,000.00 USD": (150000, 180000),
            "$30.00 /Hr. - $30.00 /Hr. Actual starting pay will vary": (62400, 62400),
            "shaping the future of a $10T industry": None,
            "estimated to be $121B+ by 2025": None,
            "closing the year with $3.7 billion in revenue": None,
            "a $15,000 signing bonus": None,
            "up to an additional $20,000 annually": None,
            "We match up to $2000 for learning": None,
            "$150K - $190K plus bonus and equity": (150000, 190000),
            "Salary $95,000 with a $10,000 sign-on bonus": (95000, 95000),
        }
        for text, want in cases.items():
            self.assertEqual(filters.pay_range(text), want, text)


class RadarTaggingTests(unittest.TestCase):
    def setUp(self):
        saved = {k: getattr(radar, k) for k in ("SEARCH", "SETTINGS")}
        self.addCleanup(lambda: [setattr(radar, k, v) for k, v in saved.items()])
        data = profile(id="data", name="Data", roles=["data"], seniority=["entry", "mid"],
                       visa={"status": "authorized", "hide_no_sponsorship": False})
        radar.SEARCH = filters.Search([config.PROFILE.default, data])

    def job(self, title, location="Chicago, IL", desc=REQ, **kw):
        base = dict(company="Acme", title=title, location=location, country=None, remote=False, url="u/" + title,
                    source="greenhouse", posted_at=time.time() - 60, posted_exact=True, description=desc,
                    apply_url="a/" + title, verify_url="v", posted_field="first_published", posted_raw="x")
        return radar.build_job(**{**base, **kw})

    def test_build_job_tags_profiles(self):
        self.assertEqual(json.loads(self.job("Data Engineer").profiles), ["default", "data"])
        self.assertEqual(json.loads(self.job("Data Analyst").profiles), ["data"])
        self.assertEqual(json.loads(self.job("Software Engineer, New Grad").profiles), ["default"])
        self.assertIsNone(self.job("Sales Manager"))
        j = self.job("Data Engineer", desc=REQ + "Salary: $100,000 - $120,000", country="US")
        self.assertEqual((j.pay_min, j.pay_max, j.country), (100000, 120000, "US"))

    def test_store_and_retag_on_render(self):
        conn = sqlite3.connect(":memory:")
        radar.migrate(conn)
        now = time.time()
        jobs = [self.job("Data Engineer"), self.job("Data Analyst"), self.job("Software Engineer, New Grad")]
        radar.store(conn, jobs, now)

        class NoH1B:
            years = []

            def lookup(self, company):
                return None
        rows = radar.load_for_dashboard(conn, now, None, NoH1B())
        self.assertEqual({r["title"]: r["profiles"] for r in rows},
                         {"Data Engineer": ["default", "data"], "Data Analyst": ["data"],
                          "Software Engineer, New Grad": ["default"]})
        radar.save_scoring(conn, rows, {"skill_w": {}, "term_w": {}}, now)
        # The data profile is deleted: its jobs are no longer shown, and lose their tags.
        radar.SEARCH = filters.Search([config.PROFILE.default])
        rows = radar.load_for_dashboard(conn, now, None, NoH1B())
        self.assertEqual(sorted(r["title"] for r in rows), ["Data Engineer", "Software Engineer, New Grad"])
        radar.save_scoring(conn, rows, {"skill_w": {}, "term_w": {}}, now)
        tags = dict(conn.execute("SELECT title, profiles FROM jobs"))
        self.assertEqual(tags["Data Analyst"], "[]")
        self.assertEqual(json.loads(tags["Data Engineer"]), ["default"])

    def test_wider_profiles_drop_the_country_filter_from_searches(self):
        urls = []
        radar.SEARCH = filters.Search([profile(id="any", countries=[])])
        with mock.patch.object(radar, "get_json", lambda url, *a, **k: urls.append(url) or {"content": [], "totalFound": 1}):
            radar.fetch_smartrecruiters("Acme")
        self.assertNotIn("country=", urls[0])
        self.assertEqual(radar.country_param("country"), {})
        radar.SEARCH = filters.Search()
        urls.clear()
        with mock.patch.object(radar, "get_json", lambda url, *a, **k: urls.append(url) or {"content": [], "totalFound": 1}):
            radar.fetch_smartrecruiters("Acme")
        self.assertIn("country=us&", urls[0])
        self.assertEqual(radar.country_param("country", radar.ISO3), {"country": "USA"})
        self.assertEqual(radar.skipped_commitments({"contract"}, {"intern"}), {"contract", "intern"})
        radar.SEARCH = filters.Search([profile(id="i", seniority=["intern"])])
        self.assertEqual(radar.skipped_commitments({"contract"}, {"intern"}), {"contract"})

    def test_user_companies_merge_with_the_shipped_list(self):
        s = config.defaults()
        s["companies"] = {"added": {"greenhouse": ["newco"], "workday": ["acme.wd1/External"]},
                          "removed": ["greenhouse:airbnb"], "blocked": ["Palantir"], "display_names": {"newco": "NewCo"}}
        c = radar.load_companies(s)
        self.assertIn("newco", c["greenhouse"])
        self.assertNotIn("airbnb", c["greenhouse"])
        self.assertIn("acme.wd1/External", c["workday"])
        self.assertNotIn("palantir", c["lever"])
        self.assertEqual(c["display_names"]["newco"], "NewCo")


if __name__ == "__main__":
    unittest.main()
