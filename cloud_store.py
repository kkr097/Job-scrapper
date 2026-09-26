"""PostgreSQL storage used by the deployed cover-letter website."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterable

from cover_letters import (
    ACTIVE_DAYS,
    EVIDENCE_VERSION,
    PROMPT_VERSION,
    PUBLIC_RETENTION_DAYS,
    TREND_RETENTION_DAYS,
    TREND_WINDOW_DAYS,
    description_hash,
    iso_utc,
    job_id_for_url,
    make_public_sample,
    normalize_url,
    parse_datetime,
    public_sort_order,
    prepare_trend_snapshot,
    utc_now,
    validate_cover_letter,
)


VALID_STATUSES = {"queued", "generating", "ready", "unavailable"}


def prepare_sync_record(raw: dict[str, Any], profile_id: str = "kk") -> dict[str, Any]:
    """Validate a local record and derive every public field server-side."""
    profile_id = (profile_id or "").strip().lower()
    if profile_id not in {"kk", "sandra"}:
        raise ValueError("unsupported profile")
    supplied_profile = str(raw.get("profile_id") or profile_id).strip().lower()
    if supplied_profile != profile_id:
        raise ValueError("record profile does not match authenticated profile")
    url = normalize_url(str(raw.get("url") or ""))
    if not url:
        raise ValueError("job URL is required")
    job_id = job_id_for_url(url, profile_id)
    supplied_id = str(raw.get("job_id") or "")
    if supplied_id and supplied_id != job_id:
        raise ValueError("job ID does not match URL")

    description = str(raw.get("description") or "").strip()
    status = str(raw.get("status") or "unavailable").lower()
    if status not in VALID_STATUSES:
        raise ValueError("invalid cover-letter status")
    language = str(raw.get("language") or "").lower() or None
    if language not in {None, "de", "en"}:
        raise ValueError("invalid cover-letter language")

    full_text = str(raw.get("full_text") or "").strip() or None
    public_text = None
    if status == "ready":
        if not full_text:
            raise ValueError("ready cover letter requires full text")
        validate_cover_letter(full_text)
        public_text = make_public_sample(full_text, profile_id)
    else:
        full_text = None
        language = None

    try:
        score = float(raw["score"]) if raw.get("score") not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid job score") from exc

    now = utc_now()
    first_seen = iso_utc(parse_datetime(str(raw.get("first_seen") or ""), now))
    last_seen = iso_utc(parse_datetime(str(raw.get("last_seen") or ""), now))
    posted_raw = str(raw.get("posted_at") or "").strip()
    generated_raw = str(raw.get("generated_at") or "").strip()
    return {
        "profile_id": profile_id,
        "job_id": job_id,
        "url": url,
        "title": str(raw.get("title") or "Untitled job")[:500],
        "company": str(raw.get("company") or "")[:500],
        "location": str(raw.get("location") or "")[:500],
        "source": str(raw.get("source") or "")[:100],
        "score": score,
        "description": description,
        "description_hash": description_hash(description),
        "posted_at": iso_utc(parse_datetime(posted_raw)) if posted_raw else None,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "evidence_version": str(raw.get("evidence_version") or EVIDENCE_VERSION)[:100],
        "prompt_version": str(raw.get("prompt_version") or PROMPT_VERSION)[:100],
        "language": language,
        "status": status,
        "full_text": full_text,
        "public_text": public_text,
        "generated_at": iso_utc(parse_datetime(generated_raw)) if generated_raw else (iso_utc(now) if status == "ready" else None),
        "attempts": max(0, int(raw.get("attempts") or 0)),
        "error": str(raw.get("error") or "")[:500] or None,
    }


class PostgresCoverLetterStore:
    """Cloud store with the same website-facing contract as CoverLetterStore."""

    def __init__(self, dsn: str, profile_id: str = "kk"):
        if not dsn:
            raise ValueError("DATABASE_URL is required")
        self.dsn = dsn
        self.profile_id = (profile_id or "").strip().lower()
        if self.profile_id not in {"kk", "sandra"}:
            raise ValueError("unsupported profile")
        self._initialize()

    @contextmanager
    def connection(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary] for PostgreSQL support") from exc
        with psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            connect_timeout=15,
            prepare_threshold=None,
        ) as conn:
            yield conn

    def _initialize(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    public_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_order INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO profiles(profile_id,display_name,public_enabled,sort_order)
                VALUES ('kk','KK',TRUE,1),('sandra','Sandra',TRUE,2)
                ON CONFLICT(profile_id) DO NOTHING
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL DEFAULT 'kk' REFERENCES profiles(profile_id),
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    score DOUBLE PRECISION,
                    description TEXT NOT NULL DEFAULT '',
                    description_hash TEXT NOT NULL,
                    posted_at TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                )
                """
            )
            conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS profile_id TEXT NOT NULL DEFAULT 'kk'")
            conn.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_url_key")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cloud_jobs_profile_url ON jobs(profile_id,url)")
            conn.execute(
                """
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
                    regeneration_requested_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )
                """
            )
            conn.execute(
                "ALTER TABLE cover_letters ADD COLUMN IF NOT EXISTS regeneration_requested_at TEXT"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    applied INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trend_snapshots (
                    profile_id TEXT NOT NULL DEFAULT 'kk' REFERENCES profiles(profile_id),
                    snapshot_date TEXT NOT NULL,
                    active_14d_count INTEGER NOT NULL CHECK(active_14d_count >= 0),
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id,snapshot_date)
                )
                """
            )
            conn.execute("ALTER TABLE trend_snapshots ADD COLUMN IF NOT EXISTS profile_id TEXT NOT NULL DEFAULT 'kk'")
            conn.execute(
                """
                DO $$ BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid='trend_snapshots'::regclass AND contype='p'
                          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (snapshot_date)'
                    ) THEN
                        ALTER TABLE trend_snapshots DROP CONSTRAINT trend_snapshots_pkey;
                    END IF;
                END $$
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trend_profile_date ON trend_snapshots(profile_id,snapshot_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_jobs_first_seen ON jobs(first_seen)")

    def sync_jobs(self, records: Iterable[dict[str, Any]]) -> int:
        prepared = [prepare_sync_record(record, self.profile_id) for record in records]
        with self.connection() as conn:
            for item in prepared:
                conn.execute(
                    """
                    INSERT INTO jobs(job_id,profile_id,url,title,company,location,source,score,description,
                                     description_hash,posted_at,first_seen,last_seen)
                    VALUES(%(job_id)s,%(profile_id)s,%(url)s,%(title)s,%(company)s,%(location)s,%(source)s,
                           %(score)s,%(description)s,%(description_hash)s,%(posted_at)s,
                           %(first_seen)s,%(last_seen)s)
                    ON CONFLICT(job_id) DO UPDATE SET
                        url=excluded.url,title=excluded.title,company=excluded.company,
                        location=excluded.location,source=excluded.source,score=excluded.score,
                        description=excluded.description,description_hash=excluded.description_hash,
                        posted_at=COALESCE(excluded.posted_at,jobs.posted_at),last_seen=excluded.last_seen
                    """,
                    item,
                )
                conn.execute(
                    """
                    INSERT INTO cover_letters(job_id,description_hash,evidence_version,prompt_version,
                                              language,status,full_text,public_text,generated_at,
                                              attempts,error,regeneration_requested_at)
                    VALUES(%(job_id)s,%(description_hash)s,%(evidence_version)s,%(prompt_version)s,
                           %(language)s,%(status)s,%(full_text)s,%(public_text)s,%(generated_at)s,
                           %(attempts)s,%(error)s,NULL)
                    ON CONFLICT(job_id) DO UPDATE SET
                        description_hash=excluded.description_hash,
                        evidence_version=excluded.evidence_version,
                        prompt_version=excluded.prompt_version,
                        language=excluded.language,status=excluded.status,
                        full_text=excluded.full_text,public_text=excluded.public_text,
                        generated_at=excluded.generated_at,attempts=excluded.attempts,error=excluded.error,
                        regeneration_requested_at=CASE
                            WHEN excluded.status='ready' AND (
                                cover_letters.regeneration_requested_at IS NULL OR
                                excluded.generated_at >= cover_letters.regeneration_requested_at
                            ) THEN NULL
                            ELSE cover_letters.regeneration_requested_at END
                    """,
                    item,
                )
        self.prune()
        return len(prepared)

    def public_jobs(
        self,
        view: str = "all",
        min_score: float | None = None,
        primary_sort: str = "date_desc",
        secondary_sort: str | None = "score_desc",
    ) -> list[dict[str, Any]]:
        order_by = public_sort_order(primary_sort, secondary_sort)
        cutoff = iso_utc(utc_now() - timedelta(days=PUBLIC_RETENTION_DAYS))
        active_cutoff = iso_utc(utc_now() - timedelta(days=ACTIVE_DAYS))
        conditions = ["j.profile_id = %s", "COALESCE(j.posted_at,j.first_seen) >= %s"]
        params: list[Any] = [self.profile_id, cutoff]
        if view == "active":
            conditions.append("COALESCE(j.posted_at,j.first_seen) >= %s")
            params.append(active_cutoff)
        elif view == "expired":
            conditions.append("COALESCE(j.posted_at,j.first_seen) < %s")
            params.append(active_cutoff)
        if min_score is not None:
            conditions.append("j.score >= %s")
            params.append(min_score)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT j.job_id,j.url,j.title,j.company,j.location,j.source,j.score,
                       j.posted_at,j.first_seen,c.status,c.public_text,c.language,c.generated_at,
                       CASE WHEN COALESCE(j.posted_at,j.first_seen) >= %s THEN 'active' ELSE 'expired' END AS age_status
                FROM jobs j JOIN cover_letters c USING(job_id)
                WHERE {' AND '.join(conditions)}
                ORDER BY {order_by}
                """,
                (active_cutoff, *params),
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_jobs(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT j.*,c.status,c.full_text,c.public_text,c.language,c.generated_at,c.error,c.attempts,
                       COALESCE(a.applied,0) AS applied,a.applied_at,a.notes
                FROM jobs j JOIN cover_letters c USING(job_id)
                LEFT JOIN applications a USING(job_id)
                WHERE j.profile_id=%s
                ORDER BY j.first_seen DESC
                """,
                (self.profile_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
                    INSERT INTO trend_snapshots(profile_id,snapshot_date,active_14d_count,recorded_at)
                    VALUES(%s,%s,%s,%s)
                    ON CONFLICT(profile_id,snapshot_date) DO UPDATE SET
                        active_14d_count=excluded.active_14d_count,
                        recorded_at=excluded.recorded_at
                    """,
                    (self.profile_id, point["snapshot_date"], point["active_14d_count"], point["recorded_at"]),
                )
            conn.execute("DELETE FROM trend_snapshots WHERE profile_id=%s AND snapshot_date < %s", (self.profile_id, cutoff))
        return len(prepared)

    def trend_snapshots(self, days: int = TREND_WINDOW_DAYS) -> list[dict[str, Any]]:
        cutoff = (utc_now().date() - timedelta(days=max(1, days) - 1)).isoformat()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT snapshot_date,active_14d_count,recorded_at
                FROM trend_snapshots WHERE profile_id=%s AND snapshot_date >= %s ORDER BY snapshot_date
                """,
                (self.profile_id, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_public_job(self, job_id: str) -> dict[str, Any] | None:
        return next((job for job in self.public_jobs() if job["job_id"] == job_id), None)

    def get_admin_job(self, job_id: str) -> dict[str, Any] | None:
        return next((job for job in self.admin_jobs() if job["job_id"] == job_id), None)

    def set_application(self, job_id: str, applied: bool, notes: str = "") -> None:
        now = iso_utc()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO applications(job_id,applied,applied_at,notes,updated_at)
                VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(job_id) DO UPDATE SET applied=excluded.applied,
                    applied_at=excluded.applied_at,notes=excluded.notes,updated_at=excluded.updated_at
                """,
                (job_id, int(applied), now if applied else None, notes[:4000], now),
            )

    def regenerate(self, job_id: str) -> bool:
        now = iso_utc()
        with self.connection() as conn:
            changed = conn.execute(
                """
                UPDATE cover_letters SET status='queued',full_text=NULL,public_text=NULL,
                    generated_at=NULL,attempts=0,error=NULL,regeneration_requested_at=%s
                WHERE job_id=%s
                """,
                (now, job_id),
            ).rowcount
        return changed == 1

    def cloud_state(self) -> dict[str, list[dict[str, Any]]]:
        with self.connection() as conn:
            applications = conn.execute(
                """SELECT a.job_id,a.applied,a.applied_at,a.notes,a.updated_at
                   FROM applications a JOIN jobs j USING(job_id)
                   WHERE j.profile_id=%s ORDER BY a.updated_at""",
                (self.profile_id,),
            ).fetchall()
            requests = conn.execute(
                """
                SELECT job_id,regeneration_requested_at
                FROM cover_letters c JOIN jobs j USING(job_id)
                WHERE j.profile_id=%s AND regeneration_requested_at IS NOT NULL
                ORDER BY regeneration_requested_at
                """,
                (self.profile_id,),
            ).fetchall()
        return {
            "applications": [dict(row) for row in applications],
            "regeneration_requests": [dict(row) for row in requests],
            "trend_snapshots": self.trend_snapshots(TREND_RETENTION_DAYS),
        }

    def prune(self) -> int:
        cutoff = iso_utc(utc_now() - timedelta(days=PUBLIC_RETENTION_DAYS))
        trend_cutoff = (utc_now().date() - timedelta(days=TREND_RETENTION_DAYS)).isoformat()
        with self.connection() as conn:
            deleted = conn.execute(
                """
                DELETE FROM jobs
                WHERE profile_id=%s AND COALESCE(posted_at,first_seen) < %s
                  AND job_id NOT IN (SELECT job_id FROM applications WHERE applied=1)
                """,
                (self.profile_id, cutoff),
            ).rowcount
            conn.execute("DELETE FROM trend_snapshots WHERE profile_id=%s AND snapshot_date < %s", (self.profile_id, trend_cutoff))
            return deleted
