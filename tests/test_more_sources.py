"""The job boards added in 0.1.27: Workable, BambooHR, Rippling, Breezy HR,
Paylocity, ADP Workforce Now, Jobvite, JazzHR, Pinpoint, Comeet, SAP
SuccessFactors, Avature and Taleo. Every answer is a fake shaped like the
real one (checked against live boards when these were written).

Run:  python -m unittest tests.test_more_sources
"""

import json
import unittest
from unittest import mock

import ats
import radar
from tests.test_sources import FakeWeb, SourceTestCase


class NewSourceTest(SourceTestCase):
    """Names come from each board's own feed here, not from companies.json,
    which other tests load into radar.DISPLAY_NAMES."""

    def setUp(self) -> None:
        super().setUp()
        p = mock.patch.dict(radar.DISPLAY_NAMES, clear=True)
        p.start()
        self.addCleanup(p.stop)


def jsonld(title, place="New York, NY", country="US", date="2026-11-10", body="<p>Build things in Python.</p>"):
    city, region = place.split(", ")
    item = {"@type": "JobPosting", "title": title, "datePosted": date, "description": body,
            "jobLocation": {"address": {"addressLocality": city, "addressRegion": region, "addressCountry": country}}}
    return f'<html><script type="application/ld+json">{json.dumps(item)}</script></html>'


class WorkableTests(NewSourceTest):
    def test_rows_per_location_make_one_job(self):
        row = {"title": "Software Engineer", "shortcode": "AADE688F5F", "employment_type": "Full-time",
               "telecommuting": False, "published_on": "2026-11-02", "description": "<p>C++ and Python</p>",
               "url": "https://apply.workable.com/j/AADE688F5F", "country": "United States"}
        data = {"name": "Trexquant Investment", "jobs": [
            {**row, "locations": [{"city": "Stamford", "region": "Connecticut", "country": "United States", "countryCode": "US"}]},
            {**row, "locations": [{"city": "New York", "region": "New York", "country": "United States", "countryCode": "US"}]},
            {**row, "shortcode": "CONTRACT01", "title": "Software Engineer", "employment_type": "Contract", "locations": []}]}
        self.web({"/widget/accounts/trexquant?details=true": data})
        jobs = radar.fetch_workable("trexquant")
        self.assertEqual(len(jobs), 1, "one job across its locations; the contract one is skipped")
        j = jobs[0]
        self.assertEqual((j.company, j.source, j.posted_raw, j.posted_exact), ("Trexquant Investment", "workable", "2026-11-02", False))
        self.assertEqual(j.location, "Stamford, Connecticut, United States / New York, New York, United States")
        self.assertEqual(j.url, "https://apply.workable.com/trexquant/j/AADE688F5F/", "the account is in the link")
        self.assertIsNotNone(radar.posting_loader(j.url), "and the posting reader takes it")

    def test_check(self):
        self.web({"/widget/accounts/gone": radar.BoardMissing})
        with mock.patch.object(radar, "get_json", FakeWeb({"/accounts/gone": _http(404)})):
            self.assertFalse(ats.check(ats.Board("workable", "gone"))["ok"])


def _http(code):
    import urllib.error
    def raise_it(url):
        raise urllib.error.HTTPError(url, code, "x", {}, None)
    return raise_it


