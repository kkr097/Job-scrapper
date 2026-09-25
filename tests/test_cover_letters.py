import io
import json
import os
import unittest
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from cover_letters import CoverLetterStore, iso_utc, utc_now
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
        environ = {
            "PATH_INFO": path,
            "QUERY_STRING": "",
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
        _, home = self.request("/")
        _, faq = self.request("/faq")
        self.assertIn("Buy me a coffee", home)
        self.assertIn("/static/donation-qr.jpeg", home)
        self.assertIn("ChatGPT/Codex Cowork", home + faq)
        self.assertIn("Frequently asked questions", faq)
        self.assertIn("<details>", faq)
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


if __name__ == "__main__":
    unittest.main()
