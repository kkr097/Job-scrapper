import json
import unittest

from rater import JobRater

PROFILE = {"matching_rules": {"negative_title_keywords": ["director", "lead", "IT"]},
           "scoring_protocol": {"title_filter_mandatory": "cap 2"}}
LONG_DESC = "Entwicklung von Embedded Software in C++ fuer Steuergeraete nach ASPICE mit MATLAB Simulink und AUTOSAR. " * 2


def make(score=8, min_score=6):
    rater = JobRater({"deepseek_api_key": "sk-test", "min_score": min_score}, PROFILE)
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return json.dumps({"ratings": [{"job_id": "1", "score": score, "analysis": {"missing_skills": []},
                                        "verdict": "ok"}]})
    rater._call_deepseek = fake
    return rater, prompts


class RaterScoringTests(unittest.TestCase):
    def rate(self, title, description=LONG_DESC, **kw):
        rater, prompts = make(**kw)
        job = rater._rate_batch([{"title": title, "description": description}])[0]
        return job["score"], prompts[0]

    def test_plain_title_keeps_model_score_and_hides_keyword_lists(self):
        score, prompt = self.rate("Embedded Software Engineer")
        self.assertEqual(score, 8)
        self.assertIn("checked by code, authoritative): none", prompt)
        self.assertNotIn('"director"', prompt)

    def test_title_cap_decided_in_code(self):
        score, prompt = self.rate("Director Software Engineering")
        self.assertEqual(score, 2)
        self.assertIn("HIT 'title:director'", prompt)

    def test_ambiguous_word_is_signal_not_cap(self):
        score, prompt = self.rate("Lead Software Engineer")
        self.assertEqual(score, 8)
        self.assertIn("ambiguous title word: lead", prompt)

    def test_pronoun_it_in_description_is_no_signal(self):
        score, prompt = self.rate("Embedded Engineer", "We think it is great. " + LONG_DESC)
        self.assertEqual(score, 8)

    def test_missing_description_stays_below_match_threshold(self):
        self.assertEqual(self.rate("Embedded Engineer", "", min_score=6)[0], 5)
        self.assertEqual(self.rate("Embedded Engineer", "", min_score=5)[0], 4)

    def test_prompt_contains_evidence_rules(self):
        _, prompt = self.rate("Embedded Engineer")
        self.assertIn("pre-sales", prompt)
        self.assertIn("A score of 7 or more needs", prompt)


if __name__ == "__main__":
    unittest.main()