class BambooHRTests(NewSourceTest):
    def test_list_then_details_for_titles_that_pass(self):
        web = self.web({
            "/careers/company-info": {"result": {"name": "Acme Inc", "inTrial": False}},
            "/careers/list": {"result": [
                {"id": "7", "jobOpeningName": "Software Engineer", "employmentStatusLabel": "Full-Time",
                 "location": {"city": "Austin", "state": "Texas"}, "locationType": "0"},
                {"id": "8", "jobOpeningName": "Account Executive", "employmentStatusLabel": "Full-Time",
                 "location": {"city": "Austin", "state": "Texas"}},
                {"id": "9", "jobOpeningName": "Software Engineer", "employmentStatusLabel": "Part-Time",
                 "location": {"city": "Austin", "state": "Texas"}}]},
            "/careers/7/detail": {"result": {"jobOpening": {
                "jobOpeningName": "Software Engineer", "datePosted": "2026-11-09", "description": "<p>Go and SQL</p>",
                "location": {"city": "Austin", "state": "Texas", "addressCountry": "United States"},
                "jobOpeningShareUrl": "https://acme.bamboohr.com/careers/7"}}}})
        jobs = radar.fetch_bamboohr("acme")
        self.assertEqual([u for u in web.calls if "/detail" in u], ["https://acme.bamboohr.com/careers/7/detail"])
        self.assertEqual((jobs[0].company, jobs[0].posted_raw, jobs[0].url), ("Acme Inc", "2026-11-09",
                                                                             "https://acme.bamboohr.com/careers/7"))

    def test_trial_accounts_show_sample_jobs(self):
        self.web({"/careers/company-info": {"result": {"name": "Kin", "inTrial": True}}})
        self.assertEqual(radar.fetch_bamboohr("kin"), [])

    def test_an_unknown_account_redirects_home(self):
        self.web({"/careers/company-info": ValueError})
        with self.assertRaises(radar.BoardMissing):
            radar.fetch_bamboohr("nope")


class RipplingTests(NewSourceTest):
    def test_rows_merge_and_details_date_them(self):
        loc = lambda c, s: {"city": c, "stateCode": s, "countryCode": "US", "workplaceType": "ON_SITE"}
        self.web({
            "/board/edgehog/jobs?page=0": {"items": [
                {"id": "a1", "name": "Graduate Software Engineer", "locations": [loc("Chicago", "IL")]},
                {"id": "a1", "name": "Graduate Software Engineer", "locations": [loc("New York", "NY")]},
                {"id": "b2", "name": "Software Engineer", "locations": [loc("Chicago", "IL")]}], "totalPages": 1},
            "/board/edgehog/jobs/a1": {"name": "Graduate Software Engineer", "createdOn": "2026-09-02T13:55:34.642000-07:00",
                                       "employmentType": {"label": "SALARIED_FT"},
                                       "description": {"company": "<p>We trade.</p>", "role": "<p>C++</p>"}},
            "/board/edgehog/jobs/b2": {"name": "Software Engineer", "createdOn": "2026-09-02T13:55:34-07:00",
                                       "employmentType": {"label": "PART_TIME"}, "description": {}}})
        jobs = radar.fetch_rippling("edgehog")
        self.assertEqual(len(jobs), 1, "the part-time one is dropped")
        self.assertEqual((jobs[0].location, jobs[0].posted_exact), ("Chicago, IL, US / New York, NY, US", True))
        self.assertIn("C++", jobs[0].description)


class BreezyTests(NewSourceTest):
    def test_feed(self):
        self.web({"zero-hash.breezy.hr/json": [
            {"name": "Software Engineer", "url": "https://zero-hash.breezy.hr/p/421c5b813857-software-engineer",
             "published_date": "2026-10-15T13:57:39.360Z", "type": {"id": "fullTime"}, "company": {"name": "zerohash"},
             "location": {"country": {"id": "US"}, "state": {"id": "NY"}, "city": "New York"}, "description": "<p>Rust</p>"},
            {"name": "Software Engineer", "url": "https://zero-hash.breezy.hr/p/x", "type": {"id": "partTime"},
             "location": {"country": {"id": "US"}}}]})
        jobs = radar.fetch_breezy("zero-hash")
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].company, jobs[0].location, jobs[0].apply_url),
                         ("zerohash", "New York, NY, US", "https://zero-hash.breezy.hr/p/421c5b813857-software-engineer/apply"))


