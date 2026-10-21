"""Checks that the drafted-answer voice check flags machine-written text and
leaves plain student-style text alone.

Run:  python -m unittest test_voice
"""

import unittest

import tailor

ROBOTIC = ("ElevenLabs' mission appeals to me because it turns AI research into tools people can directly "
           "create with. ElevenMusic makes that connection concrete: listening, creating, and remixing put model "
           "capabilities inside a consumer experience where interface and backend decisions both matter. In "
           "AI-thelete, I built a retrieval-augmented chatbot with persistent agent state. That work connects to "
           "the challenge of making AI capabilities useful through a complete application. I would bring "
           "full-stack experience from React and Node.js applications to building ElevenMusic features.")

HUMAN = ("Music is something I love very dearly, so ElevenMusic is the kind of product I'd actually use. "
         "I built a chatbot that remembers past conversations, and the hardest part was making it feel fast. "
         "I'd love to work on problems like that with people who care about music as much as I do.")


class VoiceTests(unittest.TestCase):
    def test_flags_stock_phrases_and_repeats(self):
        problems = " | ".join(tailor.style_problems(ROBOTIC, "ElevenLabs"))
        for phrase in ("appeals to me", "concrete", "connects to", "I would bring"):
            self.assertIn(phrase, problems)
        self.assertIn("capabilities", problems)
        self.assertIn("connection/connects", problems)

    def test_plain_voice_passes(self):
        self.assertEqual(tailor.style_problems(HUMAN, "ElevenLabs"), [])

    def test_punctuation(self):
        self.assertIn("uses an em dash", tailor.style_problems("I built it — fast.", ""))
        self.assertIn("uses a semicolon", tailor.style_problems("I built it; it worked.", ""))

    def test_prompts_carry_voice_and_about_me(self):
        instructions, material = tailor.draft_prompt(
            "\\begin{document}x\\end{document}", {"company": "Acme", "title": "SWE"}, "Why Acme?", "", "", 0)
        self.assertIn("new-grad student", instructions)
        self.assertNotIn("{length}", instructions)


if __name__ == "__main__":
    unittest.main()
