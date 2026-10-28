"""Tests for discover.py: harvesting boards from public lists, checking
them once, and handing tracked ones to radar.py on a schedule.

Run:  python -m unittest tests.test_discover
"""

import json
import sqlite3
import unittest
from collections import Counter
from unittest import mock

import discover
import radar

MARKDOWN = """
| Company | Role | Location | Application |
| --- | --- | --- | --- |
| **[Acme](https://acme.com)** | Software Engineer | NYC | <a href="https://job-boards.greenhouse.io/acme/jobs/123?utm_source=x">Apply</a> |
| ↳ | Backend Engineer | Remote | <a href="https://job-boards.greenhouse.io/acme/jobs/456">Apply</a> |
| **Globex** | New Grad SWE | Austin, TX | <a href="https://globex.wd5.myworkdayjobs.com/en-US/Careers/job/Austin/SWE_R1">Apply</a> |
| Initech | Engineer | SF | <a href="https://initech.example.com/careers/1">Apply</a> |
"""


class MarkdownTests(unittest.TestCase):
    def test_company_per_link(self):
        links = discover._markdown_links(MARKDOWN)
        self.assertIn(("Acme", "https://job-boards.greenhouse.io/acme/jobs/123?utm_source=x"), links)
        self.assertIn(("Acme", "https://job-boards.greenhouse.io/acme/jobs/456"), links)    # ↳ row
        self.assertIn(("Globex", "https://globex.wd5.myworkdayjobs.com/en-US/Careers/job/Austin/SWE_R1"), links)
        self.assertIn(("Initech", "https://initech.example.com/careers/1"), links)


class DiscoverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        radar.migrate(self.conn)
        discover.migrate(self.conn)

    def boards(self) -> dict:
        return {r[0]: r[1:] for r in self.conn.execute("SELECT board, name, origin, links, status FROM boards")}


class HarvestTests(DiscoverTestCase):
    FEEDS = [discover.Feed("list-a", "https://example.test/a.json", "simplify", "test"),
             discover.Feed("list-b", "https://example.test/b.md", "markdown", "test")]
    KNOWN = {"ashby:OldCo", "eightfold:apply.careers.microsoft.com|microsoft.com|filter_profession=software engineering"}

    def test_harvest(self):
        simplify = [
            {"company_name": "Acme Corp", "url": "https://job-boards.greenhouse.io/acme/jobs/1"},
            {"company_name": "Acme", "url": "https://job-boards.greenhouse.io/acme/jobs/2"},
            {"company_name": "Acme", "url": "https://boards.greenhouse.io/acme/jobs/3"},
            {"company_name": "Hooli", "url": "https://jobs.lever.co/Hooli/0e8d1c7a-1b8e-4a65-9a8d-1d6f0b0e2a11"},
            {"company_name": "Old Co", "url": "https://jobs.ashbyhq.com/oldco/0e8d1c7a-1b8e-4a65-9a8d-1d6f0b0e2a11"},
            {"company_name": "Amazon", "url": "https://www.amazon.jobs/en/jobs/1"},
            {"company_name": "Nowhere", "url": "https://nowhere.example.com/jobs/1"},
            {"company_name": "Microsoft", "url": "https://apply.careers.microsoft.com/careers/job/1970393556982911"},
        ]
        bodies = {"https://example.test/a.json": json.dumps(simplify).encode(),
                  "https://example.test/b.md": MARKDOWN.encode()}
        self.conn.execute("INSERT INTO jobs (key, company, title, location, remote, url, source, posted_exact, "
                          "sponsorship, sponsor_note, entry_hint, first_seen, last_seen, apply_url) VALUES "
                          "('k', 'Umbrella', 't', 'l', 0, 'https://jobs.smartrecruiters.com/Umbrella/1', 'simplify', 1, "
                          "'unknown', '', 0, 1, 1, 'https://jobs.smartrecruiters.com/Umbrella/1')")
        with mock.patch.object(radar, "fetch", lambda url, **kw: bodies[url]), \
                mock.patch.object(radar, "log"):
            added = discover.harvest(self.conn, self.KNOWN, now=1000.0, feeds=self.FEEDS)

        boards = self.boards()
        self.assertEqual(boards["greenhouse:acme"], ("Acme", "list-a", 5, "new"))   # most used name, links from both lists
        self.assertEqual(boards["lever:Hooli"][0], "Hooli")
        self.assertNotIn("ashby:oldco", {b.lower() for b in boards if b != "ashby:oldco"})
        self.assertFalse(any(b.lower() == "ashby:oldco" for b in boards))     # already read, any case
        self.assertFalse(any(b.startswith("amazon:") for b in boards))      # Amazon is one search, not boards
        self.assertFalse(any(b.startswith("eightfold:") for b in boards))   # read already, with a search filter
        self.assertEqual(boards["workday:globex.wd5/Careers"][:2], ("Globex", "list-b"))
        self.assertEqual(boards["smartrecruiters:Umbrella"][:2], ("Umbrella", "jobs"))
        self.assertEqual(added, {"list-a": 2, "list-b": 1, "jobs": 1})

        # A second harvest adds nothing it already has.
        with mock.patch.object(radar, "fetch", lambda url, **kw: bodies[url]), mock.patch.object(radar, "log"):
            again = discover.harvest(self.conn, self.KNOWN, now=2000.0, feeds=self.FEEDS)
        self.assertEqual(sum(again.values()), 0)
        self.assertFalse(discover.harvest_due(self.conn, 2000.0 + 3600))
        self.assertTrue(discover.harvest_due(self.conn, 2000.0 + discover.HARVEST_EVERY))

    def test_unreadable_feed_does_not_stop_the_others(self):
        def fetch(url, **kw):
            if url.endswith(".json"):
                raise radar.Throttled(url)
            return MARKDOWN.encode()
        with mock.patch.object(radar, "fetch", fetch), mock.patch.object(radar, "log"):
            added = discover.harvest(self.conn, set(), now=1.0, feeds=self.FEEDS)
        self.assertEqual(added["list-b"], 2)
        self.assertNotIn("list-a", added)