class PaylocityTests(NewSourceTest):
    def test_page_data_dated_by_the_list(self):
        board = "window.pageData = " + json.dumps({"ModuleTitle": "Spatial Front, Inc", "Jobs": [
            {"JobId": 1, "JobTitle": "Software Developer", "PublishedDate": "2026-11-13T08:15:19-06:00",
             "JobLocation": {"City": "Arlington", "State": "VA", "Country": "USA"}, "IsRemote": False, "IsInternal": False},
            {"JobId": 2, "JobTitle": "Software Developer", "IsInternal": True, "JobLocation": {"Country": "USA"}},
            {"JobId": 3, "JobTitle": "Software Developer (Contractor)", "JobLocation": {"Country": "USA"}}]}) + ";</script>"
        self.web({}, text={"/jobs/All/abc": board, "/Jobs/Details/1": jsonld("Software Developer", "Arlington, VA", date="2026-11-14")})
        jobs = radar.fetch_paylocity("abc")
        self.assertEqual(len(jobs), 1, "internal and contractor postings are left out")
        j = jobs[0]
        self.assertEqual((j.company, j.posted_raw, j.posted_field), ("Spatial Front, Inc", "2026-11-13T08:15:19-06:00", "PublishedDate"),
                         "the list's date, not the page's, which is today's")
        self.assertEqual(j.apply_url, "https://recruiting.paylocity.com/Recruiting/Jobs/Apply/1")

    def test_no_board(self):
        self.web({}, text={"/jobs/All/gone": "<html>Job not found</html>"})
        with self.assertRaises(radar.BoardMissing):
            radar.fetch_paylocity("gone")


class AdpTests(NewSourceTest):
    def req(self, i, title, place, level="Full Time"):
        return {"itemID": f"id{i}", "requisitionTitle": title, "workLevelCode": {"shortName": level},
                "requisitionLocations": [{"nameCode": {"shortName": place}}],
                "customFieldGroup": {"dateFields": [{"nameCode": {"codeValue": "PostingDate"}, "dateValue": "2026-11-12T15:23Z"}],
                                     "stringFields": [{"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": f"6344{i}"}]}}

    def test_pages_and_places(self):
        page1 = {"jobRequisitions": [self.req(i, "Account Manager", " Austin, TX, US") for i in range(19)]
                 + [self.req(19, "Software Engineer", " Austin, TX, US")], "meta": {"totalNumber": 22}}
        page2 = {"jobRequisitions": [self.req(20, "Software Engineer", "JPN - Virtual"),
                                     self.req(21, "Software Engineer", "USA - Virtual, US", "Hourly Part-Time")],
                 "meta": {"totalNumber": 22}}
        web = self.web({"$skip=0": page1, "$skip=20": page2, "/job-requisitions/id19?": {"requisitionDescription": "<p>Java</p>"}})
        jobs = radar.fetch_adp("cid1|19000101_000001")
        self.assertEqual([j.title for j in jobs], ["Software Engineer"], "Japan and part-time are left out")
        self.assertEqual(jobs[0].location, "Austin, TX, US")
        self.assertIn("jobId=634419", jobs[0].url)
        self.assertEqual(sum("$skip" in u for u in web.calls), 2)
        self.assertEqual(radar._adp_place({"requisitionLocations": [{"nameCode": {"shortName": "USA - Virtual"}}]})[1], "US")


class JobviteTests(NewSourceTest):
    def test_search_pages_and_posting_data(self):
        row = lambda path, title, place: (f'<td class="jv-job-list-name"><a href="/egnyte/job/{path}">{title}</a></td>'
                                          f'<td class="jv-job-list-location">{place}</td>')
        p0 = row("aaa", "Software Engineer", "Raleigh, North Carolina") + row("bbb", "Sales Lead", "Raleigh") + "jv-pagination-next"
        p1 = row("ccc", "Software Developer", "4 Locations")
        self.web({}, text={"search/?p=0": p0, "search/?p=1": p1,
                           "/job/aaa": jsonld("Software Engineer", "Raleigh, North Carolina"),
                           "/job/ccc": jsonld("Software Developer", "Draper, Utah")})
        jobs = radar.fetch_jobvite("egnyte")
        self.assertEqual(sorted(j.title for j in jobs), ["Software Developer", "Software Engineer"])
        self.assertTrue(all(j.apply_url.endswith("/apply") for j in jobs))


