import os
import tempfile
import unittest
from datetime import datetime, timedelta

from source_records import (EMPTY, ERROR, GONE, MALFORMED, OK, THROTTLED, RecordStore, assess_description,
                            canonical_url, classify_http)

T0 = datetime(2026, 10, 1, 12, 0, 0)
GOOD = "Aufgaben: Entwicklung von Embedded Software in C++ für Steuergeräte nach ASPICE. " * 3


class CanonicalTests(unittest.TestCase):
    def test_linkedin_variants_share_a_key(self):
        a = canonical_url("https://de.linkedin.com/jobs/view/embedded-engineer-at-acme-4472727127?trk=abc&refId=1")
        b = canonical_url("https://www.linkedin.com/jobs/view/4472727127/")
        self.assertEqual(a[2], b[2])
        self.assertEqual(a[:2], ("linkedin", "4472727127"))

    def test_xing_and_other(self):
        self.assertEqual(canonical_url("https://www.xing.com/jobs/fuerstenfeldbruck-entwicklungsingenieur-elektronik-157127788")[2],
                         "xing:157127788")
        self.assertEqual(canonical_url("https://Example.com/Jobs/1/?a=1")[2], "example.com/jobs/1")


class QualityTests(unittest.TestCase):
    def test_assess(self):
        self.assertEqual(assess_description(""), EMPTY)
        self.assertEqual(assess_description("kurz"), EMPTY)
        self.assertEqual(assess_description("WÃ¼lfrath KÃ¶ln BrÃ¼hl " + GOOD), MALFORMED)
        self.assertEqual(assess_description(GOOD), OK)
        self.assertEqual(assess_description("Wülfrath Köln Brühl äöüß " + GOOD), OK)

    def test_http(self):
        self.assertEqual((classify_http(429), classify_http(503), classify_http(404), classify_http(403), classify_http(None)),
                         (THROTTLED, THROTTLED, GONE, ERROR, ERROR))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RecordStore(os.path.join(self.tmp.name, "r.db"))
        self.url = "https://www.linkedin.com/jobs/view/4472727127"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_then_fresh_then_stale(self):
        self.assertEqual(self.store.decide(self.url, T0), (True, "new"))
        self.assertEqual(self.store.record_result(self.url, GOOD, 200, T0), OK)
        self.assertEqual(self.store.decide(self.url, T0 + timedelta(days=1)), (False, "fresh"))
        self.assertEqual(self.store.description(self.url), GOOD)
        self.assertEqual(self.store.decide(self.url, T0 + timedelta(days=15)), (True, "stale"))

    def test_other_profile_alias_hits_same_record(self):
        self.store.record_result("https://de.linkedin.com/jobs/view/some-slug-4472727127?trk=1", GOOD, 200, T0)
        self.assertEqual(self.store.decide(self.url, T0), (False, "fresh"))

    def test_retry_with_backoff_then_exhausted(self):
        self.assertEqual(self.store.record_result(self.url, "", 429, T0), THROTTLED)
        self.assertEqual(self.store.decide(self.url, T0 + timedelta(hours=1)), (False, "backoff"))
        self.assertEqual(self.store.decide(self.url, T0 + timedelta(hours=7)), (True, "retry"))
        self.store.record_result(self.url, "", 200, T0 + timedelta(hours=7))          # empty body -> retry 2
        self.store.record_result(self.url, "", 429, T0 + timedelta(hours=40))         # retry 3
        self.assertEqual(self.store.decide(self.url, T0 + timedelta(days=30)), (False, "retries_exhausted"))

    def test_success_resets_failures_and_gone_never_retries(self):
        self.store.record_result(self.url, "", 429, T0)
        self.store.record_result(self.url, GOOD, 200, T0 + timedelta(hours=7))
        self.assertEqual(self.store.decide(self.url, T0 + timedelta(hours=8)), (False, "fresh"))
        other = "https://www.linkedin.com/jobs/view/4999999999"
        self.assertEqual(self.store.record_result(other, "", 404, T0), GONE)
        self.assertEqual(self.store.decide(other, T0 + timedelta(days=60)), (False, "gone"))

    def test_failed_refresh_keeps_old_description_with_backoff(self):
        self.store.record_result(self.url, GOOD, 200, T0)
        later = T0 + timedelta(days=15)
        self.store.record_result(self.url, "", 429, later)
        self.assertEqual(self.store.description(self.url), GOOD)
        self.assertEqual(self.store.decide(self.url, later + timedelta(hours=1)), (False, "backoff"))
        self.assertEqual(self.store.decide(self.url, later + timedelta(hours=7)), (True, "stale"))

    def test_prune(self):
        self.store.observe(self.url, T0)
        self.assertEqual(self.store.prune(90, T0 + timedelta(days=100)), 1)


if __name__ == "__main__":
    unittest.main()


class StatusOverrideTests(unittest.TestCase):
    def test_low_quality_is_retryable_and_not_stored(self):
        with tempfile.TemporaryDirectory() as d:
            store = RecordStore(os.path.join(d, "r.db"), refresh_days=14, max_retries=3)
            url = "https://www.xing.com/jobs/x-1-157127788"
            self.assertEqual(store.record_result(url, "", 200, now=T0, status_override="low_quality"), "low_quality")
            self.assertEqual(store.description(url), "")
            self.assertEqual(store.decide(url, now=T0 + timedelta(hours=1)), (False, "backoff"))
            self.assertEqual(store.decide(url, now=T0 + timedelta(hours=7)), (True, "retry"))
            store.close()
