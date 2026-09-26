import os
import unittest
import uuid
from datetime import datetime
from nightly_cover_letters import BERLIN, PROFILE_ORDER, NightlyCoordinator, build_generation_manifest


TEST_TEMP_ROOT = os.path.join(os.path.dirname(__file__), ".tmp")
class NightlyCoordinatorTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(TEST_TEMP_ROOT, exist_ok=True)
        self.db = os.path.join(TEST_TEMP_ROOT, f"nightly-{uuid.uuid4().hex}.db")
        self.coordinator = NightlyCoordinator(self.db)

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = self.db + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_start_gate_distinguishes_89_90_and_unavailable_usage(self):
        now = datetime(2026, 9, 28, 23, 0, tzinfo=BERLIN)
        self.assertEqual(
            self.coordinator.start("low", 89, now=now)["reason"],
            "insufficient_allowance",
        )
        self.assertEqual(
            self.coordinator.start("missing", None, now=now)["reason"],
            "usage_unavailable",
        )
        self.assertTrue(self.coordinator.start("enough", 90, now=now)["started"])

    def test_shared_marker_and_lease_prevent_duplicate_runs_and_recover_when_stale(self):
        now = datetime(2026, 9, 28, 23, 0, tzinfo=BERLIN)
        self.assertTrue(self.coordinator.start("scheduled", 100, now=now, lease_minutes=30)["started"])
        self.assertEqual(
            self.coordinator.start("manual", 100, now=now)["reason"],
            "run_active",
        )
        recovered = self.coordinator.start(
            "recovery", 100, now=now.replace(hour=23, minute=31)
        )
        self.assertTrue(recovered["started"])

    def test_continuation_gate_stops_below_twenty_percent_and_after_cutoff(self):
        start = datetime(2026, 9, 28, 23, 0, tzinfo=BERLIN)
        self.assertTrue(self.coordinator.start("run", 90, now=start)["started"])
        self.assertTrue(self.coordinator.can_continue("run", 20, now=start)["continue"])
        self.assertEqual(
            self.coordinator.can_continue("run", 19, now=start)["reason"],
            "allowance_below_continuation_threshold",
        )
        self.assertEqual(
            self.coordinator.can_continue(
                "run", 100, now=datetime(2026, 9, 29, 6, 31, tzinfo=BERLIN)
            )["reason"],
            "scheduled_cutoff",
        )

    def test_forced_manual_start_bypasses_only_usage_gate(self):
        now = datetime(2026, 9, 28, 23, 0, tzinfo=BERLIN)
        self.assertTrue(self.coordinator.start("forced", 1, now=now, forced=True)["started"])
        self.assertEqual(
            self.coordinator.start("duplicate", 100, now=now, forced=True)["reason"],
            "run_active",
        )

    def test_completed_night_cannot_be_started_again(self):
        now = datetime(2026, 9, 28, 23, 0, tzinfo=BERLIN)
        self.assertTrue(self.coordinator.start("run", 100, now=now)["started"])
        self.assertTrue(self.coordinator.finish("run", "completed", now=now)["finished"])
        self.assertEqual(
            self.coordinator.start("recovery", 100, now=now)["reason"],
            "already_completed",
        )


class GenerationManifestTests(unittest.TestCase):
    def test_profile_execution_order_is_kk_then_sandra(self):
        self.assertEqual(PROFILE_ORDER, ("kk", "sandra"))

    def test_profile_context_appears_once_and_forty_jobs_form_four_chunks(self):
        jobs = [{"job_id": f"job-{index}", "description": f"Role {index}"} for index in range(40)]
        facts = {"candidate": "private facts loaded once"}

        manifest = build_generation_manifest("kk", jobs, facts)

        self.assertEqual(manifest["profile_id"], "kk")
        self.assertEqual(manifest["generation_context"]["candidate_facts"], facts)
        self.assertEqual([len(chunk) for chunk in manifest["chunks"]], [10, 10, 10, 10])
        self.assertNotIn("candidate_facts", manifest["chunks"][0][0])

    def test_manifest_rejects_mixed_profile_jobs_and_more_than_forty(self):
        with self.assertRaisesRegex(ValueError, "profile"):
            build_generation_manifest(
                "kk", [{"job_id": "x", "profile_id": "sandra"}], {"candidate": "KK"}
            )
        with self.assertRaisesRegex(ValueError, "40"):
            build_generation_manifest(
                "kk", [{"job_id": str(index)} for index in range(41)], {"candidate": "KK"}
            )


if __name__ == "__main__":
    unittest.main()