class JazzHRTests(NewSourceTest):
    def test_board_page(self):
        row = ("<h3 class='list-group-item-heading'>\n<a href=\"https://analytica.applytojob.com/apply/3RwUdDpZMh/Software-Engineer\">"
               "\n Software Engineer </a>\n</h3>\n<ul class='list-inline list-group-item-text'><li><i class='fa'></i>Bethesda, MD</li></ul>")
        self.web({}, text={"analytica.applytojob.com/apply/3Rw": jsonld("Software Engineer", "Bethesda, MD"),
                           "analytica.applytojob.com/apply": row})
        jobs = radar.fetch_jazzhr("analytica")
        self.assertEqual([(j.title, j.location) for j in jobs], [("Software Engineer", "Bethesda, MD, US")])


class PinpointTests(NewSourceTest):
    def test_list_with_dates_from_the_posting(self):
        self.web({"groupo.pinpointhq.com/postings.json": {"data": [
            {"title": "Software Engineer", "url": "https://groupo.pinpointhq.com/en/postings/1f71", "employment_type": "full_time",
             "workplace_type": "onsite", "location": {"city": "Fort Worth", "province": "Texas", "name": "Fort Worth, Texas (409)"},
             "description": "<p>Python</p>"},
            {"title": "Software Engineer", "url": "https://groupo.pinpointhq.com/en/postings/2", "employment_type": "contract",
             "location": {}}]}},
            text={"/postings/1f71": jsonld("Software Engineer", "Fort Worth, Texas", date="2026-06-26T14:45:27+01:00")})
        jobs = radar.fetch_pinpoint("groupo")
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].posted_raw, jobs[0].description), ("2026-06-26T14:45:27+01:00", "Python"))

    def test_a_posting_robots_keeps_out_is_undated(self):
        self.web({"x.pinpointhq.com/postings.json": {"data": [
            {"title": "Software Engineer", "url": "https://x.pinpointhq.com/en/postings/9", "employment_type": "full_time",
             "location": {"city": "Austin", "province": "TX"}, "description": "<p>Go</p>"}]}},
            text={"/postings/9": radar.Disallowed})
        jobs = radar.fetch_pinpoint("x")
        self.assertEqual((len(jobs), jobs[0].posted_at), (1, None))


class ComeetTests(NewSourceTest):
    def test_a_job_in_several_places_is_one(self):
        pos = lambda uid, place: {"name": "Software Engineer", "uid": uid, "employment_type": "Full-time",
                                  "location": {"name": place, "country": "US"}, "time_updated": "2026-10-30T22:05:56Z",
                                  "url_comeet_hosted_page": f"https://www.comeet.com/jobs/vastdata/43.001/se/{uid}",
                                  "details": [{"name": "Description", "value": "<p>Storage</p>"}]}
        self.web({"/company/43.001/positions": [pos("A", "Austin, TX"), pos("B", "Seattle, WA")]})
        jobs = radar.fetch_comeet("43.001|TOKEN")
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].location, jobs[0].posted_at, jobs[0].posted_field),
                         ("Austin, TX / Seattle, WA", None, "time_updated"), "only an update time: dated when first seen")

    def test_the_token_comes_from_the_board_page(self):
        with mock.patch.object(radar, "get_text", FakeWeb({"/jobs/vastdata/43.001": 'x = {"token": "34110453411D4968234168209C31D49"}'})):
            b = ats.complete_board(ats.detect("https://www.comeet.com/jobs/vastdata/43.001"), "")
        self.assertEqual((b.token, b.name), ("43.001|34110453411D4968234168209C31D49", "Vastdata"))


SF_ROW = ('<tr class="data-row"><td><a href="/Nomura/job/New-York-Software-Engineer-NY-10019/1440430900/" class="jobTitle-link">'
          'Software Engineer</a><span class="jobLocation"> New York, NY, US, 10019 </span></td></tr>')
SF_POSTING = ('<body class="coreCSB job-page bodyNomura" id="body"><span itemprop="title" class="x">Software Engineer </span>'
              '<meta itemprop="addressLocality" content="New York"><meta itemprop="addressRegion" content="NY">'
              '<meta itemprop="addressCountry" content="US"><meta itemprop="datePosted" content="Fri Nov 13 08:00:00 UTC 2026">'
              '<span itemprop="description" class="x"><span class="jobdescription"><p>Python &amp; SQL</p></span></span>'
              '<div class="jobmarkets">Markets</div></body>')


