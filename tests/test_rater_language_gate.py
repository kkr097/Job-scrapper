import json
import sys
import types
import unittest

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = types.SimpleNamespace(get=None, post=None)

from rater import JobRater


class RaterLanguageGateTests(unittest.TestCase):
    def setUp(self):
        self.rater = JobRater({
            "rating_preferred": "lmstudio", "lmstudio_enabled": True,
            "llm_language_gate_enabled": True, "reject_any_mandatory_german": True,
            "candidate_german_level": "A2",
        }, {})

    def _rate(self, description, assessment):
        response = {"ratings": [{"job_id": "1", "score": 8,
            "analysis": {"missing_skills": []}, "language_assessment": assessment,
            "verdict": "Good technical fit."}]}
        self.rater._call_lmstudio = lambda _prompt: json.dumps(response)
        return self.rater._rate_batch([{"description": description}])[0]

    def test_rejects_only_grounded_mandatory_requirement(self):
        job = self._rate("German B2 is required for this role.", {
            "german_requirement": "mandatory", "required_level": "B2",
            "evidence": "German B2 is required for this role.",
        })
        self.assertFalse(job["language_eligible"])
        ungrounded = self._rate("The role is based in Germany. English is required.", {
            "german_requirement": "mandatory", "required_level": "B2",
            "evidence": "German B2 is required.",
        })
        self.assertIsNone(ungrounded["language_eligible"])
        self.assertEqual(ungrounded["german_requirement"], "unclear")


if __name__ == "__main__":
    unittest.main()
