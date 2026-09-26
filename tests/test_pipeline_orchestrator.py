import unittest
from threading import Event

from pipeline_orchestrator import orchestrate


class PipelinePriorityTests(unittest.TestCase):
    def test_scoring_is_serial_and_kk_always_first(self):
        calls = []
        result = orchestrate(lambda profile, phase: calls.append((profile, phase)) or 0, lambda: False)
        self.assertEqual(result["kk"], "success")
        self.assertLess(calls.index(("kk", "score")), calls.index(("sandra", "score")))

    def test_each_profile_publishes_immediately_after_its_successful_score(self):
        calls = []
        result = orchestrate(lambda profile, phase: calls.append((profile, phase)) or 0, lambda: False)

        self.assertEqual(result["kk"], "success")
        self.assertEqual(result["sandra"], "success")
        self.assertLess(calls.index(("kk", "score")), calls.index(("kk", "publish")))
        self.assertLess(calls.index(("kk", "publish")), calls.index(("sandra", "score")))
        self.assertLess(calls.index(("sandra", "score")), calls.index(("sandra", "publish")))

    def test_publication_failure_does_not_reclassify_successful_scoring(self):
        def run(profile, phase):
            return 75 if profile == "kk" and phase == "publish" else 0

        result = orchestrate(run, lambda: False)

        self.assertEqual(result["kk"], "success")
        self.assertEqual(result["kk_publication"], "pending")
        self.assertEqual(result["sandra"], "success")

    def test_escalated_publication_failure_is_reported_separately(self):
        def run(profile, phase):
            return 1 if profile == "kk" and phase == "publish" else 0

        result = orchestrate(run, lambda: False)

        self.assertEqual(result["kk"], "success")
        self.assertEqual(result["kk_publication"], "escalated")

    def test_kk_warning_prevents_sandra_scoring(self):
        calls = []
        result = orchestrate(lambda profile, phase: calls.append((profile, phase)) or 0, lambda: True)
        self.assertEqual(result["kk"], "blocked")
        self.assertNotIn(("sandra", "score"), calls)

    def test_sandra_failure_does_not_fail_successful_kk(self):
        def run(profile, phase):
            return 1 if profile == "sandra" and phase == "score" else 0
        result = orchestrate(run, lambda: False)
        self.assertEqual(result["kk"], "success")
        self.assertEqual(result["sandra"], "failed")


if __name__ == "__main__":
    unittest.main()
