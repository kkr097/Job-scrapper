import unittest

from language_filter import (
    evidence_appears_in_text,
    language_requirement_excerpts,
    llm_language_rejection,
    mandatory_german_requirement,
    normalize_language_assessment,
)


class MandatoryGermanRequirementTests(unittest.TestCase):
    def test_clear_mandatory_requirements_are_rejected(self):
        for text in (
            "German language skills are mandatory for this role.",
            "You must speak German and English.",
            "Deutschkenntnisse mindestens B2.",
            "Gute Deutsch- und Englischkenntnisse.",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(mandatory_german_requirement(text, "A2"))

    def test_context_and_optional_mentions_are_not_rejected(self):
        for text in (
            "The position is based in Munich, Germany.",
            "English is required for our German engineering team.",
            "German language courses are offered.",
            "German preferred but not required.",
            "Deutschkenntnisse sind wünschenswert.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(mandatory_german_requirement(text, "A2"))

    def test_cefr_comparison_and_long_description_excerpts(self):
        self.assertIsNotNone(mandatory_german_requirement("German B1 required.", "A2"))
        self.assertIsNone(mandatory_german_requirement("German A2 required.", "A2", False))
        text = ("Python and machine learning work. " * 300) + "Deutschkenntnisse auf B2-Niveau sind erforderlich."
        self.assertTrue(any("Deutschkenntnisse" in item for item in language_requirement_excerpts(text)))

    def test_llm_normalization_and_grounding(self):
        normalized = normalize_language_assessment({
            "german_requirement": "required", "required_level": "b2", "evidence": " German B2 required. "
        })
        self.assertEqual(normalized["german_requirement"], "mandatory")
        self.assertIsNotNone(llm_language_rejection(normalized, "A2", True))
        source = "German B2 is required for customer meetings."
        self.assertTrue(evidence_appears_in_text(source, source))
        self.assertFalse(evidence_appears_in_text("Fluent German required", source))


if __name__ == "__main__":
    unittest.main()
