import unittest

from filter_policy import CAP, HARD, NONE, REVIEW, SOFT, Policy, strip_boilerplate

MR = {
    "negative_title_keywords": ["lead", "student", "IT", "director", "java", "expert"],
    "negative_description_keywords": ["IT", "ai", "java", "cloud"],
}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy.from_matching_rules(MR)

    def ev(self, title, description=""):
        return self.policy.evaluate({"title": title, "description": description})

    def test_hard_title_rejects(self):
        self.assertEqual(self.ev("Werkstudent Embedded Software (student)").effect, HARD)
        self.assertEqual(self.ev("Java Entwickler").effect, HARD)

    def test_ambiguous_title_words_only_signal(self):
        d = self.ev("Technical Lead Embedded Software")
        self.assertEqual(d.effect, REVIEW)
        self.assertIsNone(d.cap)
        self.assertEqual(self.ev("Embedded Expert").effect, REVIEW)

    def test_clear_title_penalty_caps_score(self):
        d = self.ev("Director Software")
        self.assertEqual((d.effect, d.cap), (CAP, 2))

    def test_pronoun_it_and_boilerplate_do_not_trigger(self):
        d = self.ev("Embedded Engineer", "We build ECUs and it is great. Our IT&I partner is here.")
        self.assertEqual(d.effect, REVIEW)  # "IT&I" uppercase is a weak review signal only
        d = self.ev("Embedded Engineer", "We build ECUs and it is great.")
        self.assertEqual(d.effect, NONE)
        text = "x" * 300 + " Similar jobs Cloud engineer IT manager Java"
        d = self.ev("Embedded Engineer", text)
        self.assertEqual(d.effect, NONE)

    def test_description_keywords_never_hard_reject(self):
        d = self.ev("Embedded Engineer", "Some Java and cloud work, plus AI.")
        self.assertEqual(d.effect, SOFT)
        self.assertTrue(d.signals)

    def test_profile_override_of_hard_list(self):
        p = Policy.from_matching_rules({**MR, "filter_policy": {"hard_title": ["student"]}})
        self.assertEqual(p.evaluate({"title": "Java Entwickler"}).effect, CAP)

    def test_strip_boilerplate_keeps_short_texts(self):
        self.assertEqual(strip_boilerplate("Similar jobs here"), "Similar jobs here")


if __name__ == "__main__":
    unittest.main()
