import json
import shutil
import unittest
import uuid
from pathlib import Path

from profile_workspace import ProfileWorkspace


class ProfileWorkspaceTests(unittest.TestCase):
    def workspace(self):
        root = Path(__file__).parent / ".tmp" / f"profiles-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_profiles_resolve_to_isolated_private_and_state_paths(self):
        root = self.workspace()
        for profile_id in ("kk", "sandra"):
            private = root / "profiles" / profile_id / "private"
            private.mkdir(parents=True)
            (private / "config.json").write_text(
                json.dumps({"profile_id": profile_id}), encoding="utf-8"
            )
            (private / "candidate_profile.json").write_text(
                json.dumps({"candidate_profile": {"name": profile_id}}), encoding="utf-8"
            )

        kk = ProfileWorkspace.load("kk", root)
        sandra = ProfileWorkspace.load("sandra", root)

        self.assertEqual(kk.profile_id, "kk")
        self.assertEqual(sandra.profile_id, "sandra")
        self.assertNotEqual(kk.state_dir, sandra.state_dir)
        self.assertEqual(kk.output_path("daily_jobs.csv"), kk.state_dir / "daily_jobs.csv")
        self.assertEqual(sandra.output_path("cover_letters.db"), sandra.state_dir / "cover_letters.db")

    def test_unknown_profiles_and_mismatched_private_config_fail_closed(self):
        root = self.workspace()
        private = root / "profiles" / "kk" / "private"
        private.mkdir(parents=True)
        (private / "config.json").write_text(
            json.dumps({"profile_id": "sandra"}), encoding="utf-8"
        )
        (private / "candidate_profile.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(ValueError):
            ProfileWorkspace.load("unknown", root)
        with self.assertRaises(ValueError):
            ProfileWorkspace.load("kk", root)


if __name__ == "__main__":
    unittest.main()
