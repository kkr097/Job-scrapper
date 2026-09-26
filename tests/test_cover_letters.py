import io
import json
import os
import unittest
import uuid
from datetime import timedelta
from urllib.parse import urlencode
from unittest.mock import Mock, patch

from cover_letters import CoverLetterStore, iso_utc, job_id_for_url, make_public_sample, utc_now
from cover_letter_web import CoverLetterWebApp, hash_password
from cloud_store import prepare_sync_record


TEST_TEMP_ROOT = os.path.join(os.path.dirname(__file__), ".tmp")
os.makedirs(TEST_TEMP_ROOT, exist_ok=True)


def valid_letter(contact=True):
    header = ["Bewerbung als Embedded Software Engineer", "Krishnakumar Radhakrishna Panicker"]
    if contact:
        header += ["rkrishnakumar097@gmail.com", "+49 176 88230106", "Bert-Brecht-Str. 6, 85055 Ingolstadt"]
    body = []
    for seed in ("Entwicklung", "Testautomatisierung", "Zusammenarbeit"):
        body.append(" ".join([seed] + ["Erfahrung"] * 94) + ".")
    return "\n".join(header + ["Sehr geehrte Damen und Herren,", "", body[0], "", body[1], "", body[2], "", "Mit freundlichen Grüßen", "Krishnakumar Radhakrishna Panicker"])


def valid_english_letter():
    header = ["Application for Embedded Software Engineer", "Krishnakumar Radhakrishna Panicker"]
    body = []
    for seed in ("Development", "Verification", "Collaboration"):
        body.append(" ".join([seed] + ["experience"] * 94) + ".")
    return "\n".join(header + ["Dear Hiring Manager,", "", body[0], "", body[1], "", body[2], "", "Kind regards", "Krishnakumar Radhakrishna Panicker"])


class CoverLetterStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(TEST_TEMP_ROOT, f"letters-{uuid.uuid4().hex}.db")
        self.store = CoverLetterStore(self.db)

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = self.db + suffix
            if os.path.exists(path):
                os.remove(path)

    def job(self, url="https://example.test/jobs/1", first_seen=None, description=None):
        return {
            "url": url,
            "title": "Embedded Software Engineer",
            "company": "Example GmbH",
            "source": "XING",
            "score": 8,
            "first_seen": first_seen or iso_utc(),
            "description": description or ("Develop embedded software and tests. " * 20),
        }

    def test_same_url_has_profile_scoped_identity_without_changing_legacy_kk_id(self):
        url = "https://example.test/jobs/42?tracking=abc"
        self.assertEqual(job_id_for_url(url), job_id_for_url(url, "kk"))
        self.assertNotEqual(job_id_for_url(url, "kk"), job_id_for_url(url, "sandra"))

    def test_sandra_public_sample_removes_private_family_motivation(self):
        full = valid_english_letter().replace(
            "Dear Hiring Manager,",
            "Dear Hiring Manager,\n\nMy husband lives in Germany, and I am learning German while seeking international experience.",
        )
        public = make_public_sample(full, profile_id="sandra")
        self.assertNotIn("husband", public.lower())
        self.assertIn("Development experience", public)

    def test_sandra_automatic_batch_excludes_legacy_and_low_score_jobs(self):
        store = CoverLetterStore(self.db, profile_id="sandra")
        legacy = self.job("https://example.test/jobs/legacy")
        legacy["auto_letter_eligible"] = False
        store.enqueue_job(legacy)
        low = self.job("https://example.test/jobs/low")
        low["score"] = 7
        store.enqueue_job(low)
        high = self.job("https://example.test/jobs/high")
        high["score"] = 9
        high_id = store.enqueue_job(high)
        self.assertEqual(
            [row["job_id"] for row in store.claim_batch(10, min_score=8, eligible_only=True)],
            [high_id],
        )

    def test_sandra_daily_cap_survives_repeated_claims(self):
        store = CoverLetterStore(self.db, profile_id="sandra")
        store.enqueue_job(self.job("https://example.test/jobs/cap-1"))
        store.enqueue_job(self.job("https://example.test/jobs/cap-2"))
        claimed = store.claim_batch(1, min_score=8, eligible_only=True, daily_limit=1)
        self.assertEqual(len(claimed), 1)
        store.commit_result(claimed[0]["job_id"], valid_english_letter(), "en")
        self.assertEqual(store.claim_batch(10, min_score=8, eligible_only=True, daily_limit=1), [])

    def test_inadequate_description_is_unavailable(self):
        job_id = self.store.enqueue_job(self.job(description="Too short"))
        self.assertEqual(self.store.get_admin_job(job_id)["status"], "unavailable")
        self.assertEqual(self.store.claim_batch(), [])

    def test_claim_commit_and_public_redaction(self):
        job_id = self.store.enqueue_job(self.job())
        batch = self.store.claim_batch()
        self.assertEqual([row["job_id"] for row in batch], [job_id])
        metrics = self.store.commit_result(job_id, valid_letter(), "de")
        self.assertGreaterEqual(metrics["words"], 300)
        public = self.store.get_public_job(job_id)
        self.assertNotIn("@", public["public_text"])
        self.assertNotIn("+49", public["public_text"])
        self.assertNotIn("Mit freundlichen", public["public_text"])
        self.assertNotIn("Krishnakumar Radhakrishna Panicker", public["public_text"])
        self.assertIn("Mit freundlichen", self.store.get_admin_job(job_id)["full_text"])

    def test_english_letter_is_validated_and_redacted(self):
        job_id = self.store.enqueue_job(self.job())
        self.store.claim_batch()
        self.store.commit_result(job_id, valid_english_letter(), "en")
        public = self.store.get_public_job(job_id)
        self.assertEqual(public["language"], "en")
        self.assertNotIn("Kind regards", public["public_text"])
        self.assertNotIn("Krishnakumar Radhakrishna Panicker", public["public_text"])

    def test_batch_isolation_and_partial_retry(self):
        first = self.store.enqueue_job(self.job("https://example.test/jobs/1"))
        second = self.store.enqueue_job(self.job("https://example.test/jobs/2"))
        claimed = self.store.claim_batch(10)
        self.assertEqual({row["job_id"] for row in claimed}, {first, second})
        self.store.commit_result(first, valid_letter(), "de")
        self.store.release_with_error(second, "temporary failure")
        retry = self.store.claim_batch(10)
        self.assertEqual([row["job_id"] for row in retry], [second])
        self.assertEqual(self.store.get_admin_job(first)["status"], "ready")

    def test_run_lease_allows_one_owner_and_expires(self):
        now = utc_now()
        self.assertTrue(self.store.acquire_run_lease("first-run", lease_minutes=15, now=now))
        self.assertFalse(self.store.acquire_run_lease("second-run", lease_minutes=15, now=now))
        self.assertTrue(self.store.acquire_run_lease("second-run", lease_minutes=15, now=now + timedelta(minutes=16)))

    def test_run_lease_renewal_and_release_require_the_owner(self):
        now = utc_now()
        self.assertTrue(self.store.acquire_run_lease("first-run", now=now))
        self.assertFalse(self.store.renew_run_lease("second-run", now=now + timedelta(minutes=1)))
        self.assertFalse(self.store.release_run_lease("second-run"))
        self.assertTrue(self.store.renew_run_lease("first-run", now=now + timedelta(minutes=1)))
        self.assertTrue(self.store.release_run_lease("first-run"))
        self.assertTrue(self.store.acquire_run_lease("second-run", now=now + timedelta(minutes=1)))

    def test_evidence_version_change_clears_stale_letter(self):
        job = self.job()
        job_id = self.store.enqueue_job(job)
        self.store.claim_batch()
        self.store.commit_result(job_id, valid_letter(), "de")
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE cover_letters SET evidence_version='older-profile' WHERE job_id=?",
                (job_id,),
            )
        self.store.enqueue_job(job)
        refreshed = self.store.get_admin_job(job_id)
        self.assertEqual(refreshed["status"], "queued")
        self.assertIsNone(refreshed["full_text"])
        self.assertIsNone(refreshed["public_text"])
        self.assertEqual(refreshed["attempts"], 0)

    def test_retention_hides_old_jobs_but_preserves_applied_history(self):
        old = iso_utc(utc_now() - timedelta(days=35))
        job_id = self.store.enqueue_job(self.job(first_seen=old))
        self.store.set_application(job_id, True, "Sent")
        self.assertEqual(self.store.public_jobs(), [])
        self.assertEqual(self.store.prune(), 0)
        self.assertTrue(self.store.get_admin_job(job_id)["applied"])

    def test_cloud_state_import_preserves_private_application_history(self):
        job_id = self.store.enqueue_job(self.job())
        result = self.store.import_cloud_state({
            "applications": [{
                "job_id": job_id,
                "applied": 1,
                "applied_at": iso_utc(),
                "notes": "Cloud-only note",
                "updated_at": iso_utc(),
            }],
            "regeneration_requests": [],
        })
        self.assertEqual(result["applications"], 1)
        restored = self.store.get_admin_job(job_id)
        self.assertTrue(restored["applied"])
        self.assertEqual(restored["notes"], "Cloud-only note")

    def test_cloud_regeneration_request_requeues_ready_letter(self):
        job_id = self.store.enqueue_job(self.job())
        self.store.claim_batch()
        self.store.commit_result(job_id, valid_letter(), "de")
        requested_at = iso_utc()
        result = self.store.import_cloud_state({
            "applications": [],
            "regeneration_requests": [{
                "job_id": job_id,
                "regeneration_requested_at": requested_at,
            }],
        })
        self.assertEqual(result["regeneration_requests"], 1)
        refreshed = self.store.get_admin_job(job_id)
        self.assertEqual(refreshed["status"], "queued")
        self.assertIsNone(refreshed["full_text"])

    def test_trend_snapshots_are_idempotent_sparse_and_retained(self):
        today = utc_now().date()
        weekday = (today - timedelta(days=2)).isoformat()
        older = (today - timedelta(days=101)).isoformat()
        self.assertEqual(self.store.sync_trend_snapshots([
            {"snapshot_date": weekday, "active_14d_count": 4, "recorded_at": iso_utc()},
            {"snapshot_date": older, "active_14d_count": 99, "recorded_at": iso_utc()},
        ]), 2)
        self.assertEqual(self.store.sync_trend_snapshots([
            {"snapshot_date": weekday, "active_14d_count": 7, "recorded_at": iso_utc()},
        ]), 1)
        self.store.record_trend_snapshot(today.isoformat(), 11)
        points = self.store.trend_snapshots(90)
        self.assertEqual([(row["snapshot_date"], row["active_14d_count"]) for row in points], [
            (weekday, 7),
            (today.isoformat(), 11),
        ])
        state = self.store.cloud_state()
        self.assertEqual(state["trend_snapshots"], points)

    def test_cloud_state_import_restores_trend_history(self):
        snapshot = {
            "snapshot_date": utc_now().date().isoformat(),
            "active_14d_count": 12,
            "recorded_at": iso_utc(),
        }
        result = self.store.import_cloud_state({
            "applications": [],
            "regeneration_requests": [],
            "trend_snapshots": [snapshot],
        })
        self.assertEqual(result["trend_snapshots"], 1)
        self.assertEqual(self.store.trend_snapshots(), [snapshot])

    def test_kk_cloud_sync_falls_back_to_legacy_routes_on_profile_404(self):
        missing = Mock(status_code=404)
        legacy_state = Mock(status_code=200)
        legacy_state.json.return_value = {
            "applications": [],
            "regeneration_requests": [],
            "trend_snapshots": [],
        }
        pushed = Mock(status_code=200)
        pushed.json.return_value = {"ok": True}

        with patch("requests.get", side_effect=[missing, legacy_state]) as get, patch(
            "requests.post", return_value=pushed
        ) as post:
            result = self.store.sync_cloud("https://example.test", "secret")

        self.assertEqual(get.call_args_list[0].args[0], "https://example.test/api/v1/profiles/kk/state")
        self.assertEqual(get.call_args_list[1].args[0], "https://example.test/api/v1/state")
        self.assertEqual(post.call_args.args[0], "https://example.test/api/v1/sync")
        self.assertEqual(result["pushed"], {"ok": True})

    def test_public_jobs_support_independent_and_combined_sorting(self):
        now = utc_now()
        fixtures = [
            ("a", now - timedelta(days=1), 5),
            ("b", now - timedelta(days=1), 9),
            ("c", now - timedelta(days=2), 10),
            ("d", None, None),
        ]
        for suffix, posted_at, score in fixtures:
            job = self.job(
                f"https://example.test/jobs/{suffix}",
                first_seen=iso_utc(now - timedelta(days=3) if suffix == "d" else now),
            )
            job["score"] = score
            if posted_at:
                job["posted_at"] = iso_utc(posted_at)
            job["title"] = suffix.upper()
            self.store.enqueue_job(job)

        titles = lambda rows: [row["title"] for row in rows]
        self.assertEqual(titles(self.store.public_jobs()), ["B", "A", "C", "D"])
        date_asc = titles(self.store.public_jobs(primary_sort="date_asc", secondary_sort=None))
        self.assertEqual(date_asc[:2], ["D", "C"])
        self.assertEqual(set(date_asc[2:]), {"A", "B"})
        self.assertEqual(date_asc, titles(self.store.public_jobs(primary_sort="date_asc", secondary_sort=None)))
        self.assertEqual(titles(self.store.public_jobs(primary_sort="score_desc", secondary_sort=None)), ["C", "B", "A", "D"])
        self.assertEqual(titles(self.store.public_jobs(primary_sort="score_asc", secondary_sort=None)), ["A", "B", "C", "D"])
        self.assertEqual(titles(self.store.public_jobs(primary_sort="score_desc", secondary_sort="date_desc")), ["C", "B", "A", "D"])
        self.assertEqual(titles(self.store.public_jobs(primary_sort="date_asc", secondary_sort="score_asc")), ["D", "C", "A", "B"])
        self.assertEqual(
            titles(self.store.public_jobs("active", 7, "score_asc", "date_desc")),
            ["B", "C"],
        )
        self.assertEqual(
            titles(self.store.public_jobs(primary_sort="date_desc", secondary_sort="date_asc")),
            titles(self.store.public_jobs(primary_sort="date_desc", secondary_sort=None)),
        )

    def test_public_jobs_reject_unknown_sort_modes(self):
        self.store.enqueue_job(self.job())
        with self.assertRaisesRegex(ValueError, "sort mode"):
            self.store.public_jobs(primary_sort="DROP TABLE jobs")


