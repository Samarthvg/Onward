"""Roles (filters.py) and how radar.py uses them.

Run:  python -m unittest test_roles
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

# --------------------------------------------------------------------------
# The radar's filter from before roles existed, kept here as the reference
# the seeded role must reproduce.

SWE_TITLE = re.compile(
    r"\b(software|sde|swe|developer|back[- ]?end|front[- ]?end|full[- ]?stack|"
    r"platform|infrastructure|mobile|ios|android|devops|site reliability|sre|"
    r"machine learning|ml|ai|data|founding|forward deployed|"
    r"distributed systems|cloud|security|web|application|compiler|embedded software)\b", re.I)
ENGINEER_WORD = re.compile(r"\b(engineer|engineering|developer|sde|swe|programmer)\b", re.I)
NOT_SWE = re.compile(
    r"\b(sales|solutions?|support|customer|field|hardware|mechanical|electrical|"
    r"manufacturing|technician|recruit\w*|account|marketing|designer|analyst|"
    r"quality assurance|qa|validation|test engineer|network engineer|process|"
    r"civil|chemical|rf|optical|facilities|propulsion|avionics|structures|"
    r"scientist|mission|integration|deployment strategist|softgoods|textile|industrial design|"
    r"product design|mechanical design)\b", re.I)
STRONG_SWE = re.compile(r"\b(software|sde|swe|developer|back[- ]?end|front[- ]?end|full[- ]?stack)\b", re.I)


def legacy_swe_title(title: str) -> bool:
    if not ENGINEER_WORD.search(title):
        return False
    if STRONG_SWE.search(title):
        return not re.search(r"\b(sales|solutions?|support|test|qa)\s+engineer|\b(marketing|recruit\w*)\b", title, re.I)
    return bool(SWE_TITLE.search(title) and not NOT_SWE.search(title))


def legacy_keep(title: str, location: str, country, description: str) -> bool:
    if not (legacy_swe_title(title) and filters.is_entry_title(title)):
        return False
    if not filters.us_location(location, country):
        return False
    yoe = radar.min_years(description)
    return not (yoe is not None and yoe >= 3 and not filters.ENTRY_HINT.search(title))


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
    ("Acme", "Full-Stack Engineer", "Denver, CO", None, REQ),
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

# Titles the old filter decides in less obvious ways: a software word beats
# a non-software word, "test engineer" also covers "test engineering", and
# words must be whole.
TRICKY_TITLES = [
    "Full-Stack Software Engineer, Mission Autonomy", "Software Quality Assurance Engineer",
    "Embedded Software Engineer, RF Payloads", "Software Test Engineering Intern", "Software Engineer in Test",
    "Solutions Engineer", "Sales Engineer, Cloud", "Software Engineer, Sales Platform", "Recruiting Engineer",
    "Data Center Technician", "Cloud Support Engineer", "Platform Engineer", "Engineer, Data Platform",
    "ML Engineer", "Machine Learning Scientist", "Developer Advocate", "Back-End Developer", "Front End Engineer",
    "Fullstack Engineer", "Network Engineer", "Security Engineer", "Hardware Engineer", "AI Engineer",
    "SDE II", "SDE2", "Software Architect", "Platforms Engineer", "Web Developer", "Customer Success Engineer",
    "Application Support Engineer", "QA Engineer", "Programmer Analyst", "Analytics Engineer",
    "Founding Engineer", "Forward Deployed Engineer", "Compiler Engineer", "Integration Engineer",
    "Mobile Developer (iOS)", "Engineering Manager, Infrastructure", "DevOps Engineer",
]


def facts(c, t, loc, country, desc) -> filters.Facts:
    yoe = radar.min_years(desc)
    return filters.Facts(c, t, loc, country, bool(filters.REMOTE.search(loc)), desc, yoe,
                         bool(filters.ENTRY_HINT.search(t)), filters.pay_range(desc))


def role(**kw) -> dict:
    return config.ROLE.clean({**config.ROLE.default, **kw}, "r", [])


def template(name: str) -> list[str]:
    return filters.ROLE_TEMPLATES[name][1]


def picked(search: filters.Search, rid: str = None) -> list[tuple[str, str]]:
    out = []
    for c, t, loc, country, desc in POSTINGS:
        ids = search.matching(facts(c, t, loc, country, desc))
        if (rid in ids) if rid else ids:
            out.append((t, loc))
    return out


class SeededRoleTests(unittest.TestCase):
    """The role a new install starts with keeps what the radar always kept."""

    def test_title_keywords_are_the_old_filter(self):
        for _, t, *_ in POSTINGS:
            self.assertEqual(filters.is_swe_title(t), legacy_swe_title(t), t)
        for t in TRICKY_TITLES:
            self.assertEqual(filters.is_swe_title(t), legacy_swe_title(t), t)

    def test_seeded_role_is_the_old_filter(self):
        search = filters.Search()       # what radar uses without settings
        for c, t, loc, country, desc in POSTINGS:
            with self.subTest(title=t, location=loc):
                self.assertEqual(bool(search.matching(facts(c, t, loc, country, desc))),
                                 legacy_keep(t, loc, country, desc))
        s = config.defaults()
        self.assertEqual(picked(filters.Search(s["roles"], s["companies"]["blocked"])), picked(search))

    def test_prefilters_agree_with_old_ones(self):
        search = filters.Search()
        for _, t, loc, country, _ in POSTINGS:
            self.assertEqual(search.title_stage(t) is None, legacy_swe_title(t) and filters.is_entry_title(t), t)
            self.assertEqual(search.location_wanted(loc, country), filters.us_location(loc, country), loc)
        self.assertEqual(search.only_country(), "US")
        self.assertFalse(search.wants_level("intern"))


class TitleKeywordTests(unittest.TestCase):
    def test_syntax(self):
        r = filters.TitleRules(["- manager/director", "full stack/fullstack", "ios/android + engineer/developer",
                                "recruit*", "   ", "- /"])
        self.assertEqual(len(r.rules), 4, "lines without words are ignored")
        self.assertTrue(r.matches("Full-Stack Engineer"))
        self.assertTrue(r.matches("Fullstack Developer"))
        self.assertFalse(r.matches("Full Stack Engineering Manager"), "a drop line above wins")
        self.assertTrue(r.matches("Engineer, Android Platform"), "parts in any order")
        self.assertFalse(r.matches("Android Designer"), "every part must appear")
        self.assertTrue(r.matches("Recruiting Coordinator"))
        self.assertFalse(r.matches("Stack Full Engineer"), "a phrase's words stay in order")
        self.assertFalse(r.matches("Data Engineer"), "no line fits: left out")
        self.assertFalse(filters.TitleRules([]).matches("Anything"))
        order = filters.TitleRules(["full stack", "- manager"])
        self.assertTrue(order.matches("Full Stack Manager"), "the first line that fits decides")

    def test_templates_are_only_title_keywords(self):
        self.assertEqual(template("blank"), [])
        self.assertIs(template("software"), filters.SOFTWARE_KEYWORDS)
        for name, (label, kw) in filters.ROLE_TEMPLATES.items():
            self.assertIsInstance(label, str)
            if kw:
                self.assertTrue(filters.TitleRules(kw).rules, name)
        cases = {
            "data": (["Data Engineer", "Business Intelligence Analyst", "Analytics Engineer", "Data Scientist"],
                     ["Data Center Technician", "Clinical Data Specialist", "Sales Engineer", "Insights Analyst"]),
            "ml": (["Machine Learning Engineer", "Applied Scientist", "Research Engineer, NLP"], ["ML Sales Lead"]),
            "mobile": (["Android Developer", "iOS Engineer"], ["Mobile Sales Engineer"]),
            "infra": (["Site Reliability Engineer", "DevOps Engineer", "Cloud Infrastructure Engineer"],
                      ["Solutions Engineer, Cloud"]),
            "fullstack": (["Full-Stack Engineer", "Backend Developer"], ["Customer Support Engineer"]),
        }
        for name, (yes, no) in cases.items():
            rules = filters.TitleRules(template(name))
            for t in yes:
                self.assertTrue(rules.matches(t), (name, t))
            for t in no:
                self.assertFalse(rules.matches(t), (name, t))


class RoleTests(unittest.TestCase):
    def test_data_engineer_any_us_city_no_visa_filters(self):
        data = role(id="data", name="Data engineer, any US city", title_keywords=template("data"),
                    seniority=["entry", "mid"], visa={"status": "authorized", "hide_no_sponsorship": False})
        search = filters.Search([config.ROLE.default, data])
        default, de = picked(search, "software"), picked(search, "data")
        self.assertNotEqual(default, de)
        self.assertIn(("Data Engineer", "Chicago, IL"), de)
        self.assertIn(("Analytics Engineer", "Atlanta, GA"), de)
        self.assertIn(("Data Analyst", "Dallas, TX"), de)
        self.assertIn(("Data Engineer", "Denver, CO"), de, "asks 4 years: mid level")
        self.assertNotIn(("Data Engineer", "Denver, CO"), default, "asks 4 years: not entry")
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), de)
        self.assertNotIn(("Data Analyst", "Dallas, TX"), default)

    def test_user_written_role(self):
        fs = filters.Search([role(id="fs", name="Full-stack", title_keywords=["- manager", "full stack/fullstack"],
                                  seniority=["entry", "mid"])])
        self.assertEqual(picked(fs), [("Full-Stack Engineer", "Denver, CO")])

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

    def test_senior_and_intern_roles(self):
        senior = filters.Search([role(id="sr", seniority=["senior"])])
        got = picked(senior)
        self.assertIn(("Senior Software Engineer", "Remote - US"), got)
        self.assertIn(("Staff Backend Engineer", "Austin, TX"), got)
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), got)
        intern = filters.Search([role(id="in", seniority=["intern"])])
        self.assertEqual(picked(intern), [("Software Engineer Intern", "Boston, MA")])
        self.assertTrue(intern.wants_level("intern"))

    def test_countries_cities_and_work_setting(self):
        ca = filters.Search([role(id="ca", countries=["CA"], seniority=["entry", "mid"])])
        got = picked(ca)
        self.assertIn(("Backend Engineer", "Toronto, Ontario"), got)
        self.assertIn(("Software Engineer", "Remote"), got, "country field CA, remote")
        self.assertIn(("Software Development Engineer", "Seattle, WA / Vancouver, BC"), got)
        self.assertIn(("Frontend Engineer", "Remote"), got, "bare Remote counts for any country")
        self.assertNotIn(("Backend Engineer", "London, UK"), got)
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), got)
        self.assertIsNone(filters.Search([config.ROLE.default, role(id="ca", countries=["CA"])]).only_country())
        anywhere = filters.Search([role(id="any", countries=[])])
        self.assertIn(("Backend Engineer", "London, UK"), picked(anywhere))
        self.assertIsNone(anywhere.only_country())

        both = filters.SOFTWARE_KEYWORDS + template("mobile")
        seattle = filters.Search([role(id="sea", cities=["Seattle"], title_keywords=both)])
        got = picked(seattle)
        self.assertIn(("iOS Engineer", "Hybrid - Seattle, WA"), got)
        self.assertIn(("Frontend Engineer", "Remote"), got, "remote jobs count for every city")
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), got)
        office = filters.Search([role(id="sea", cities=["Seattle"], title_keywords=both, workplace=["onsite", "hybrid"])])
        self.assertNotIn(("Frontend Engineer", "Remote"), picked(office))
        remote = filters.Search([role(id="r", workplace=["remote"])])
        self.assertEqual({loc for _, loc in picked(remote)} - {"Remote", "Remote - US", "Remote (US)"}, set())
        self.assertEqual(filters.workplace("Remote / New York, NY", False), {"remote", "onsite"})
        self.assertEqual(filters.workplace("Hybrid - Seattle, WA", False), {"hybrid"})
        self.assertEqual(filters.workplace("Portland, OR", False), {"onsite"})

    def test_keywords_pay_and_companies(self):
        wide = filters.SOFTWARE_KEYWORDS + template("data")
        must = filters.Search([role(id="m", title_keywords=wide, seniority=["entry", "mid"], must_have=["pay range"])])
        self.assertEqual(picked(must), [("Data Engineer", "Chicago, IL")])
        infra = filters.SOFTWARE_KEYWORDS + template("infra")
        excl = filters.Search([role(id="x", title_keywords=infra, seniority=["entry", "mid"], exclude=["clearance"])])
        self.assertNotIn(("Site Reliability Engineer", "Remote (US)"), picked(excl))
        title = filters.Search([role(id="t", title_keywords=["- new grad"] + filters.SOFTWARE_KEYWORDS)])
        self.assertNotIn(("Software Engineer, New Grad", "New York, NY"), picked(title))
        rich = filters.Search([role(id="p", title_keywords=filters.SOFTWARE_KEYWORDS + template("data") + template("ml"),
                                    seniority=["entry", "mid"], salary_min=145000)])
        got = picked(rich)
        self.assertNotIn(("Data Engineer", "Chicago, IL"), got, "pays at most 140k")
        self.assertIn(("Machine Learning Engineer", "Mountain View, CA"), got)
        self.assertIn(("Software Engineer, New Grad", "New York, NY"), got, "no pay stated: kept")
        self.assertNotIn(("Software Engineer I", "Pittsburgh, PA"), got, "$45/hour is about 94k a year")
        only = filters.Search([role(id="o", companies_allow=["globex"])])
        self.assertEqual(picked(only), [("Software Engineer I", "Pittsburgh, PA")])
        block = filters.Search([role(id="b", companies_block=["Acme"])])
        self.assertEqual(picked(block), [("Software Engineer I", "Pittsburgh, PA"), ("Software Development Engineer", "Seattle, WA / Vancouver, BC")])
        everywhere = filters.Search([config.ROLE.default], blocked=["ACME"])
        self.assertFalse(any(everywhere.matching(facts(*p)) for p in POSTINGS if p[0] == "Acme"))

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
        data = role(id="data", name="Data", title_keywords=template("data"), seniority=["entry", "mid"],
                    visa={"status": "authorized", "hide_no_sponsorship": False})
        radar.SEARCH = filters.Search([config.ROLE.default, data])

    def job(self, title, location="Chicago, IL", desc=REQ, **kw):
        base = dict(company="Acme", title=title, location=location, country=None, remote=False, url="u/" + title,
                    source="greenhouse", posted_at=time.time() - 60, posted_exact=True, description=desc,
                    apply_url="a/" + title, verify_url="v", posted_field="first_published", posted_raw="x")
        return radar.build_job(**{**base, **kw})

    def test_build_job_tags_roles(self):
        self.assertEqual(json.loads(self.job("Data Engineer").roles), ["software", "data"])
        self.assertEqual(json.loads(self.job("Data Analyst").roles), ["data"])
        self.assertEqual(json.loads(self.job("Software Engineer, New Grad").roles), ["software"])
        self.assertIsNone(self.job("Sales Manager"))
        j = self.job("Data Engineer", desc=REQ + "Salary: $100,000 - $120,000", country="US")
        self.assertEqual((j.pay_min, j.pay_max, j.country), (100000, 120000, "US"))

    def test_store_and_retag_on_render(self):
        conn = sqlite3.connect(":memory:")
        radar.migrate(conn)
        now = time.time()
        radar.store(conn, [self.job("Data Engineer"), self.job("Data Analyst"), self.job("Software Engineer, New Grad")], now)

        class NoH1B:
            years = []

            def lookup(self, company):
                return None
        rows = radar.load_for_dashboard(conn, now, None, NoH1B())
        self.assertEqual({r["title"]: r["roles"] for r in rows},
                         {"Data Engineer": ["software", "data"], "Data Analyst": ["data"],
                          "Software Engineer, New Grad": ["software"]})
        radar.save_scoring(conn, rows, {"skill_w": {}, "term_w": {}}, now)
        # The data role is deleted: its jobs are no longer shown, and lose their tags.
        radar.SEARCH = filters.Search([config.ROLE.default])
        rows = radar.load_for_dashboard(conn, now, None, NoH1B())
        self.assertEqual(sorted(r["title"] for r in rows), ["Data Engineer", "Software Engineer, New Grad"])
        radar.save_scoring(conn, rows, {"skill_w": {}, "term_w": {}}, now)
        tags = dict(conn.execute("SELECT title, roles FROM jobs"))
        self.assertEqual(tags["Data Analyst"], "[]")
        self.assertEqual(json.loads(tags["Data Engineer"]), ["software"])

    def test_profiles_column_becomes_roles(self):
        conn = sqlite3.connect(":memory:")
        radar.migrate(conn)
        conn.execute("ALTER TABLE jobs RENAME COLUMN roles TO profiles")
        conn.execute("INSERT INTO jobs (key, company, title, location, remote, url, source, posted_exact, sponsorship, "
                     "sponsor_note, entry_hint, first_seen, last_seen, profiles) "
                     "VALUES ('k', 'Acme', 'SWE', 'NY', 0, 'u', 'greenhouse', 1, 'unknown', '', 0, 1, 1, '[\"default\"]')")
        radar.migrate(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        self.assertIn("roles", cols)
        self.assertNotIn("profiles", cols)
        self.assertEqual(conn.execute("SELECT roles FROM jobs").fetchone()[0], '["default"]')

    def test_wider_roles_drop_the_country_filter_from_searches(self):
        urls = []
        radar.SEARCH = filters.Search([role(id="any", countries=[])])
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
        radar.SEARCH = filters.Search([role(id="i", seniority=["intern"])])
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
