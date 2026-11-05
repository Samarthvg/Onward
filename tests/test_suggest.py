"""Title keyword suggestions for the role editor (filters.keep_suggestions,
filters.drop_suggestions) and the finance role template.

Run:  python -m unittest tests.test_suggest
"""

import unittest

import filters

ENTRY = ["entry"]


class KeepTests(unittest.TestCase):
    LINES = ["- clerk/sales", "credit/risk/fp&a + analyst/associate"]

    def suggest(self, misses, lines=None):
        return filters.keep_suggestions(lines or self.LINES, misses, ENTRY)

    def test_the_word_a_line_misses_is_suggested_for_its_group(self):
        got = self.suggest([("Equity Analyst", 3), ("Equity Analyst - New York", 2), ("Pricing Analyst", 1)])
        self.assertEqual([(s["word"], s["line"], s["group"], s["count"]) for s in got], [("equity", 2, 1, 5)],
                         "once only isn't enough to suggest")
        self.assertEqual(got[0]["examples"][0], "Equity Analyst")

    def test_a_leave_out_line_above_still_wins(self):
        self.assertEqual(self.suggest([("Credit Clerk", 4), ("Risk Clerk", 3)]), [], "clerk is left out by line 1")

    def test_titles_of_other_levels_dont_count(self):
        self.assertEqual(self.suggest([("Senior Equity Analyst", 9), ("Equity Analyst Intern", 9)]), [])

    def test_only_the_role_part_of_a_title(self):
        got = self.suggest([("Treasury Analyst - Chicago, IL", 3), ("Treasury Analyst (Hybrid)", 2)])
        self.assertEqual([s["word"] for s in got], ["treasury"], "not chicago, il or hybrid")

    def test_titles_the_rules_keep_dont_count(self):
        self.assertEqual(self.suggest([("Credit Analyst", 10)]), [])

    def test_the_word_goes_into_the_line_as_text(self):
        self.assertEqual(filters._with_word(self.LINES, 1, 0, "equity"), ["- clerk/sales", "credit/risk/fp&a/equity + analyst/associate"])
        self.assertEqual(filters._with_word(self.LINES, 0, 0, "teller"), ["- clerk/sales/teller", self.LINES[1]])


class DropTests(unittest.TestCase):
    LINES = ["- clerk", "risk/credit + analyst"]

    def test_a_word_in_titles_passed_on_and_none_gone_for(self):
        got = filters.drop_suggestions(self.LINES, ["Insurance Risk Analyst"] * 3 + ["Credit Analyst"],
                                       ["Risk Analyst", "Credit Analyst"])
        self.assertEqual([(s["word"], s["count"], s["lost"], s["line"]) for s in got], [("insurance", 3, 0, 1)],
                         "risk and analyst would cost jobs gone for")

    def test_without_a_leave_out_line_it_starts_one(self):
        got = filters.drop_suggestions(["risk + analyst"], ["Insurance Risk Analyst"] * 2, [])
        self.assertIsNone(got[0]["line"])

    def test_nothing_passed_on_nothing_suggested(self):
        self.assertEqual(filters.drop_suggestions(self.LINES, [], ["Risk Analyst"]), [])


class FinanceTemplateTests(unittest.TestCase):
    def setUp(self):
        self.rules = filters.TitleRules(filters.ROLE_TEMPLATES["finance"][1])

    def test_finance_titles(self):
        for t in ("Financial Analyst", "FP&A Analyst", "Analyst, Investment Banking", "Equity Research Associate",
                  "Staff Accountant", "Audit Associate", "Quantitative Researcher", "Financial Leadership Development Program",
                  "Actuarial Analyst", "M&A Analyst", "Underwriter"):
            self.assertTrue(self.rules.matches(t), t)

    def test_not_finance(self):
        for t in ("Software Engineer", "Sales Associate", "Loan Officer", "Bank Teller", "Quantitative Developer",
                  "Marketing Analyst", "Data Entry Clerk", "Customer Service Representative - Banking"):
            self.assertFalse(self.rules.matches(t), t)

    def test_banks_summer_analysts_are_internships(self):
        for t in ("2028 Investment Banking Summer Financial Analyst (Class of 2029)", "Summer Associate",
                  "Summer 2027 Analyst Program"):
            self.assertEqual(filters.title_levels(t), {"intern"}, t)
        for t in ("Financial Analyst", "Analyst - Summer Hours", "Software Engineer, Summer Sale Team"):
            self.assertNotIn("intern", filters.title_levels(t), t)

    def test_no_warnings(self):
        self.assertEqual(filters.keyword_warnings(filters.ROLE_TEMPLATES["finance"][1]), [])


if __name__ == "__main__":
    unittest.main()