class CloudPayloadTests(unittest.TestCase):
    def test_server_derives_redaction_and_rejects_mismatched_id(self):
        raw = {
            "url": "https://example.test/jobs/cloud",
            "title": "Cloud Job",
            "description": "Embedded verification. " * 30,
            "status": "ready",
            "language": "de",
            "full_text": valid_letter(),
            "public_text": "LEAKED CLIENT VALUE",
        }
        prepared = prepare_sync_record(raw)
        self.assertNotEqual(prepared["public_text"], raw["public_text"])
        self.assertNotIn("@", prepared["public_text"])
        with self.assertRaisesRegex(ValueError, "job ID"):
            prepare_sync_record({**raw, "job_id": "wrong"})

    def test_server_scopes_identity_and_redaction_to_profile(self):
        raw = {
            "url": "https://example.test/jobs/shared",
            "title": "AI Engineer",
            "description": "Build grounded machine learning services. " * 30,
            "status": "ready",
            "language": "en",
            "full_text": valid_english_letter().replace(
                "Dear Hiring Manager,",
                "Dear Hiring Manager,\n\nMy husband lives in Germany, and I am learning German while seeking international experience.",
            ),
        }
        kk = prepare_sync_record(raw, "kk")
        sandra = prepare_sync_record(raw, "sandra")
        self.assertNotEqual(kk["job_id"], sandra["job_id"])
        self.assertEqual(sandra["profile_id"], "sandra")
        self.assertNotIn("husband", sandra["public_text"].lower())


class WebPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(TEST_TEMP_ROOT, f"web-{uuid.uuid4().hex}.db")
        self.store = CoverLetterStore(self.db)
        self.job_id = self.store.enqueue_job({
            "url": "https://example.test/jobs/secure",
            "title": "Secure Embedded Role",
            "company": "Example GmbH",
            "source": "LinkedIn",
            "score": 9,
            "description": "Embedded software verification and validation. " * 20,
        })
        self.store.claim_batch()
        self.full = valid_letter()
        self.store.commit_result(self.job_id, self.full, "de")
        self.app = CoverLetterWebApp(self.store, hash_password("correct horse"), "test-secret", secure_cookie=False, sync_token="sync-secret")

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = self.db + suffix
            if os.path.exists(path):
                os.remove(path)

    def request(self, path, method="GET", data=None, cookie=""):
        body = urlencode(data or {}).encode()
        captured = {}
        path_info, _, query_string = path.partition("?")
        environ = {
            "PATH_INFO": path_info,
            "QUERY_STRING": query_string,
            "REQUEST_METHOD": method,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_COOKIE": cookie,
        }
        def start(status, headers):
            captured["status"] = status
            captured["headers"] = headers
        response = b"".join(self.app(environ, start)).decode()
        return captured, response

    def test_guest_pages_never_return_full_letter(self):
        _, home = self.request("/")
        _, sample = self.request(f"/jobs/{self.job_id}/cover-letter")
        self.assertNotIn("rkrishnakumar097@gmail.com", home + sample)
        self.assertNotIn("Mit freundlichen", sample)
        self.assertNotIn("Krishnakumar Radhakrishna Panicker", sample)
        self.assertIn("Cover letter sample", home)

    def test_guest_donation_and_faq_are_public_without_private_data(self):
        self.store.record_trend_snapshot(utc_now().date().isoformat(), 1)
        _, home = self.request("/")
        _, faq = self.request("/faq")
        self.assertIn("Buy me a coffee", home)
        self.assertIn("/static/donation-qr.jpeg", home)
        self.assertIn('<dialog id="donation-dialog"', home)
        self.assertNotIn('<section class="card donation">', home)
        self.assertLess(home.index('<dialog id="donation-dialog"'), home.index('/static/donation-qr.jpeg'))
        self.assertNotIn("ChatGPT/Codex Cowork", home)
        self.assertIn("ChatGPT/Codex Cowork", faq)
        self.assertIn("Frequently asked questions", faq)
        self.assertIn("<details>", faq)
        self.assertIn("3 Month Job Trend", home)
        self.assertNotIn("Jobs in last 14 days — 3-month trend", home)
        self.assertIn("Trend history starts today", home)
        self.assertIn('role="img"', home)
        self.assertIn("LM Studio", faq)
        self.assertIn("Supabase", faq)
        self.assertIn("not necessarily confirmed closed", faq)
        self.assertIn("does not predict", faq)
        self.assertNotIn("rkrishnakumar097@gmail.com", home + faq)
        self.assertNotIn("Private dashboard", faq)

        captured = {}
        environ = {
            "PATH_INFO": "/static/donation-qr.jpeg",
            "QUERY_STRING": "",
            "REQUEST_METHOD": "GET",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
        }
        def start(status, headers):
            captured["status"] = status
            captured["headers"] = headers
        body = b"".join(self.app(environ, start))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(("Content-Type", "image/jpeg"), captured["headers"])
        self.assertTrue(body.startswith(b"\xff\xd8\xff"))

    def test_public_sort_controls_preserve_combined_selection(self):
        _, page = self.request("/?view=active&score=7&sort=date_asc&then=score_desc")
        self.assertIn('<option value="date_asc" selected>Oldest published</option>', page)
        self.assertIn('<option value="score_desc" selected>Highest score</option>', page)
        self.assertIn('name="view"', page)
        self.assertIn('name="score"', page)
        self.assertLess(page.index("Sort first"), page.index("Then by"))

    def test_admin_requires_authentication_and_returns_full_letter_after_login(self):
        captured, anonymous = self.request("/admin")
        self.assertTrue(captured["status"].startswith("303"))
        self.assertNotIn("rkrishnakumar097@gmail.com", anonymous)
        captured, _ = self.request("/admin/login", "POST", {"password": "correct horse"})
        cookie = next(value for key, value in captured["headers"] if key == "Set-Cookie").split(";", 1)[0]
        _, admin = self.request("/admin", cookie=cookie)
        self.assertIn("rkrishnakumar097@gmail.com", admin)
        self.assertIn("Private dashboard", admin)

    def test_sync_api_requires_bearer_token_and_never_accepts_public_sample(self):
        captured = {}
        payload = json.dumps({"jobs": []}).encode()

        def call(token=""):
            environ = {
                "PATH_INFO": "/api/v1/sync",
                "QUERY_STRING": "",
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(payload)),
                "wsgi.input": io.BytesIO(payload),
                "HTTP_AUTHORIZATION": token,
            }
            def start(status, headers):
                captured["status"] = status
            return b"".join(self.app(environ, start)).decode()

        self.assertIn("unauthorized", call())
        self.assertTrue(captured["status"].startswith("401"))
        self.store.sync_jobs = lambda jobs: len(jobs)
        self.assertIn('"synced": 0', call("Bearer sync-secret"))
        self.assertTrue(captured["status"].startswith("200"))

    def test_private_state_api_requires_bearer_token(self):
        captured = {}

        def call(token=""):
            environ = {
                "PATH_INFO": "/api/v1/state",
                "QUERY_STRING": "",
                "REQUEST_METHOD": "GET",
                "CONTENT_LENGTH": "0",
                "wsgi.input": io.BytesIO(b""),
                "HTTP_AUTHORIZATION": token,
            }
            def start(status, headers):
                captured["status"] = status
            return b"".join(self.app(environ, start)).decode()

        anonymous = call()
        self.assertTrue(captured["status"].startswith("401"))
        self.assertNotIn("applications", anonymous)
        authorized = call("Bearer sync-secret")
        self.assertTrue(captured["status"].startswith("200"))
        self.assertIn('"applications"', authorized)

    def test_duplicate_sync_upload_is_idempotent(self):
        record = self.store.sync_records()[0]
        self.assertEqual(self.store.sync_jobs([record]), 1)
        self.assertEqual(self.store.sync_jobs([record]), 1)
        with self.store.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id=?", (self.job_id,)).fetchone()[0]
        self.assertEqual(count, 1)