class SuccessFactorsTests(NewSourceTest):
    def test_search_pages_and_microdata(self):
        self.web({}, text={"startrow=0": "Results <b>1 – 1</b> of <b>1</b>" + SF_ROW, "/1440430900/": SF_POSTING})
        jobs = radar.fetch_successfactors("careers.nomura.com|/Nomura")
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j.location, j.posted_raw, j.description), ("New York, NY, US", "Fri Nov 13 08:00:00 UTC 2026", "Python & SQL"))
        self.assertEqual(j.url, "https://careers.nomura.com/Nomura/job/New-York-Software-Engineer-NY-10019/1440430900/")

    def test_a_closed_posting_lands_on_the_home_page(self):
        with mock.patch.object(radar, "get_text", FakeWeb({"1000000001": '<body class="coreCSB home-page body" id="body">'})):
            self.assertIs(radar._still_open("https://careers.nomura.com/Nomura/job/Gone/1000000001/"), False)
        with mock.patch.object(radar, "get_text", FakeWeb({"/1440430900/": SF_POSTING})):
            self.assertIs(radar._still_open("https://careers.nomura.com/Nomura/job/x/1440430900/"), True)

    def test_found_from_a_page_on_the_company_domain(self):
        b = ats.board_on_page("https://careers.nomura.com/Nomura/job/x/1/", '<link href="https://rmkcdn.successfactors.com/a.css">')
        self.assertEqual((b.source, b.token, ats.default_name(b)), ("successfactors", "careers.nomura.com|/Nomura", "Nomura"))


class AvatureTests(NewSourceTest):
    def test_sitemap_titles_then_posting_fields(self):
        sitemap = ("<urlset><url><loc>https://bloomberg.avature.net/careers/JobDetail/Software-Engineer-2027/46000</loc></url>"
                   "<url><loc>https://bloomberg.avature.net/careers/JobDetail/Sales-Representative/46001</loc></url>"
                   "<url><loc>https://bloomberg.avature.net/careers/Login</loc></url></urlset>")
        page = ('<h2 class="article__header__text__title title">Software Engineer 2027</h2>'
                '<div class="article__content__view__field__label"> Location </div>'
                '<div class="article__content__view__field__value"> New York </div></div>'
                '<div class="article__content__view__field field--rich-text"><p>C++ &amp; Python</p></div></div>')
        pages = FakeWeb({"/careers/sitemap.xml": sitemap, "/46000": page})
        with mock.patch.object(radar, "get_text", pages):
            jobs = radar.fetch_avature("bloomberg.avature.net|/careers")
        self.assertEqual([(j.title, j.location, j.description) for j in jobs], [("Software Engineer 2027", "New York", "C++ & Python")])
        self.assertFalse(any("/46001" in u for u in pages.calls), "a title that doesn't pass isn't opened")


class AvatureTitleTests(NewSourceTest):
    def test_titles_places_and_descriptions_as_each_portal_writes_them(self):
        # Bloomberg: "<title with a dash> - <id> - <company>", section headings on the page.
        bloomberg = ('<title>\n Senior Software Engineer - Hadoop Infrastructure\n - 45958\n - Bloomberg </title>'
                     '<h2 class="article__header__text__title">Description &amp; Requirements</h2>')
        # MetLife: "<title> - <place> - <id> - <company>", "Location" left as the location's value,
        # the description in a long field.
        metlife = ('<title> Big Data Engineer II - Pune, India - 21452 - MetLife </title>'
                   '<div class="article__content__view__field__label"> Location </div>'
                   '<div class="article__content__view__field__value"> Location </div></div>'
                   '<div class="article__content__view__field__value fieldSpec--odd">' + "<p>Spark and Scala.</p>" * 30 + '</div>')
        pages = FakeWeb({"/45958": bloomberg, "/21452": metlife, "/1": "<title>Error | Bloomberg</title>"})
        with mock.patch.object(radar, "get_text", pages):
            b = radar._avature_posting("https://bloomberg.avature.net/careers/JobDetail/Senior-Software-Engineer-Hadoop-Infrastructure/45958")
            m = radar._avature_posting("https://www.metlifecareers.com/en_US/ml/JobDetail/Big-Data-Engineer-II/21452")
            self.assertIs(radar._still_open("https://bloomberg.avature.net/careers/JobDetail/Gone/1"), False)
        self.assertEqual(b["title"], "Senior Software Engineer - Hadoop Infrastructure")
        self.assertEqual((m["title"], m["location"]), ("Big Data Engineer II", "Pune, India"))
        self.assertTrue(m["description"].startswith("Spark and Scala."))
        self.assertEqual(radar.site_name("www.metlifecareers.com"), "Metlife")


