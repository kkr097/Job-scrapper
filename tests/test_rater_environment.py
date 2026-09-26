import os
import sys
import types
import unittest
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = types.SimpleNamespace(get=None, post=None)

from rater import JobRater


class RaterEnvironmentTests(unittest.TestCase):
    def test_lmstudio_environment_base_overrides_config(self):
        config = {
            "lmstudio_enabled": True,
            "rating_preferred": "lmstudio",
            "lmstudio_api_base": "http://stale-host:1234/v1",
        }
        with patch.dict(os.environ, {"LMSTUDIO_API_BASE": "http://dynamic-host:1234/v1"}):
            rater = JobRater(config, {})
        self.assertEqual(rater.lmstudio_api_base, "http://dynamic-host:1234/v1")
        self.assertEqual(rater.lmstudio_native_api_base, "http://dynamic-host:1234/api/v1")


if __name__ == "__main__":
    unittest.main()
