"""The applicant's own entries that the extension picked for a job reach
the drafting and answering prompts, and nothing is added without them.

Run:  python -m unittest tests.test_entries_prompt
"""

import unittest
from pathlib import Path

import tailor

MASTER = (Path(__file__).resolve().parent / "fixtures" / "history_master.tex").read_text(encoding="utf-8")
JOB = {"title": "Frontend Engineer", "company": "Example Co"}
ENTRIES = "JOB: Full Stack Developer Intern, Example Labs\n- Rewrote the estimate screens in Vue.js."


class EntriesInPrompts(unittest.TestCase):
    def test_draft_prompt_carries_entries(self):
        _, material = tailor.draft_prompt(MASTER, JOB, "Why this role?", "", "Vue.js work", entries=ENTRIES)
        self.assertIn("THE APPLICANT'S OWN ENTRIES FOR THIS JOB", material)
        self.assertIn("Rewrote the estimate screens in Vue.js.", material)

    def test_answers_prompt_carries_entries(self):
        _, material = tailor.answers_prompt(MASTER, JOB, {}, [{"id": "1", "kind": "textarea", "question": "Tell us about a project"}],
                                            "Vue.js work", entries=ENTRIES)
        self.assertIn("Rewrote the estimate screens in Vue.js.", material)

    def test_no_entries_no_block(self):
        _, material = tailor.draft_prompt(MASTER, JOB, "Why this role?", "", "Vue.js work")
        self.assertNotIn("OWN ENTRIES", material)

    def test_manual_prompt_carries_entries(self):
        self.assertIn("Rewrote the estimate screens", tailor.build_draft_prompt(MASTER, JOB, "Why?", "", "jd", entries=ENTRIES))


if __name__ == "__main__":
    unittest.main()