class TaleoTests(NewSourceTest):
    def test_newest_first_until_the_window_and_the_description(self):
        rows = lambda *titles: {"requisitionList": [
            {"jobId": str(i), "contestNo": f"C{i}", "column": [t, '["UNITED STATES-NY-New York"]', d], "linkedColumn": 0,
             "locationsColumns": [1]} for i, (t, d) in enumerate(titles)], "pagingData": {"pageSize": 25, "totalCount": 3}}
        pages = []

        def search(url, body, *a, **k):
            pages.append(body["pageNo"])
            return rows(("Software Engineer", "Nov 12, 2026"), ("Sales Manager", "Nov 11, 2026"), ("Software Engineer II", "Jan 2, 2020"))
        history = ("ftlx0!|!Software Engineer!|!Full-time!|!!*!" + "%3Cp%3EJava%3C%2Fp%3E" + "!|!!*!%3Cp%3EJava%3C%2Fp%3E")
        detail = f'<input type="hidden" id="initialHistory" value="{history}">'
        with mock.patch.object(radar, "FETCH_DAYS", 400), mock.patch.object(radar, "post_json", search), \
                mock.patch.object(radar, "get_text", FakeWeb({"jobdetail.ftl?job=C0": detail})):
            jobs = radar.fetch_taleo("equitable|eqh_1|361")
        self.assertEqual(pages, [1])
        self.assertEqual([(j.title, j.location, j.description) for j in jobs],
                         [("Software Engineer", "New York, NY, United States", "Java")], "the 2020 one ends the read")

    def test_portal_found_from_the_career_section(self):
        with mock.patch.object(radar, "get_text", FakeWeb({"/careersection/1/jobsearch.ftl": '<a href="x?lang=en&portal=460210089">'})):
            b = ats.complete_board(ats.detect("https://hyatt.taleo.net/careersection/1/jobdetail.ftl?job=1"), "")
        self.assertEqual(b.token, "hyatt|1|460210089")


class RegistryTests(unittest.TestCase):
    def test_every_source_has_a_fetcher_check_and_label(self):
        import config
        from pathlib import Path
        page = (Path(__file__).resolve().parent.parent / "dashboard_template.html").read_text(encoding="utf-8")
        for source in config.SOURCES:
            self.assertIn(source, radar.FETCHERS, source)
            self.assertIn(source, ats.CHECKS, source)
            self.assertRegex(page, rf"\b{source}: \"", source)

    def test_shipped_boards_fit_their_fetchers(self):
        c = json.loads(radar.COMPANIES_PATH.read_text(encoding="utf-8"))
        for source in ("comeet", "adp", "taleo", "successfactors", "avature"):
            for token in c.get(source, []):
                self.assertTrue(token, source)
                if source == "comeet":
                    self.assertRegex(token, r"^[0-9A-F]{2}\.[0-9A-F]{3}\|[0-9A-F]+$")
                if source == "taleo":
                    self.assertEqual(len(token.split("|")), 3)
                    self.assertTrue(token.split("|")[2].isdigit())
                if source in ("successfactors", "avature"):
                    self.assertNotIn("/", token.split("|")[0])


if __name__ == "__main__":
    unittest.main()
