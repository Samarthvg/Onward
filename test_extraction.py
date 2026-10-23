"""Regression tests for requirement parsing and skill detection.

Run:  python -m unittest test_extraction
Every case here is a real heading or phrase from a stored posting that was
once misread.
"""

import unittest

import filters
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
        "The TOOLS you'll bring with you:": "req",
        # nice to have
        "Preferred Qualifications": "pref", "Nice to haves": "pref", "Bonus points if you have": "pref",
        "Ideally you have": "pref", "What We Value:": "pref", "Not required, but helpful": "pref",
        "Nice If You Have:": "pref",                     # says "you have", but isn't required
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
            "Early Careers: Software Development Engineer", "Entry Level Software Engineer (Spring 2027)",
            "Backend Engineer, Secrets Management", "Software Engineer, Identity and Access Management"]
    DROP = ["Software Solution Engineer", "Solutions Engineer", "Sr Software Engineer",
            "Software Engineer II", "Principal Data Engineering Architect", "Software Engineering Manager",
            "FVP, Platform & Automation Engineering", "AVP, Software Engineering", "Vice-President, Software Development"]

    def test_titles(self):
        for title in self.KEEP:
            with self.subTest(title=title):
                self.assertTrue(filters.is_swe_title(title) and filters.is_entry_title(title))
        for title in self.DROP:
            with self.subTest(title=title):
                self.assertFalse(filters.is_swe_title(title) and filters.is_entry_title(title))


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

    def kw(self, desc, title="Platform Engineer"):
        return {k: kind for k, w, kind in reqparse.keywords(desc, title, radar.find_skills, {}, radar.TERM_STOP)}

    def test_a_requirement_that_says_a_plus_is_nice_to_have(self):
        kw = self.kw("Requirements\n• Basic SQL or willingness to learn.\n• Experience with Kafka is a plus.\n"
                     "• Programming experience is helpful but not required, Python ideally.")
        self.assertEqual((kw.get("SQL"), kw.get("Kafka"), kw.get("Python")), ("req", "pref", "pref"))

    def test_tools_named_as_examples_count_however_rare(self):
        kw = self.kw("The TOOLS you'll bring with you:\n"
                     "• Familiarity with, or willingness to learn, JSON, APIs, data structures, and debugging.\n"
                     "• Experience or familiarity with software such as Braze, Qualtrics, Digioh, or Rudderstack a plus.\n"
                     "• Experience with policy-as-code tools such as OPA/Gatekeeper.\n"
                     "• A degree in a field such as Computer Science or Engineering.")
        self.assertEqual({k: kw.get(k) for k in ("JSON", "Braze", "Qualtrics", "Digioh", "Rudderstack")},
                         {"JSON": "req", "Braze": "pref", "Qualtrics": "pref", "Digioh": "pref", "Rudderstack": "pref"})
        self.assertEqual(kw.get("OPA/Gatekeeper"), "req", "a name with a slash stays whole")
        self.assertNotIn("Computer Science", kw, "a field of study, not a tool")
        self.assertNotIn("Engineering", kw)

    def test_example_lists_skip_known_skills_their_short_forms_and_plain_words(self):
        kw = self.kw("Requirements\n• A backend language such as Go and/or Python.\n"
                     "• Frameworks such as Node.js, Next.js, or Express.js; Node and Next preferred.\n"
                     "• Reporting tools such as Dashboards and Reports; you'll build dashboards.\n"
                     "• Cloud suites such as Office 365 or SOC 2 tooling.")
        self.assertEqual(kw.get("Go"), "req")
        for junk in ("Node", "Next", "Express", "Dashboards"):
            self.assertNotIn(junk, kw, junk)
        self.assertIn("Office 365", kw, "the version is part of the name")
        about = self.kw("About us\nWe build tools for work such as Notion, Gmail and Slack.\n"
                        "Responsibilities\n• Ship products like Starlink.")
        self.assertFalse({"Notion", "Gmail", "Starlink"} & set(about), "only requirement lines name tools to have")

    def test_degree(self):
        d = reqparse.degree("Requirements\n• Bachelor's degree in CS or equivalent experience")
        self.assertEqual(d, {"level": "bachelor", "or_equivalent": True})


