#!/usr/bin/env python3
"""Persistent cover-letter queue and command-line integration.

The scraper enqueues matched jobs here. A Codex scheduled task claims a batch,
uses the personal ``kk-cover-letter`` skill to draft each letter, and commits
results. Public samples are derived locally from the full letter so an agent
cannot accidentally publish contact details or the signature block.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


EVIDENCE_VERSION = "2026-09-25.1"
PROMPT_VERSION = "2026-09-25.1"
PROFILE_VERSIONS = {
    "kk": (EVIDENCE_VERSION, PROMPT_VERSION),
    "sandra": ("sandra-resume-2026-09-26.1", "sandra-cover-letter-2026-09-26.1"),
}
MIN_DESCRIPTION_CHARS = 300
PUBLIC_RETENTION_DAYS = 28
ACTIVE_DAYS = 14
TREND_WINDOW_DAYS = 90
TREND_RETENTION_DAYS = 100
MAX_ATTEMPTS = 5
DEFAULT_RUN_LEASE_MINUTES = 90
RUN_LEASE_NAME = "cover-letter-generation"
PUBLICATION_BATCH_SIZE = 250
PUBLICATION_LEASE_MINUTES = 15
PUBLICATION_FAILURE_ESCALATION = 3
DEFAULT_DB = "cover_letters.db"
PUBLIC_SORT_MODES = {
    "date_desc": ("date", "DESC"),
    "date_asc": ("date", "ASC"),
    "score_desc": ("score", "DESC"),
    "score_asc": ("score", "ASC"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_datetime(value: str | None, fallback: datetime | None = None) -> datetime:
    if value:
        text = value.strip()
        if text:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
    return fallback or utc_now()


def prepare_trend_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    snapshot_date = str(raw.get("snapshot_date") or "").strip()
    try:
        parsed_date = date.fromisoformat(snapshot_date)
    except ValueError as exc:
        raise ValueError("invalid trend snapshot date") from exc
    try:
        count = int(raw.get("active_14d_count"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid trend snapshot count") from exc
    if count < 0 or count > 1_000_000:
        raise ValueError("invalid trend snapshot count")
    recorded_at = iso_utc(parse_datetime(str(raw.get("recorded_at") or "")))
    return {
        "snapshot_date": parsed_date.isoformat(),
        "active_14d_count": count,
        "recorded_at": recorded_at,
    }


def public_sort_order(primary_sort: str = "date_desc", secondary_sort: str | None = "score_desc") -> str:
    """Build a portable, allowlisted ORDER BY expression for public jobs."""
    if primary_sort not in PUBLIC_SORT_MODES:
        raise ValueError("invalid public job sort mode")
    if secondary_sort is not None and secondary_sort not in PUBLIC_SORT_MODES:
        raise ValueError("invalid public job sort mode")

    clauses: list[str] = []
    used_fields: set[str] = set()
    for mode in (primary_sort, secondary_sort):
        if mode is None:
            continue
        field, direction = PUBLIC_SORT_MODES[mode]
        if field in used_fields:
            continue
        used_fields.add(field)
        if field == "date":
            clauses.append(f"COALESCE(j.posted_at,j.first_seen) {direction}")
        else:
            clauses.extend(("CASE WHEN j.score IS NULL THEN 1 ELSE 0 END ASC", f"j.score {direction}"))
    clauses.append("j.job_id ASC")
    return ", ".join(clauses)


def normalize_url(url: str) -> str:
    """Normalize a job URL while preserving meaningful search identifiers."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    path = re.sub(r"/+$", "", parts.path) or "/"
    ignored = {"trk", "trackingid", "ref", "utm_source", "utm_medium", "utm_campaign"}
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in ignored]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(query)), ""))


def job_id_for_url(url: str, profile_id: str = "kk") -> str:
    normalized = normalize_url(url)
    profile = (profile_id or "").strip().lower()
    if profile not in {"kk", "sandra"}:
        raise ValueError(f"unsupported profile: {profile_id!r}")
    identity = normalized if profile == "kk" else f"{profile}\n{normalized}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def description_hash(description: str) -> str:
    normalized = re.sub(r"\s+", " ", description or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def description_is_adequate(description: str) -> bool:
    return len(re.sub(r"\s+", " ", description or "").strip()) >= MIN_DESCRIPTION_CHARS


GREETING_RE = re.compile(r"^(?:sehr geehrte|guten tag|dear\b|hello\b)", re.IGNORECASE)
SIGNOFF_RE = re.compile(
    r"^(?:mit freundlichen gr(?:ü|ue)ßen|freundliche gr(?:ü|ue)ße|kind regards|best regards|sincerely)[, ]*$",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()/.-]{7,}\d)")
ADDRESS_RE = re.compile(r"\b(?:straße|str\.|street|road|weg|allee)\b", re.IGNORECASE)


def _body_bounds(lines: list[str]) -> tuple[int, int]:
    greeting = next((i for i, line in enumerate(lines) if GREETING_RE.match(line.strip())), None)
    signoff = next((i for i, line in enumerate(lines) if SIGNOFF_RE.match(line.strip())), None)
    if greeting is None or signoff is None or signoff <= greeting + 1:
        raise ValueError("letter must contain a recognizable greeting and sign-off")
    return greeting + 1, signoff


def validate_cover_letter(text: str) -> dict[str, int]:
    """Validate the copy-ready letter contract before storing it."""
    cleaned = (text or "").replace("\r\n", "\n").strip()
    if not cleaned:
        raise ValueError("letter is empty")
    if "—" in cleaned or "--" in cleaned:
        raise ValueError("em dashes and double hyphens are not allowed")
    lines = cleaned.splitlines()
    body_start, body_end = _body_bounds(lines)
    body = "\n".join(lines[body_start:body_end]).strip()
    words = re.findall(r"\b[\wÄÖÜäöüß'-]+\b", cleaned, re.UNICODE)
    if not 285 <= len(words) <= 375:
        raise ValueError(f"cover letter must contain 285-375 words; found {len(words)}")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if len(paragraphs) > 4:
        raise ValueError(f"cover letter may have at most four body paragraphs; found {len(paragraphs)}")
    return {"words": len(words), "body_paragraphs": len(paragraphs)}