class CheckAndTrackTests(DiscoverTestCase):
    def add(self, board: str, status: str = "new", **cols) -> None:
        source, token = board.split(":", 1)
        row = {**dict(board=board, source=source, token=token, name=token.title(), origin="t", links=1, found=0.0,
                      status=status), **cols}
        self.conn.execute(f"INSERT INTO boards ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          list(row.values()))

    def test_check_sets_status(self):
        for b in ("greenhouse:a", "greenhouse:b", "lever:c", "ashby:d"):
            self.add(b)
        self.add("greenhouse:old-empty", "empty", checked=0.0)
        self.add("greenhouse:fresh-empty", "empty", checked=10**10)

        def gh(token):
            if token == "b":
                raise radar.BoardMissing(token)
            return {"a": 5, "old-empty": 2}.get(token, 0), ""

        self.add("icims:careers-x.icims.com")
        checks = {"greenhouse": gh, "lever": lambda t: (0, ""),
                  "icims": lambda t: (_ for _ in ()).throw(radar.Disallowed(t)),
                  "ashby": lambda t: (_ for _ in ()).throw(radar.Throttled(t))}
        with mock.patch.dict(discover.ats.CHECKS, checks):
            tally = discover.check_new(self.conn, now=10**9)
        status = {b: s for b, (_, _, _, s) in self.boards().items()}
        self.assertEqual(status, {"greenhouse:a": "ok", "greenhouse:b": "missing", "lever:c": "empty",
                                  "ashby:d": "error", "greenhouse:old-empty": "ok",
                                  "greenhouse:fresh-empty": "empty", "icims:careers-x.icims.com": "disallowed"})
        self.assertEqual(tally, Counter(ok=2, missing=1, empty=1, error=1, disallowed=1))

    def test_tracked_schedule(self):
        now = 1_000_000.0
        self.add("greenhouse:never-read", "ok")
        self.add("greenhouse:quiet-recent", "ok", fetched=now - 1800)
        self.add("greenhouse:quiet-due", "ok", fetched=now - discover.QUIET_EVERY)
        self.add("greenhouse:active", "ok", fetched=now - 1800, kept_at=now - 86400)
        self.add("lever:gone", "missing")
        boards, names = discover.tracked(self.conn, now)
        self.assertEqual(sorted(boards["greenhouse"]), ["active", "never-read", "quiet-due"])
        self.assertNotIn("lever", boards)
        with mock.patch.object(discover, "NEW_PER_RUN", 1):
            self.add("ashby:x", "ok", links=9)
            boards, _ = discover.tracked(self.conn, now)
        self.assertEqual(boards["ashby"], ["x"])                  # the most-linked new board first
        self.assertNotIn("never-read", boards["greenhouse"])
        self.assertNotIn("lever", boards)
        self.assertEqual(names["active"], "Active")

    def test_record_run(self):
        self.add("greenhouse:a", "ok")
        self.add("greenhouse:b", "ok")
        discover.record_run(self.conn, ["greenhouse:a", "greenhouse:b"], Counter({"greenhouse:a": 2}),
                            ["greenhouse:b"], 500.0)
        rows = {r[0]: r[1:] for r in self.conn.execute("SELECT board, status, fetched, kept_at FROM boards")}
        self.assertEqual(rows["greenhouse:a"], ("ok", 500.0, 500.0))
        self.assertEqual(rows["greenhouse:b"], ("missing", 500.0, None))


class LoadCompaniesTests(unittest.TestCase):
    def test_discovered_boards_merge_and_respect_removals(self):
        settings = radar.config.defaults()
        settings["companies"]["removed"] = ["greenhouse:hidden"]
        companies = radar.load_companies(settings, {"greenhouse": ["newco", "hidden", "airtable"]},
                                         {"newco": "New Co", "airtable": "Not Airtable"})
        self.assertIn("newco", companies["greenhouse"])
        self.assertNotIn("hidden", companies["greenhouse"])
        self.assertEqual(companies["greenhouse"].count("airtable"), 1)
        self.assertEqual(companies["display_names"]["newco"], "New Co")
        self.assertEqual(companies["display_names"]["airtable"], "Airtable")   # curated names win


if __name__ == "__main__":
    unittest.main()