class MultiProfileWebTests(unittest.TestCase):
    def setUp(self):
        self.paths = {
            profile: os.path.join(TEST_TEMP_ROOT, f"web-{profile}-{uuid.uuid4().hex}.db")
            for profile in ("kk", "sandra")
        }
        self.stores = {
            profile: CoverLetterStore(path, profile_id=profile)
            for profile, path in self.paths.items()
        }
        for profile, store in self.stores.items():
            store.enqueue_job({
                "url": "https://example.test/jobs/shared",
                "title": f"{profile.title()} Role",
                "company": "Example GmbH",
                "source": "LinkedIn",
                "score": 9,
                "description": "Build and validate production software systems. " * 20,
            })
        self.app = CoverLetterWebApp(
            self.stores,
            hash_password("correct horse"),
            "test-secret",
            secure_cookie=False,
            sync_token={"kk": "kk-secret", "sandra": "sandra-secret"},
        )

    def tearDown(self):
        for path in self.paths.values():
            for suffix in ("", "-shm", "-wal"):
                candidate = path + suffix
                if os.path.exists(candidate):
                    os.remove(candidate)

    def request(self, path, method="GET", data=None, token="", cookie=""):
        body = urlencode(data or {}).encode()
        captured = {}
        path_info, _, query_string = path.partition("?")
        environ = {
            "PATH_INFO": path_info,
            "QUERY_STRING": query_string,
            "REQUEST_METHOD": method,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_COOKIE": cookie,
            "HTTP_AUTHORIZATION": token,
        }
        def start(status, headers):
            captured["status"] = status
            captured["headers"] = headers
        return captured, b"".join(self.app(environ, start)).decode()

    def test_profile_selector_and_lists_are_isolated(self):
        _, kk = self.request("/jobs/kk")
        _, sandra = self.request("/jobs/sandra")
        self.assertIn("Kk Role", kk)
        self.assertNotIn("Sandra Role", kk)
        self.assertIn("Sandra Role", sandra)
        self.assertNotIn("Kk Role", sandra)
        self.assertIn("KK Jobs", sandra)
        self.assertIn("Sandra Jobs", sandra)

    def test_profile_tokens_fail_closed(self):
        captured, _ = self.request("/api/v1/profiles/sandra/state", token="Bearer kk-secret")
        self.assertTrue(captured["status"].startswith("401"))
        captured, body = self.request("/api/v1/profiles/sandra/state", token="Bearer sandra-secret")
        self.assertTrue(captured["status"].startswith("200"))
        self.assertIn('"applications"', body)

    def test_legacy_kk_state_route_remains_available(self):
        captured, _ = self.request("/api/v1/state", token="Bearer kk-secret")
        self.assertTrue(captured["status"].startswith("200"))


if __name__ == "__main__":
    unittest.main()
