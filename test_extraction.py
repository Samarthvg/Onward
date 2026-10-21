"""Regression tests for requirement parsing and skill detection.

Run:  python -m unittest test_extraction
Every case here is a real heading or phrase from a stored posting that was
once misread.
"""

import unittest

import radar
import reqparse


class HeadingTests(unittest.TestCase):
    CASES = {
        # requirements
        "Requirements:": "req", "Qualifications": "req", "Minimum Qualifications": "req",
        "Basic Qualifications": "req", "Who You Are": "req", "About you": "req",
        "What We're Looking For": "req", "What We Look For": "req", "You'll need": "req",
        "What We Require:": "req", "The Tech": "req",
        "What You'll Bring to The Team": "req",          # mentions "the team" but is requirements
        # nice to have
        "Preferred Qualifications": "pref", "Nice to haves": "pref", "Bonus points if you have": "pref",
        "Ideally you have": "pref", "What We Value:": "pref", "Not required, but helpful": "pref",
        # responsibilities
        "Responsibilities": "resp", "Core Responsibilities:": "resp", "What you will do": "resp",
        "About the role": "resp", "In this role you will": "resp",
        # skipped
        "Benefits & Perks": "skip", "Compensation": "skip", "What we'll offer": "skip",
        "Why you will love working here": "skip", "About Nextdoor": "skip", "About the team": "skip",
        "Life at Stripe": "skip", "Our Values": "skip", "Our Principles:": "skip",
    }

    def test_headings(self):
        for heading, want in self.CASES.items():
            with self.subTest(heading=heading):
                self.assertEqual(reqparse._classify(heading), want)


class SkillTests(unittest.TestCase):
    POSITIVE = {
        "Python, Node, and Go": {"Python", "Node.js", "Go"},
        "Java or Go.": {"Java", "Go"},
        "MEAN stack apps": {"Angular", "MongoDB", "Node.js"},
        "MERN stack project": {"React", "MongoDB", "Node.js"},
        "Knowledge of CI/CD, Jenkins, GitLab, Docker, and Kubernetes": {"Jenkins", "GitLab", "Docker", "Kubernetes"},
        "Proficiency with Oracle, SQL Server, and MySQL databases": {"Oracle DB", "SQL Server", "MySQL"},
        "Java, Spring Boot, React": {"Java", "Spring Boot", "React"},
        "HTML5, CSS3, React, and Microfrontend architecture": {"HTML/CSS", "React", "Microfrontends"},
        "Experience with build systems (Make, Bazel/Pants/Buck, Gradle)": {"Bazel", "Gradle", "Build Systems"},
        "secrets in Vault, KMS": {"HashiCorp Vault"},
        "Datadog, Sentry, and PagerDuty": {"Datadog", "Sentry", "PagerDuty"},
        "agentic coding tools (e.g. Claude Code, Codex, Cursor)": {"AI coding tools"},
    }
    NEGATIVE = {
        "what we mean by ownership": {"Node.js", "MongoDB", "Angular"},
        "multi-node inference across GPUs": {"Node.js"},
        "Go to market strategy": {"Go"},
        "you will go above and beyond": {"Go"},
        "Experience with Veeva Vault and CRM": {"HashiCorp Vault"},
        "next generation Sentry Tower systems": {"Sentry"},
        "role-based authentication": {"Security"},
        "Let us know the rest of your story": {"REST", "REST APIs"},
    }

    def test_found(self):
        for text, want in self.POSITIVE.items():
            with self.subTest(text=text):
                self.assertTrue(want <= radar.find_skills(text), radar.find_skills(text))

    def test_not_found(self):
        for text, banned in self.NEGATIVE.items():
            with self.subTest(text=text):
                self.assertFalse(banned & radar.find_skills(text), radar.find_skills(text))


class TitleTests(unittest.TestCase):
    KEEP = ["Software Engineer", "Software Development Engineer I – AI/ML Network Infrastructure",
            "Early Careers: Software Development Engineer", "Entry Level Software Engineer (Spring 2027)"]
    DROP = ["Software Solution Engineer", "Solutions Engineer", "Sr Software Engineer",
            "Software Engineer II", "Principal Data Engineering Architect"]

    def test_titles(self):
        for title in self.KEEP:
            with self.subTest(title=title):
                self.assertTrue(radar.is_swe_title(title) and radar.is_entry_title(title))
        for title in self.DROP:
            with self.subTest(title=title):
                self.assertFalse(radar.is_swe_title(title) and radar.is_entry_title(title))


class KeywordTests(unittest.TestCase):
    def test_sections_weight_keywords(self):
        desc = ("About Acme\nWe build rockets with Fortran.\n"
                "What You'll Bring to The Team\n• Strong experience with Java and Spring Boot\n"
                "Nice to have\n• Kubernetes\n"
                "Benefits\n• Free Snowflake credits")
        kw = {k: kind for k, w, kind in reqparse.keywords(desc, "Software Engineer", radar.find_skills, {})}
        self.assertEqual(kw.get("Java"), "req")
        self.assertEqual(kw.get("Spring Boot"), "req")
        self.assertEqual(kw.get("Kubernetes"), "pref")
        self.assertNotIn("Snowflake", kw)      # benefits section
        self.assertNotIn("Fortran", kw)        # company intro

    def test_degree(self):
        d = reqparse.degree("Requirements\n• Bachelor's degree in CS or equivalent experience")
        self.assertEqual(d, {"level": "bachelor", "or_equivalent": True})


if __name__ == "__main__":
    unittest.main()
