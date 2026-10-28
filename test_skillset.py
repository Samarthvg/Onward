"""The fast skill finder answers exactly as running every pattern would.

Run: python -m unittest test_skillset
"""

import unittest

import skillset

PHRASES = [
    "Restful services and REST, APIs", "C/C++ and Go, golang", "MySQL, MariaDB or the MERN stack",
    "R programming and RStudio", "Vault, and Veeva Vault", ".NET and C#", "AI agents, agentic, multi-agent systems",
    "Kubernetes (k8s), Helm and Docker", "single sign-on with SAML and OIDC", "scikit-learn / sklearn",
    "• 3+ years with Node.js, Express.js and TypeScript.", "Experience with GCP, Google Cloud Platform or GKE",
    "We value curiosity.", "",
]


def plain(text: str) -> set[str]:
    return {name for name, rx in skillset.SKILLS if rx.search(text)}


class FinderTests(unittest.TestCase):
    def test_same_answers_as_every_pattern(self):
        for text in PHRASES + [s["n"] for s in skillset.TAXONOMY] + [s["n"].lower() for s in skillset.TAXONOMY]:
            with self.subTest(text=text):
                self.assertEqual(skillset.find(text), plain(text))

    def test_most_patterns_are_prefiltered(self):
        """Only patterns that start with a single letter (C, R) or a
        lookahead-only word run on every line."""
        always = [s["n"] for s in skillset.TAXONOMY if skillset.triggers(s["re"]) is None]
        self.assertLessEqual(len(always), 6, always)

    def test_answers_are_copies(self):
        a = skillset.find("Python and Go")
        a.add("Nothing")
        self.assertNotIn("Nothing", skillset.find("Python and Go"))


if __name__ == "__main__":
    unittest.main()