class SponsorshipTests(unittest.TestCase):
    """Postings closed to visa holders: no sponsorship, citizens only, or a
    security clearance (which needs U.S. citizenship). Phrasings from real
    postings that were missed before."""
    NO = [
        "Clearance:\nApplicants selected will be subject to a security investigation; Top Secret clearance is required.",
        "You Have:\n• Experience with Agile\n• Top Secret clearance\n• Bachelor's degree",
        "Qualifications\n• Ability to obtain and maintain a U.S. Department of Energy Q-level security clearance, "
        "which requires U.S. citizenship.",
        "This position requires a Department of Energy (DOE) Q-level clearance.",
        "• Ability to obtain a Secret clearance",
        "Requirements\n• Active TS/SCI with Polygraph Clearance",
        "Must possess an active Secret clearance.",
        "Eligibility to obtain and maintain a U.S. Government security clearance is required.",
        "We will not sponsor visas for this role.",
        # The clearance is required; a heading follows on the same line.
        "REQUIRED QUALIFICATIONS • Eligible to obtain and maintain an active U.S. Secret security clearance "
        "PREFERRED QUALIFICATIONS • MS in Robotics",
        "Nice If You Have:\n• Jira\nClearance:\nApplicants may need access to classified information; "
        "Secret clearance is required.",
        # Sponsorship and citizenship statements count wherever they stand.
        "Nice to have\n• Kafka experience. Please note that visa sponsorship is not available for this position.",
        "Preferred\n• Relational database design\nEligibility • U.S. Citizenship Required",
        "Preferred skills\n• Experience in classified environments Clearance: Active Secret clearance required; "
        "Top Secret preferred",
    ]
    NOT_NO = [
        "Nice If You Have:\n• TS/SCI clearance",
        "Preferred\n• Security clearance a plus",
        "Must have active clearance or ability to obtain Public Trust",
        "• Ability to obtain a S//SAR level security clearance desired.",
        "Preferred Qualifications\n• Active Secret clearance\n• Experience with Kafka",
        "We help customers with price clearance events.",
        "Visa sponsorship is available.",
    ]

    def test_closed_to_visa_holders(self):
        for text in self.NO:
            with self.subTest(text=text[:60]):
                self.assertEqual(radar.sponsorship(text)[0], "no")

    def test_not_closed(self):
        for text in self.NOT_NO:
            with self.subTest(text=text[:60]):
                self.assertNotEqual(radar.sponsorship(text)[0], "no")


class YearsTests(unittest.TestCase):
    def test_years_in_sentences_and_forms(self):
        self.assertEqual(radar.min_years("3+ years of experience with Java"), 3)
        # Home Depot's form layout: the number on the next line, as a bullet.
        form = "Minimum Years of Work Experience:\n• 1\nPreferred Years of Work Experience:\n• No additional years of experience"
        self.assertEqual(radar.min_years(form), 1)
        self.assertIsNone(radar.min_years("Minimum Years of Work Experience:\n• None"))

    def test_a_requirement_that_starts_with_its_years(self):
        """Without the word "experience": Axos's "●10+ years in software engineering"."""
        self.assertEqual(radar.min_years("Requirements:\n●Bachelor's degree or higher in related field\n"
                                         "●10+ years in software engineering with meaningful leadership"), 10)
        self.assertEqual(radar.min_years("What you'll need\n• 3-5 years building production data systems"), 3)
        self.assertEqual(radar.min_years("Qualifications\n- A minimum of 4 years of writing high performance C++"), 4)
        # Not a requirement: a nice-to-have, or the company's own history.
        self.assertIsNone(radar.min_years("Nice to have\n• 5+ years in fintech"))
        self.assertIsNone(radar.min_years("Requirements\n• 2+ years in a startup is a plus"))
        self.assertIsNone(radar.min_years("About us\nFor 30 years we have built software for banks."))

    def test_the_largest_required_figure_counts(self):
        two = ("Basic Qualifications\n- 3+ years of non-internship professional software development experience\n"
               "- 2+ years of non-internship design or architecture of new and existing systems experience")
        self.assertEqual(radar.min_years(two), 3)
        # Years instead of a degree aren't asked of someone who has it.
        alt = ("Requirements\n• 1+ years of experience with Python\n"
               "• Bachelor's degree, or HS diploma or GED and 7+ years of experience supporting IT projects")
        self.assertEqual(radar.min_years(alt), 1)
        self.assertEqual(radar.min_years("Requirements\n• Bachelor's degree and 5+ years of experience in software"), 5)


if __name__ == "__main__":
    unittest.main()
