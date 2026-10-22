"""H-1B employer history from USCIS's H-1B Employer Data Hub: reading its
CSV export, keeping the newest two years with data, refreshing them, and
falling back to what's saved (or the old bulk files) when it can't be read.

Run: python -m unittest test_h1b
"""

import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import radar

HEAD = "Employer (Petitioner) Name,Fiscal Year   ,Industry (NAICS) Code,Measure Names,Petitioner City,Petitioner State,Petitioner Zip Code,Tax ID,Line by line,Measure Values\n"


def export(*rows):
    """A Data Hub export: (employer, measure, value) rows."""
    return HEAD + "".join(f'{e},2025,,{m},AUSTIN,TX,78701,1234,1,"{v}"\n' for e, m, v in rows)


EXAMPLE = export(("ACME SOFTWARE INC", "New Employment Approval", "1,204"), ("ACME SOFTWARE INC", "New Employment Denial", "30"),
                 ("ACME SOFTWARE INC", "Continuation Approval", "300"), ("ACME SOFTWARE INC", "Change of Employer Approval", "45"),
                 ("ACME SOFTWARE INC", "Amended Approval", "5"), ("TINY LABS LLC", "Continuation Approval", "2"),
                 ("NOBODY CORP", "New Employment Approval", "0"), ("", "New Employment Approval", "9"))


class TotalsTests(unittest.TestCase):
    def test_new_employment_is_initial_and_every_other_approval_continuing(self):
        self.assertEqual(radar.h1b_totals(io.StringIO(EXAMPLE)),
                         {"ACME SOFTWARE INC": [1204, 350], "TINY LABS LLC": [0, 2]})

    def test_an_empty_year_and_a_changed_export(self):
        self.assertEqual(radar.h1b_totals(io.StringIO("\n")), {})
        with self.assertRaisesRegex(RuntimeError, "changed its columns"):
            radar.h1b_totals(io.StringIO("Employer,Approvals\nACME,3\n"))

    def test_fiscal_year(self):
        self.assertEqual(radar.fiscal_year(datetime(2026, 9, 30, 12).timestamp()), 2026)
        self.assertEqual(radar.fiscal_year(datetime(2026, 10, 1, 12).timestamp()), 2027)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jr-h1b-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(radar, "H1B_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(radar, "log", lambda m: None)
        p.start()
        self.addCleanup(p.stop)
        self.now = datetime(2026, 10, 22, 12).timestamp()      # FY2027, with no data yet
        self.asked = []

    def hub(self, years):
        def open_year(year):
            self.asked.append(year)
            if year not in years:
                return io.StringIO("\n")
            return io.StringIO(export(("ACME SOFTWARE INC", "New Employment Approval", str(years[year]))))
        return open_year

    def test_keeps_the_newest_two_years_with_data(self):
        got = radar.update_h1b(self.now, self.hub({2026: 7, 2025: 9, 2024: 11}))
        self.assertEqual(got, [2026, 2025])
        self.assertEqual(self.asked, [2027, 2026, 2025], "FY2027 has nothing yet; 2024 isn't needed")
        self.assertEqual([y for y, _ in radar.h1b_files()], [2026, 2025])
        idx = radar.H1BIndex.load()
        self.assertEqual(idx.years, [2026, 2025])
        self.assertEqual(idx.lookup("Acme Software")["years"]["2025"], {"initial": 9, "continuing": 0})

    def test_refreshes_after_90_days_and_drops_older_years(self):
        radar.update_h1b(self.now, self.hub({2026: 7, 2025: 9}))
        self.asked.clear()
        radar.update_h1b(self.now + 86400, self.hub({2026: 7, 2025: 9}))
        self.assertEqual(self.asked, [2027], "saved this week: only the year that had no data is asked again")
        self.assertFalse(radar.h1b_due(self.now + 86400))
        later = self.now + radar.H1B_REFRESH + 86400
        for f in self.dir.glob("h1b-FY*.csv"):
            os.utime(f, (self.now, self.now))
        self.assertTrue(radar.h1b_due(later))
        # A year later FY2027 has data: it and FY2026 are kept, FY2025 goes.
        radar.update_h1b(later + 365 * 86400, self.hub({2027: 3, 2026: 8}))
        self.assertEqual(sorted(f.name for f in self.dir.glob("h1b-FY*.csv")), ["h1b-FY2026.csv", "h1b-FY2027.csv"])

    def test_a_download_cut_short_is_finished_the_next_day(self):
        radar.update_h1b(self.now, self.hub({2026: 7}))      # FY2025 never came
        for f in self.dir.glob("h1b-FY*.csv"):
            os.utime(f, (self.now, self.now))
        self.assertFalse(radar.h1b_due(self.now + 3600))
        self.assertTrue(radar.h1b_due(self.now + 2 * 86400))
        self.asked.clear()
        self.assertEqual(radar.update_h1b(self.now + 2 * 86400, self.hub({2026: 7, 2025: 9})), [2026, 2025])
        self.assertEqual(self.asked, [2027, 2025], "the saved year isn't fetched again")

    def test_force(self):
        radar.update_h1b(self.now, self.hub({2026: 7, 2025: 9}))
        self.asked.clear()
        radar.update_h1b(self.now, self.hub({2026: 8, 2025: 9}), force=True)
        self.assertEqual(self.asked, [2027, 2026, 2025])

    def test_dashboard_down_keeps_what_is_saved(self):
        radar.update_h1b(self.now, self.hub({2026: 7, 2025: 9}))
        for f in self.dir.glob("h1b-FY*.csv"):
            os.utime(f, (self.now, self.now))

        def down(year):
            raise OSError("connection reset")
        with mock.patch.object(radar, "update_h1b_legacy") as legacy:
            radar.update_h1b(self.now + radar.H1B_REFRESH * 2, down)
        legacy.assert_not_called()
        self.assertEqual([y for y, _ in radar.h1b_files()], [2026, 2025])

    def test_nothing_saved_and_dashboard_down_uses_the_old_bulk_files(self):
        def down(year):
            raise OSError("connection reset")
        with mock.patch.object(radar, "update_h1b_legacy") as legacy:
            radar.update_h1b(self.now, down)
        legacy.assert_called_once()

    def test_old_bulk_files_still_read(self):
        (self.dir / "h1b_datahubexport-2023.csv").write_text(
            '"Fiscal Year",Employer,"Initial Approval","Initial Denial","Continuing Approval","Continuing Denial",NAICS,"Tax ID",State,City,ZIP\n'
            "2023,ACME SOFTWARE INC,4,0,6,0,51,1234,TX,AUSTIN,78701\n", encoding="utf-8")
        idx = radar.H1BIndex.load()
        self.assertEqual(idx.years, [2023])
        self.assertEqual(idx.lookup("Acme Software")["initial"], 4)


if __name__ == "__main__":
    unittest.main()
