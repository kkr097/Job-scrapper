import unittest

from scraper import JobScraper


class EnrichInstrumentationTests(unittest.TestCase):
    def setUp(self):
        self.scraper = JobScraper({})

    def test_counts_attempts_repeats_and_outcomes(self):
        def fills(job):
            job["description"] = "full text"

        def empty(job):
            return None

        a = {"url": "https://x.test/a", "description": "snippet"}
        self.scraper._instrument_enrich("linkedin", a, fills)
        self.scraper._instrument_enrich("linkedin", dict(a, description="snippet"), empty)  # repeat URL
        self.scraper._instrument_enrich("linkedin", {"url": "https://x.test/b"}, empty)
        s = self.scraper._stats
        self.assertEqual(s["linkedin_enrich_attempts"], 3)
        self.assertEqual(s["linkedin_enrich_repeat_fetches"], 1)
        self.assertEqual(s["linkedin_enrich_ok"], 1)
        self.assertEqual(s["linkedin_enrich_no_text"], 2)
        self.assertEqual(len(self.scraper._enrich_urls), 2)

    def test_non_http_url_is_not_counted(self):
        self.scraper._instrument_enrich("xing", {"url": ""}, lambda job: None)
        self.assertEqual(self.scraper._stats, {})


class EnrichCacheTests(unittest.TestCase):
    GOOD = "Aufgaben: Entwicklung von Embedded Software in C++ fuer Steuergeraete nach ASPICE. " * 3

    def _scraper(self, tmp):
        import os
        from source_records import RecordStore
        sc = JobScraper({})
        sc.attach_record_store(RecordStore(os.path.join(tmp, "r.db")))
        return sc

    def test_fetch_once_then_serve_from_store_to_next_run(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            sc = self._scraper(tmp)

            def impl(job):
                calls.append(job["url"])
                sc._last_http_status = 200
                job["description"] = self.GOOD

            url = "https://de.linkedin.com/jobs/view/embedded-4472727127?trk=1"
            job1 = {"url": url, "description": "snippet"}
            sc._enrich_with_cache("linkedin", job1, impl)
            sc._enrich_with_cache("linkedin", {"url": url.replace("de.", "www.")}, impl)  # same run, alias
            self.assertEqual(len(calls), 1)
            self.assertEqual(sc._stats["linkedin_enrich_run_cache_hits"], 1)

            # next run / other profile: new scraper, same store -> no fetch, text served
            sc2 = JobScraper({})
            sc2.attach_record_store(sc.record_store)
            job2 = {"url": "https://www.linkedin.com/jobs/view/4472727127", "description": "snippet"}
            sc2._enrich_with_cache("linkedin", job2, impl)
            self.assertEqual(len(calls), 1)
            self.assertEqual(job2["description"], self.GOOD)
            self.assertEqual(sc2._stats["linkedin_enrich_decision_fresh"], 1)
            sc.record_store.close()

    def test_throttled_fetch_is_recorded_and_not_retried_immediately(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sc = self._scraper(tmp)
            calls = []

            def impl(job):
                calls.append(1)
                sc._last_http_status = 429

            url = "https://www.linkedin.com/jobs/view/4472700001"
            sc._enrich_with_cache("linkedin", {"url": url, "description": "snippet"}, impl)
            self.assertEqual(sc._stats["linkedin_enrich_status_throttled"], 1)
            sc2 = JobScraper({})
            sc2.attach_record_store(sc.record_store)
            job = {"url": url, "description": "snippet"}
            sc2._enrich_with_cache("linkedin", job, impl)
            self.assertEqual(len(calls), 1)  # backing off, card snippet kept
            self.assertEqual(job["description"], "snippet")
            self.assertEqual(sc2._stats["linkedin_enrich_decision_backoff"], 1)
            sc.record_store.close()

    def test_without_store_behaviour_is_unchanged_except_run_repeats(self):
        sc = JobScraper({})
        calls = []

        def impl(job):
            calls.append(1)
            job["description"] = "x" * 100

        sc._enrich_with_cache("linkedin", {"url": "https://www.linkedin.com/jobs/view/4472700002"}, impl)
        sc._enrich_with_cache("linkedin", {"url": "https://www.linkedin.com/jobs/view/4472700002"}, impl)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