SANDRA_PRIVATE_MOTIVATION_RE = re.compile(
    r"\b(?:husband|spouse|married|family|relocat(?:e|ion|ing)|visa|work authorization)\b",
    re.IGNORECASE,
)


def make_public_sample(full_text: str, profile_id: str = "kk") -> str:
    """Return body-only public text, or fail closed if safe bounds are unclear."""
    cleaned = (full_text or "").replace("\r\n", "\n").strip()
    lines = cleaned.splitlines()
    start, end = _body_bounds(lines)
    body = "\n".join(lines[start:end])
    paragraphs = re.split(r"\n\s*\n", body)
    if profile_id == "sandra":
        paragraphs = [p for p in paragraphs if not SANDRA_PRIVATE_MOTIVATION_RE.search(p)]
    body_lines = "\n\n".join(paragraphs).splitlines()
    public_lines: list[str] = []
    for line in body_lines:
        if EMAIL_RE.search(line) or PHONE_RE.search(line) or ADDRESS_RE.search(line):
            continue
        public_lines.append(line.rstrip())
    result = "\n".join(public_lines).strip()
    if not result:
        raise ValueError("redaction removed the entire public sample")
    return result


class CoverLetterStore:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB, profile_id: str = "kk"):
        self.path = str(path)
        self.profile_id = (profile_id or "").strip().lower()
        if self.profile_id not in {"kk", "sandra"}:
            raise ValueError(f"unsupported profile: {profile_id!r}")
        self.evidence_version, self.prompt_version = PROFILE_VERSIONS[self.profile_id]
        self._initialize()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    score REAL,
                    description TEXT NOT NULL DEFAULT '',
                    description_hash TEXT NOT NULL,
                    posted_at TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    auto_letter_eligible INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS cover_letters (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    description_hash TEXT NOT NULL,
                    evidence_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    language TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','generating','ready','unavailable')),
                    full_text TEXT,
                    public_text TEXT,
                    generated_at TEXT,
                    lease_until TEXT,
                    claim_owner TEXT,
                    regeneration_requested_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS applications (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    applied INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trend_snapshots (
                    snapshot_date TEXT PRIMARY KEY,
                    active_14d_count INTEGER NOT NULL CHECK(active_14d_count >= 0),
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_leases (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    lease_until TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publication_outbox (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('pending','publishing')) DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
                CREATE INDEX IF NOT EXISTS idx_letters_status ON cover_letters(status, lease_until);
                CREATE INDEX IF NOT EXISTS idx_publication_outbox_pending ON publication_outbox(status, lease_until, updated_at);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(cover_letters)")}
            if "regeneration_requested_at" not in columns:
                conn.execute("ALTER TABLE cover_letters ADD COLUMN regeneration_requested_at TEXT")
            if "claim_owner" not in columns:
                conn.execute("ALTER TABLE cover_letters ADD COLUMN claim_owner TEXT")
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "auto_letter_eligible" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN auto_letter_eligible INTEGER NOT NULL DEFAULT 1")

    def acquire_run_lease(
        self,
        owner: str,
        lease_minutes: int = DEFAULT_RUN_LEASE_MINUTES,
        now: datetime | None = None,
    ) -> bool:
        """Atomically claim the single cover-letter generator lease."""
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("lease owner is required")
        if lease_minutes < 1:
            raise ValueError("lease duration must be positive")
        now = now or utc_now()
        now_iso = iso_utc(now)
        lease_until = iso_utc(now + timedelta(minutes=lease_minutes))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT owner,lease_until FROM automation_leases WHERE name=?", (RUN_LEASE_NAME,)
            ).fetchone()
            if existing and existing["owner"] != owner and existing["lease_until"] > now_iso:
                return False
            conn.execute(
                """
                INSERT INTO automation_leases(name,owner,lease_until,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,
                    lease_until=excluded.lease_until,updated_at=excluded.updated_at
                """,
                (RUN_LEASE_NAME, owner, lease_until, now_iso),
            )
        return True

    def renew_run_lease(
        self,
        owner: str,
        lease_minutes: int = DEFAULT_RUN_LEASE_MINUTES,
        now: datetime | None = None,
    ) -> bool:
        """Extend a live lease only when it is still owned by this run."""
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("lease owner is required")
        if lease_minutes < 1:
            raise ValueError("lease duration must be positive")
        now = now or utc_now()
        changed = 0
        lease_until = iso_utc(now + timedelta(minutes=lease_minutes))
        now_iso = iso_utc(now)
        with self.connection() as conn:
            changed = conn.execute(
                """
                UPDATE automation_leases SET lease_until=?,updated_at=?
                WHERE name=? AND owner=? AND lease_until > ?
                """,
                (
                    lease_until,
                    now_iso,
                    RUN_LEASE_NAME,
                    owner,
                    now_iso,
                ),
            ).rowcount
            if changed:
                conn.execute(
                    """
                    UPDATE cover_letters SET lease_until=?
                    WHERE status='generating' AND claim_owner=? AND lease_until > ?
                    """,
                    (lease_until, owner, now_iso),
                )
        return changed == 1

    def release_run_lease(self, owner: str) -> bool:
        """Release the generator lease without allowing another run to be removed."""
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("lease owner is required")
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE cover_letters SET status='queued', lease_until=NULL, claim_owner=NULL,
                    error='Run released before generation completed.'
                WHERE status='generating' AND claim_owner=?
                """,
                (owner,),
            )
            changed = conn.execute(
                "DELETE FROM automation_leases WHERE name=? AND owner=?", (RUN_LEASE_NAME, owner)
            ).rowcount
        return changed == 1

    def renew_claims(
        self,
        owner: str,
        lease_minutes: int = DEFAULT_RUN_LEASE_MINUTES,
        now: datetime | None = None,
    ) -> int:
        """Renew this owner's item claims without acquiring a profile run lease."""
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("claim owner is required")
        now = now or utc_now()
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE cover_letters SET lease_until=?
                WHERE status='generating' AND claim_owner=? AND lease_until > ?
                """,
                (iso_utc(now + timedelta(minutes=lease_minutes)), owner, iso_utc(now)),
            ).rowcount

    def release_claims(self, owner: str, reason: str = "Nightly run stopped before this chunk.") -> int:
        """Return only this owner's unfinished items to the queue."""
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("claim owner is required")
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE cover_letters SET status='queued',lease_until=NULL,claim_owner=NULL,error=?
                WHERE status='generating' AND claim_owner=?
                """,
                (reason[:500], owner),
            ).rowcount

    def mark_existing_jobs_manual(self, cutover_at: str | None = None) -> int:
        """Idempotently freeze all rows present at cutover out of automatic drafting."""
        marker = f"automatic-eligibility-cutover:{self.profile_id}"
        timestamp = cutover_at or iso_utc()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT value FROM automation_metadata WHERE key=?", (marker,)
            ).fetchone()
            if existing:
                return 0
            changed = conn.execute(
                "UPDATE jobs SET auto_letter_eligible=0 WHERE auto_letter_eligible<>0"
            ).rowcount
            conn.execute(
                "INSERT INTO automation_metadata(key,value,updated_at) VALUES(?,?,?)",
                (marker, timestamp, iso_utc()),
            )
        return changed

    def enqueue_job(self, job: dict[str, Any], *, publish: bool = False) -> str | None:
        url = normalize_url(str(job.get("url") or job.get("URL") or ""))
        if not url:
            return None
        job_id = job_id_for_url(url, self.profile_id)
        description = str(job.get("description") or job.get("Description") or "").strip()
        desc_hash = description_hash(description)
        now = utc_now()
        first_seen = parse_datetime(str(job.get("first_seen") or ""), now)
        posted_raw = str(job.get("posted_at") or job.get("date_posted") or job.get("Date Posted") or "").strip()
        posted_at = iso_utc(parse_datetime(posted_raw)) if posted_raw else None
        score_raw = job.get("score", job.get("Score"))
        try:
            score = float(score_raw) if score_raw not in (None, "") else None
        except (TypeError, ValueError):
            score = None
        status = "queued" if description_is_adequate(description) else "unavailable"
        error = None if status == "queued" else "Job description is missing or too short for a grounded letter."

        with self.connection() as conn:
            existing = conn.execute(
                "SELECT description_hash FROM cover_letters WHERE job_id = ?", (job_id,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO jobs(job_id,url,title,company,location,source,score,description,
                                 description_hash,posted_at,first_seen,last_seen,auto_letter_eligible)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title=excluded.title, company=excluded.company, location=excluded.location,
                    source=excluded.source, score=excluded.score, description=excluded.description,
                    description_hash=excluded.description_hash,
                    posted_at=COALESCE(excluded.posted_at,jobs.posted_at), last_seen=excluded.last_seen
                """,
                (
                    job_id,
                    url,
                    str(job.get("title") or job.get("Title") or "Untitled job"),
                    str(job.get("company") or job.get("Company") or ""),
                    str(job.get("location") or job.get("Location") or ""),
                    str(job.get("source") or job.get("Source") or ""),
                    score,
                    description,
                    desc_hash,
                    posted_at,
                    iso_utc(first_seen),
                    iso_utc(now),
                    int(bool(job.get("auto_letter_eligible", True))),
                ),
            )
            if publish:
                conn.execute(
                    """
                    INSERT INTO publication_outbox(job_id,status,attempts,lease_owner,lease_until,last_error,updated_at)
                    VALUES(?,'pending',0,NULL,NULL,NULL,?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        status=CASE
                            WHEN publication_outbox.status='publishing'
                                 AND publication_outbox.lease_until > excluded.updated_at
                            THEN publication_outbox.status ELSE 'pending' END,
                        lease_owner=CASE
                            WHEN publication_outbox.status='publishing'
                                 AND publication_outbox.lease_until > excluded.updated_at
                            THEN publication_outbox.lease_owner ELSE NULL END,
                        lease_until=CASE
                            WHEN publication_outbox.status='publishing'
                                 AND publication_outbox.lease_until > excluded.updated_at
                            THEN publication_outbox.lease_until ELSE NULL END,
                        last_error=CASE
                            WHEN publication_outbox.status='publishing'
                                 AND publication_outbox.lease_until > excluded.updated_at
                            THEN publication_outbox.last_error ELSE NULL END,
                        updated_at=excluded.updated_at
                    """,
                    (job_id, iso_utc(now)),
                )
            reset = existing is None or existing["description_hash"] != desc_hash
            if reset:
                conn.execute(
                    """
                    INSERT INTO cover_letters(job_id,description_hash,evidence_version,prompt_version,status,error)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        description_hash=excluded.description_hash,
                        evidence_version=excluded.evidence_version,
                        prompt_version=excluded.prompt_version,
                        status=excluded.status, full_text=NULL, public_text=NULL,
                        generated_at=NULL, lease_until=NULL, claim_owner=NULL, regeneration_requested_at=NULL,
                        attempts=0, error=excluded.error
                    """,
                    (job_id, desc_hash, self.evidence_version, self.prompt_version, status, error),
                )
            else:
                conn.execute(
                    """
                    UPDATE cover_letters
                    SET status = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN ?
                            ELSE status END,
                        full_text = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN NULL
                            ELSE full_text END,
                        public_text = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN NULL
                            ELSE public_text END,
                        language = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN NULL
                            ELSE language END,
                        generated_at = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN NULL
                            ELSE generated_at END,
                        lease_until = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN NULL
                            ELSE lease_until END,
                        regeneration_requested_at = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN NULL
                            ELSE regeneration_requested_at END,
                        attempts = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN 0
                            ELSE attempts END,
                        error = CASE
                            WHEN evidence_version != ? OR prompt_version != ? THEN ?
                            ELSE error END,
                        evidence_version=?, prompt_version=?
                    WHERE job_id=?
                    """,
                    (
                        self.evidence_version, self.prompt_version, status,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version,
                        self.evidence_version, self.prompt_version, error,
                        self.evidence_version, self.prompt_version, job_id,
                    ),
                )
        return job_id

    def pending_publication_ids(self) -> list[str]:
        """Return this profile's unsent website updates in oldest-first order."""
        now_iso = iso_utc()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT job_id FROM publication_outbox
                WHERE status='pending' OR (status='publishing' AND lease_until < ?)
                ORDER BY updated_at,job_id
                """,
                (now_iso,),
            ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def _claim_publication_batch(
        self,
        owner: str,
        limit: int = PUBLICATION_BATCH_SIZE,
        lease_minutes: int = PUBLICATION_LEASE_MINUTES,
    ) -> list[str]:
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("publication owner is required")
        now = utc_now()
        now_iso = iso_utc(now)
        lease_until = iso_utc(now + timedelta(minutes=lease_minutes))
        batch_limit = min(max(1, limit), PUBLICATION_BATCH_SIZE)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT job_id FROM publication_outbox
                WHERE status='pending' OR (status='publishing' AND lease_until < ?)
                ORDER BY updated_at,job_id LIMIT ?
                """,
                (now_iso, batch_limit),
            ).fetchall()
            ids = [str(row["job_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE publication_outbox
                    SET status='publishing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=?
                    WHERE job_id IN ({placeholders})
                    """,
                    (owner, lease_until, now_iso, *ids),
                )
        return ids

    def _acknowledge_publications(self, job_ids: Iterable[str], owner: str | None = None) -> int:
        ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        sql = f"DELETE FROM publication_outbox WHERE job_id IN ({placeholders})"
        params: list[Any] = list(ids)
        if owner is not None:
            sql += " AND status='publishing' AND lease_owner=?"
            params.append(owner)
        with self.connection() as conn:
            return conn.execute(sql, params).rowcount

    def _release_publications(self, owner: str, job_ids: Iterable[str], error: str) -> int:
        ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as conn:
            return conn.execute(
                f"""
                UPDATE publication_outbox
                SET status='pending',lease_owner=NULL,lease_until=NULL,last_error=?,updated_at=?
                WHERE job_id IN ({placeholders}) AND status='publishing' AND lease_owner=?
                """,
                ((error or "Publication failed.")[:500], iso_utc(), *ids, owner),
            ).rowcount

    def publish_pending_cloud(
        self,
        base_url: str,
        token: str,
        owner: str,
        limit: int = PUBLICATION_BATCH_SIZE,
    ) -> dict[str, Any]:
        """Publish scored matches in bounded delta batches without affecting scoring."""
        published_ids: list[str] = []
        failure: str | None = None
        batch_limit = min(max(1, limit), PUBLICATION_BATCH_SIZE)
        while True:
            ids = self._claim_publication_batch(owner, batch_limit)
            if not ids:
                break
            try:
                self.push_cloud(base_url, token, mode="delta", job_ids=ids)
            except Exception as exc:
                failure = str(exc)
                self._release_publications(owner, ids, failure)
                break
            self._acknowledge_publications(ids, owner)
            published_ids.extend(ids)
            if len(ids) < batch_limit:
                break
        pending_ids = self.pending_publication_ids()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempts),0) AS attempts FROM publication_outbox"
            ).fetchone()
        return {
            "published": len(published_ids),
            "published_job_ids": published_ids,
            "pending": len(pending_ids),
            "failure": failure,
            "escalated": bool(
                failure and int(row["attempts"] or 0) >= PUBLICATION_FAILURE_ESCALATION
            ),
        }

    def sync_csv(
        self,
        csv_path: str | os.PathLike[str],
        *,
        auto_letter_eligible: bool = True,
        first_seen: str | None = None,
    ) -> int:
        count = 0
        with open(csv_path, newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                row["auto_letter_eligible"] = auto_letter_eligible
                if first_seen:
                    row["first_seen"] = first_seen
                if self.enqueue_job(row):
                    count += 1
        return count

    def claim_batch(
        self,
        limit: int = 10,
        lease_minutes: int = 90,
        owner: str | None = None,
        min_score: float | None = None,
        eligible_only: bool = False,
        daily_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        owner = (owner or "").strip()
        if eligible_only:
            limit = min(limit, 40)
        if daily_limit is not None:
            remaining = max(0, daily_limit - self.generated_today())
            if remaining == 0:
                return []
            limit = min(limit, remaining)
        now = utc_now()
        lease_until = iso_utc(now + timedelta(minutes=lease_minutes))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            filters = ["c.attempts < ?", "(c.status='queued' OR (c.status='generating' AND c.lease_until < ?))"]
            params: list[Any] = [MAX_ATTEMPTS, iso_utc(now)]
            if min_score is not None:
                filters.append("j.score >= ?")
                params.append(min_score)
            if eligible_only:
                filters.append("j.auto_letter_eligible=1")
            rows = conn.execute(
                f"""
                SELECT j.*, c.attempts
                FROM jobs j JOIN cover_letters c USING(job_id)
                WHERE {' AND '.join(filters)}
                ORDER BY j.first_seen DESC, j.score DESC, j.job_id
                LIMIT ?
                """,
                (*params, max(1, limit)),
            ).fetchall()
            ids = [row["job_id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE cover_letters
                    SET status='generating', lease_until=?, claim_owner=?, attempts=attempts+1
                    WHERE job_id IN ({placeholders})
                    """,
                    (lease_until, owner or None, *ids),
                )
            return [dict(row) for row in rows]

    def generated_today(self) -> int:
        local_timezone = datetime.now().astimezone().tzinfo
        local_date = utc_now().astimezone(local_timezone).date()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT generated_at FROM cover_letters WHERE status='ready' AND generated_at IS NOT NULL"
            ).fetchall()
        return sum(
            parse_datetime(row["generated_at"]).astimezone(local_timezone).date() == local_date
            for row in rows
        )

    def commit_result(self, job_id: str, full_text: str, language: str) -> dict[str, int]:
        language = (language or "").lower()
        if language not in {"de", "en"}:
            raise ValueError("language must be 'de' or 'en'")
        metrics = validate_cover_letter(full_text)
        public_text = make_public_sample(full_text, self.profile_id)
        with self.connection() as conn:
            changed = conn.execute(
                """
                UPDATE cover_letters SET status='ready', full_text=?, public_text=?, language=?,
                    generated_at=?, lease_until=NULL, claim_owner=NULL,
                    regeneration_requested_at=NULL, error=NULL
                WHERE job_id=? AND status='generating'
                """,
                (full_text.strip(), public_text, language, iso_utc(), job_id),
            ).rowcount
            if changed != 1:
                raise ValueError("job is not currently claimed for generation")
        return metrics

    def commit_batch_results(self, results: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Persist a chunk in one session while isolating each result with a savepoint."""
        summary: dict[str, Any] = {
            "committed": 0,
            "unavailable": 0,
            "failed": 0,
            "changed_job_ids": [],
            "failed_job_ids": [],
        }
        with self.connection() as conn:
            for index, raw in enumerate(results):
                item = dict(raw)
                job_id = str(item.get("job_id") or "")
                savepoint = f"chunk_item_{index}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    if item.get("status") == "unavailable":
                        changed = conn.execute(
                            """
                            UPDATE cover_letters SET status='unavailable',full_text=NULL,public_text=NULL,
                                generated_at=?,lease_until=NULL,claim_owner=NULL,error=?
                            WHERE job_id=? AND status='generating'
                            """,
                            (
                                iso_utc(),
                                str(item.get("reason") or "Grounded letter unavailable.")[:500],
                                job_id,
                            ),
                        ).rowcount
                        if changed != 1:
                            raise ValueError("job is not currently claimed for generation")
                        summary["unavailable"] += 1
                    else:
                        language = str(item.get("language") or "").lower()
                        if language not in {"de", "en"}:
                            raise ValueError("language must be 'de' or 'en'")
                        full_text = str(item.get("full_text") or "")
                        validate_cover_letter(full_text)
                        public_text = make_public_sample(full_text, self.profile_id)
                        changed = conn.execute(
                            """
                            UPDATE cover_letters SET status='ready',full_text=?,public_text=?,language=?,
                                generated_at=?,lease_until=NULL,claim_owner=NULL,
                                regeneration_requested_at=NULL,error=NULL
                            WHERE job_id=? AND status='generating'
                            """,
                            (full_text.strip(), public_text, language, iso_utc(), job_id),
                        ).rowcount
                        if changed != 1:
                            raise ValueError("job is not currently claimed for generation")
                        summary["committed"] += 1
                    summary["changed_job_ids"].append(job_id)
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception as exc:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    conn.execute(
                        """
                        UPDATE cover_letters SET error=?
                        WHERE job_id=? AND status='generating'
                        """,
                        (str(exc)[:500], job_id),
                    )
                    summary["failed"] += 1
                    if job_id:
                        summary["failed_job_ids"].append(job_id)
        return summary

    def mark_unavailable(self, job_id: str, reason: str) -> None:
        with self.connection() as conn:
            changed = conn.execute(
                """
                UPDATE cover_letters SET status='unavailable', full_text=NULL, public_text=NULL,
                    generated_at=?, lease_until=NULL, claim_owner=NULL, error=?
                WHERE job_id=? AND status='generating'
                """,
                (iso_utc(), (reason or "Grounded letter unavailable.")[:500], job_id),
            ).rowcount
            if changed != 1:
                raise ValueError("job is not currently claimed for generation")

    def release_with_error(self, job_id: str, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE cover_letters SET status='queued', lease_until=NULL, claim_owner=NULL, error=? WHERE job_id=? AND status='generating'",
                ((error or "Generation failed.")[:500], job_id),
            )

    def regenerate(self, job_id: str) -> bool:
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE cover_letters SET status=CASE
                    WHEN length(trim((SELECT description FROM jobs WHERE jobs.job_id=cover_letters.job_id))) >= ?
                    THEN 'queued' ELSE 'unavailable' END,
                    full_text=NULL, public_text=NULL, generated_at=NULL,
                    lease_until=NULL, claim_owner=NULL, regeneration_requested_at=?, attempts=0, error=NULL
                WHERE job_id=?
                """,
                (MIN_DESCRIPTION_CHARS, iso_utc(), job_id),
            ).rowcount == 1

    def set_application(self, job_id: str, applied: bool, notes: str = "") -> None:
        now = iso_utc()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO applications(job_id,applied,applied_at,notes,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET applied=excluded.applied,
                    applied_at=excluded.applied_at, notes=excluded.notes, updated_at=excluded.updated_at
                """,
                (job_id, int(applied), now if applied else None, notes[:4000], now),
            )

    def public_jobs(
        self,
        view: str = "all",
        min_score: float | None = None,
        primary_sort: str = "date_desc",
        secondary_sort: str | None = "score_desc",
    ) -> list[dict[str, Any]]:
        now = utc_now()
        order_by = public_sort_order(primary_sort, secondary_sort)
        cutoff = iso_utc(now - timedelta(days=PUBLIC_RETENTION_DAYS))
        active_cutoff = iso_utc(now - timedelta(days=ACTIVE_DAYS))
        conditions = ["COALESCE(j.posted_at,j.first_seen) >= ?"]
        params: list[Any] = [cutoff]
        if view == "active":
            conditions.append("COALESCE(j.posted_at,j.first_seen) >= ?")
            params.append(active_cutoff)
        elif view == "expired":
            conditions.append("COALESCE(j.posted_at,j.first_seen) < ?")
            params.append(active_cutoff)
        if min_score is not None:
            conditions.append("j.score >= ?")
            params.append(min_score)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT j.job_id,j.url,j.title,j.company,j.location,j.source,j.score,
                       j.posted_at,j.first_seen,c.status,c.public_text,c.language,c.generated_at,
                       CASE WHEN COALESCE(j.posted_at,j.first_seen) >= ? THEN 'active' ELSE 'expired' END AS age_status
                FROM jobs j JOIN cover_letters c USING(job_id)
                WHERE {' AND '.join(conditions)}
                ORDER BY {order_by}
                """,
                (active_cutoff, *params),
            ).fetchall()
        return [{**dict(row), "profile_id": self.profile_id} for row in rows]

    def admin_jobs(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT j.*,c.status,c.full_text,c.public_text,c.language,c.generated_at,c.error,c.attempts,
                       COALESCE(a.applied,0) AS applied,a.applied_at,a.notes
                FROM jobs j JOIN cover_letters c USING(job_id)
                LEFT JOIN applications a USING(job_id)
                ORDER BY j.first_seen DESC
                """
            ).fetchall()
        return [{**dict(row), "profile_id": self.profile_id} for row in rows]

    def sync_records(self, job_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Return recent jobs plus applied history for authenticated cloud sync."""
        cutoff = iso_utc(utc_now() - timedelta(days=PUBLIC_RETENTION_DAYS))
        selected_ids = list(dict.fromkeys(str(job_id) for job_id in (job_ids or []) if job_id))
        if job_ids is not None and not selected_ids:
            return []
        id_filter = ""
        params: list[Any] = [cutoff]
        if job_ids is not None:
            id_filter = f" AND j.job_id IN ({','.join('?' for _ in selected_ids)})"
            params.extend(selected_ids)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT j.*,c.evidence_version,c.prompt_version,c.language,c.status,
                       c.full_text,c.generated_at,c.attempts,c.error
                FROM jobs j JOIN cover_letters c USING(job_id)
                LEFT JOIN applications a USING(job_id)
                WHERE (COALESCE(j.posted_at,j.first_seen) >= ? OR COALESCE(a.applied,0)=1)
                {id_filter}
                ORDER BY j.first_seen,j.job_id
                """,
                params,
            ).fetchall()
        return [{**dict(row), "profile_id": self.profile_id} for row in rows]

    def record_trend_snapshot(self, snapshot_date: str | None = None, count: int | None = None) -> dict[str, Any]:
        point = prepare_trend_snapshot({
            "snapshot_date": snapshot_date or utc_now().date().isoformat(),
            "active_14d_count": len(self.public_jobs("active")) if count is None else count,
            "recorded_at": iso_utc(),
        })
        self.sync_trend_snapshots([point])
        return point

    def sync_trend_snapshots(self, records: Iterable[dict[str, Any]]) -> int:
        prepared = [prepare_trend_snapshot(record) for record in records]
        cutoff = (utc_now().date() - timedelta(days=TREND_RETENTION_DAYS)).isoformat()
        with self.connection() as conn:
            for point in prepared:
                conn.execute(
                    """
                    INSERT INTO trend_snapshots(snapshot_date,active_14d_count,recorded_at)
                    VALUES(?,?,?)
                    ON CONFLICT(snapshot_date) DO UPDATE SET
                        active_14d_count=excluded.active_14d_count,
                        recorded_at=excluded.recorded_at
                    """,
                    (point["snapshot_date"], point["active_14d_count"], point["recorded_at"]),
                )
            conn.execute("DELETE FROM trend_snapshots WHERE snapshot_date < ?", (cutoff,))
        return len(prepared)

    def trend_snapshots(self, days: int = TREND_WINDOW_DAYS) -> list[dict[str, Any]]:
        cutoff = (utc_now().date() - timedelta(days=max(1, days) - 1)).isoformat()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT snapshot_date,active_14d_count,recorded_at
                FROM trend_snapshots WHERE snapshot_date >= ? ORDER BY snapshot_date
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def import_cloud_state(self, state: dict[str, Any]) -> dict[str, int]:
        """Back up cloud-only application state and apply regeneration requests."""
        applications = state.get("applications") or []
        requests = state.get("regeneration_requests") or []
        snapshots = state.get("trend_snapshots") or []
        imported = regenerated = 0
        with self.connection() as conn:
            for item in applications:
                job_id = str(item.get("job_id") or "")
                if not job_id:
                    continue
                changed = conn.execute(
                    """
                    INSERT INTO applications(job_id,applied,applied_at,notes,updated_at)
                    SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM jobs WHERE job_id=?)
                    ON CONFLICT(job_id) DO UPDATE SET applied=excluded.applied,
                        applied_at=excluded.applied_at,notes=excluded.notes,updated_at=excluded.updated_at
                    WHERE excluded.updated_at >= applications.updated_at
                    """,
                    (
                        job_id,
                        int(bool(item.get("applied"))),
                        item.get("applied_at"),
                        str(item.get("notes") or "")[:4000],
                        str(item.get("updated_at") or iso_utc()),
                        job_id,
                    ),
                ).rowcount
                imported += int(changed > 0)
            for item in requests:
                job_id = str(item.get("job_id") or "")
                requested_at = str(item.get("regeneration_requested_at") or "")
                if not job_id or not requested_at:
                    continue
                changed = conn.execute(
                    """
                    UPDATE cover_letters SET status='queued',full_text=NULL,public_text=NULL,
                        generated_at=NULL,lease_until=NULL,claim_owner=NULL,attempts=0,error=NULL,
                        regeneration_requested_at=?
                    WHERE job_id=? AND (
                        regeneration_requested_at IS NULL OR regeneration_requested_at < ?
                    )
                    """,
                    (requested_at, job_id, requested_at),
                ).rowcount
                regenerated += int(changed > 0)
        imported_snapshots = self.sync_trend_snapshots(snapshots)
        return {
            "applications": imported,
            "regeneration_requests": regenerated,
            "trend_snapshots": imported_snapshots,
        }

    def cloud_state(self) -> dict[str, list[dict[str, Any]]]:
        """Expose syncable private state when running the website locally."""
        with self.connection() as conn:
            applications = conn.execute(
                "SELECT job_id,applied,applied_at,notes,updated_at FROM applications ORDER BY updated_at"
            ).fetchall()
            requests = conn.execute(
                """
                SELECT job_id,regeneration_requested_at FROM cover_letters
                WHERE regeneration_requested_at IS NOT NULL ORDER BY regeneration_requested_at
                """
            ).fetchall()
        return {
            "applications": [dict(row) for row in applications],
            "regeneration_requests": [dict(row) for row in requests],
            "trend_snapshots": self.trend_snapshots(TREND_RETENTION_DAYS),
        }

    def sync_jobs(self, records: Iterable[dict[str, Any]], *, prune: bool = True) -> int:
        """Accept authenticated sync payloads for local deployment/testing."""
        from cloud_store import prepare_sync_record

        prepared = [prepare_sync_record(record, self.profile_id) for record in records]
        for item in prepared:
            self.enqueue_job(item)
            with self.connection() as conn:
                conn.execute(
                    """
                    UPDATE cover_letters SET description_hash=?,evidence_version=?,prompt_version=?,
                        language=?,status=?,full_text=?,public_text=?,generated_at=?,attempts=?,error=?,
                        regeneration_requested_at=CASE
                            WHEN ?='ready' THEN NULL ELSE regeneration_requested_at END
                    WHERE job_id=?
                    """,
                    (
                        item["description_hash"], item["evidence_version"], item["prompt_version"],
                        item["language"], item["status"], item["full_text"], item["public_text"],
                        item["generated_at"], item["attempts"], item["error"], item["status"], item["job_id"],
                    ),
                )
        if prune:
            self.prune()
        return len(prepared)

    def _cloud_settings(self, base_url: str, token: str) -> tuple[str, dict[str, str]]:
        """Validate cloud configuration and return the profile API base plus headers."""
        base_url = (base_url or "").strip().rstrip("/")
        parts = urlsplit(base_url)
        if parts.scheme != "https" and parts.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("cloud sync requires HTTPS")
        if not token:
            raise ValueError("COVER_LETTER_SYNC_TOKEN is required")
        return (
            f"{base_url}/api/v1/profiles/{self.profile_id}",
            {"Authorization": f"Bearer {token}", "User-Agent": "matchatlas-sync/3"},
        )

    def pull_cloud(self, base_url: str, token: str) -> dict[str, int]:
        """Download private application state once before a generation run."""
        import requests

        profile_base, headers = self._cloud_settings(base_url, token)
        state_response = requests.get(f"{profile_base}/state", headers=headers, timeout=30)
        if state_response.status_code == 404 and self.profile_id == "kk":
            # Keep KK synchronization compatible while the deployed service is
            # still on the pre-profile API. Never fall back for other profiles,
            # because the legacy routes are KK-only.
            profile_base = f"{base_url.rstrip('/')}/api/v1"
            state_response = requests.get(f"{profile_base}/state", headers=headers, timeout=30)
        state_response.raise_for_status()
        self._last_cloud_profile_base = profile_base
        return self.import_cloud_state(state_response.json())

    def push_cloud(
        self,
        base_url: str,
        token: str,
        *,
        mode: str = "full",
        job_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Upload a changed chunk or perform a complete final reconciliation."""
        import requests

        if mode not in {"delta", "full"}:
            raise ValueError("sync mode must be 'delta' or 'full'")
        profile_base, headers = self._cloud_settings(base_url, token)
        profile_base = getattr(self, "_last_cloud_profile_base", profile_base)
        if mode == "delta" and job_ids is None:
            raise ValueError("delta sync requires changed job ids")
        if mode == "full":
            self.record_trend_snapshot()
            records = self.sync_records()
            payload: dict[str, Any] = {
                "mode": "full",
                "jobs": records,
                "trend_snapshots": self.trend_snapshots(TREND_RETENTION_DAYS),
            }
        else:
            records = self.sync_records(job_ids)
            payload = {"mode": "delta", "jobs": records}
        push_response = requests.post(
            f"{profile_base}/sync",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if (
            push_response.status_code == 404
            and self.profile_id == "kk"
            and "/profiles/kk" in profile_base
        ):
            push_response = requests.post(
                f"{base_url.rstrip('/')}/api/v1/sync",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
        push_response.raise_for_status()
        if mode == "full":
            self._acknowledge_publications(record["job_id"] for record in records)
        return {"mode": mode, "jobs": len(records), "response": push_response.json()}

    def sync_cloud(self, base_url: str, token: str) -> dict[str, Any]:
        """Compatibility alias: pull state, then perform one full push."""
        pulled = self.pull_cloud(base_url, token)
        pushed = self.push_cloud(base_url, token, mode="full")
        return {"pulled": pulled, "pushed": pushed["response"]}

    def get_public_job(self, job_id: str) -> dict[str, Any] | None:
        return next((j for j in self.public_jobs() if j["job_id"] == job_id), None)

    def get_admin_job(self, job_id: str) -> dict[str, Any] | None:
        return next((j for j in self.admin_jobs() if j["job_id"] == job_id), None)

    def prune(self) -> int:
        cutoff = iso_utc(utc_now() - timedelta(days=PUBLIC_RETENTION_DAYS))
        trend_cutoff = (utc_now().date() - timedelta(days=TREND_RETENTION_DAYS)).isoformat()
        with self.connection() as conn:
            deleted = conn.execute(
                """
                DELETE FROM jobs
                WHERE COALESCE(posted_at,first_seen) < ?
                  AND job_id NOT IN (SELECT job_id FROM applications WHERE applied=1)
                """,
                (cutoff,),
            ).rowcount
            conn.execute("DELETE FROM trend_snapshots WHERE snapshot_date < ?", (trend_cutoff,))
            return deleted


def _write_json(path: str, payload: Any) -> None:
    target = Path(path)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_results(path: str) -> Iterable[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("results file must contain a JSON list")
    return payload


def _load_job_ids(path: str) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("changed_job_ids")
    if not isinstance(payload, list):
        raise ValueError("job ID file must contain a list or a commit summary")
    return [
        str(item.get("job_id") if isinstance(item, dict) else item)
        for item in payload
        if (item.get("job_id") if isinstance(item, dict) else item)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the cover-letter generation queue")
    parser.add_argument("--profile", required=True, choices=("kk", "sandra"))
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync-csv")
    sync.add_argument("--csv", default=None)
    sync.add_argument("--legacy", action="store_true")
    sync.add_argument("--first-seen", default=None)
    prepare = sub.add_parser("prepare-batch")
    prepare.add_argument("--limit", type=int, default=40)
    prepare.add_argument("--owner", default=None)
    prepare.add_argument("--output", default="cover_letter_batch.json")
    prepare.add_argument("--min-score", type=float, default=None)
    prepare.add_argument("--eligible-only", action="store_true")
    prepare.add_argument("--manual", action="store_true", help="include manual-only jobs")
    commit = sub.add_parser("commit-batch")
    commit.add_argument("--input", default="cover_letter_results.json")
    commit.add_argument("--summary-output", default=None)
    acquire = sub.add_parser("acquire-run")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--lease-minutes", type=int, default=DEFAULT_RUN_LEASE_MINUTES)
    renew = sub.add_parser("renew-run")
    renew.add_argument("--owner", required=True)
    renew.add_argument("--lease-minutes", type=int, default=DEFAULT_RUN_LEASE_MINUTES)
    release = sub.add_parser("release-run")
    release.add_argument("--owner", required=True)
    renew_claims = sub.add_parser("renew-claims")
    renew_claims.add_argument("--owner", required=True)
    renew_claims.add_argument("--lease-minutes", type=int, default=DEFAULT_RUN_LEASE_MINUTES)
    release_claims = sub.add_parser("release-claims")
    release_claims.add_argument("--owner", required=True)
    release_claims.add_argument("--reason", default="Nightly run stopped before this chunk.")
    sub.add_parser("mark-existing-manual")
    pull = sub.add_parser("pull-cloud")
    pull.add_argument("--url", default=None)
    pull.add_argument("--token", default=None)
    push = sub.add_parser("push-cloud")
    push.add_argument("--url", default=None)
    push.add_argument("--token", default=None)
    push.add_argument("--mode", choices=("delta", "full"), default="full")
    push.add_argument("--job-id", action="append", default=[])
    push.add_argument("--job-ids-file", default=None)
    publish = sub.add_parser("publish-pending")
    publish.add_argument("--owner", default=None)
    publish.add_argument("--limit", type=int, default=PUBLICATION_BATCH_SIZE)
    publish.add_argument("--url", default=None)
    publish.add_argument("--token", default=None)
    cloud = sub.add_parser("sync-cloud")
    cloud.add_argument("--url", default=None)
    cloud.add_argument("--token", default=None)
    sub.add_parser("prune")
    args = parser.parse_args()
    from profile_workspace import ProfileWorkspace

    workspace = ProfileWorkspace.load(args.profile)
    try:
        from dotenv import load_dotenv
        load_dotenv(workspace.private_path(".env"), override=True)
    except ImportError:
        pass
    db_path = args.db or str(workspace.output_path("cover_letters.db"))
    store = CoverLetterStore(db_path, args.profile)

    if args.command == "sync-csv":
        csv_path = args.csv or str(workspace.output_path("daily_jobs.csv"))
        print(json.dumps({"enqueued": store.sync_csv(
            csv_path,
            auto_letter_eligible=not args.legacy,
            first_seen=args.first_seen,
        )}))
    elif args.command == "prepare-batch":
        limit = min(max(1, args.limit), 40)
        min_score = args.min_score if args.manual else (8 if args.min_score is None else args.min_score)
        eligible_only = False if args.manual else True
        batch = store.claim_batch(
            limit,
            owner=args.owner,
            min_score=min_score,
            eligible_only=eligible_only or args.eligible_only,
        )
        output = args.output
        if not os.path.isabs(output):
            output = str(workspace.output_path(output))
        _write_json(output, batch)
        print(json.dumps({"claimed": len(batch), "output": output}))
    elif args.command == "commit-batch":
        summary = store.commit_batch_results(_load_results(args.input))
        if args.summary_output:
            _write_json(args.summary_output, summary)
        print(json.dumps(summary))
    elif args.command == "acquire-run":
        acquired = store.acquire_run_lease(args.owner, args.lease_minutes)
        print(json.dumps({"acquired": acquired}))
        return 0 if acquired else 75
    elif args.command == "renew-run":
        renewed = store.renew_run_lease(args.owner, args.lease_minutes)
        print(json.dumps({"renewed": renewed}))
        return 0 if renewed else 75
    elif args.command == "release-run":
        print(json.dumps({"released": store.release_run_lease(args.owner)}))
    elif args.command == "renew-claims":
        print(json.dumps({"renewed": store.renew_claims(args.owner, args.lease_minutes)}))
    elif args.command == "release-claims":
        print(json.dumps({"released": store.release_claims(args.owner, args.reason)}))
    elif args.command == "mark-existing-manual":
        print(json.dumps({"marked_manual": store.mark_existing_jobs_manual()}))
    elif args.command == "pull-cloud":
        print(json.dumps(store.pull_cloud(
            args.url or os.getenv("COVER_LETTER_SYNC_URL", ""),
            args.token or os.getenv("COVER_LETTER_SYNC_TOKEN", ""),
        )))
    elif args.command == "push-cloud":
        job_ids = list(args.job_id)
        if args.job_ids_file:
            job_ids.extend(_load_job_ids(args.job_ids_file))
        print(json.dumps(store.push_cloud(
            args.url or os.getenv("COVER_LETTER_SYNC_URL", ""),
            args.token or os.getenv("COVER_LETTER_SYNC_TOKEN", ""),
            mode=args.mode,
            job_ids=job_ids if args.mode == "delta" else None,
        )))
    elif args.command == "publish-pending":
        owner = args.owner or f"publication-{args.profile}-{os.getpid()}"
        result = store.publish_pending_cloud(
            args.url or os.getenv("COVER_LETTER_SYNC_URL", ""),
            args.token or os.getenv("COVER_LETTER_SYNC_TOKEN", ""),
            owner,
            args.limit,
        )
        print(json.dumps(result))
        if result["escalated"]:
            return 1
        # A retryable failed batch is deliberately quiet for scheduling, but
        # callers still receive a distinct status and can preserve the outbox.
        return 75 if result["failure"] else 0
    elif args.command == "sync-cloud":
        print(json.dumps(store.sync_cloud(
            args.url or os.getenv("COVER_LETTER_SYNC_URL", ""),
            args.token or os.getenv("COVER_LETTER_SYNC_TOKEN", ""),
        )))
    elif args.command == "prune":
        print(json.dumps({"deleted": store.prune()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
