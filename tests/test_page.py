"""The dashboard page is rebuilt when it was made from another template, so
an update never serves the old version's page until the next search.

Run: python -m unittest tests.test_page
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import radar


class PageCurrentTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="onward-page-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.template = self.dir / "dashboard_template.html"
        self.template.write_text("<!doctype html><title>t</title><script>const DATA = /*__DATA__*/null;</script>", encoding="utf-8")
        self.page = self.dir / "dashboard.html"
        for name, value in (("TEMPLATE_PATH", self.template), ("DASHBOARD_PATH", self.page)):
            p = mock.patch.object(radar, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_page_from_this_template_is_current(self):
        self.assertFalse(radar.page_current(self.page), "no page yet")
        radar.render([], {})
        self.assertTrue(radar.page_current(self.page))
        self.assertIn('"jobs":[]', self.page.read_text(encoding="utf-8"))

    def test_an_update_makes_the_old_page_stale(self):
        radar.render([], {})
        self.template.write_text(self.template.read_text(encoding="utf-8").replace("<title>t", "<title>new"), encoding="utf-8")
        self.assertFalse(radar.page_current(self.page))

    def test_a_page_from_before_the_mark_is_stale(self):
        self.page.write_text("<!doctype html><p>old green page</p>", encoding="utf-8")
        self.assertFalse(radar.page_current(self.page))


if __name__ == "__main__":
    unittest.main()
