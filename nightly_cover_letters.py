#!/usr/bin/env python3
"""Quota gate, shared lease, and immutable manifests for nightly drafting."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cover_letters import PROFILE_VERSIONS, iso_utc


try:
    BERLIN = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    # Windows installations without the optional tzdata wheel still expose the
    # configured Europe/Berlin offset through the system local timezone.
    BERLIN = datetime.now().astimezone().tzinfo or timezone.utc
START_THRESHOLD = 90.0
CONTINUE_THRESHOLD = 20.0
SCHEDULED_CUTOFF = time(6, 30)
MAX_JOBS_PER_PROFILE = 40
CHUNK_SIZE = 10
PROFILE_ORDER = ("kk", "sandra")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BERLIN)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _night_key(value: datetime) -> str:
    local = value.astimezone(BERLIN)
    return (local.date() - timedelta(days=1) if local.hour < 12 else local.date()).isoformat()


class NightlyCoordinator:
    """Own the single nightly run lease across both candidate profiles."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nightly_runs (
                    night_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    owner TEXT,
                    lease_until TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start(
        self,
        owner: str,
        remaining_percent: float | None,
        *,
        now: datetime | None = None,
        forced: bool = False,
        lease_minutes: int = 90,
    ) -> dict[str, Any]:
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("run owner is required")
        if lease_minutes < 1:
            raise ValueError("lease duration must be positive")
        if not forced and remaining_percent is None:
            return {"started": False, "reason": "usage_unavailable"}
        if not forced and float(remaining_percent) < START_THRESHOLD:
            return {"started": False, "reason": "insufficient_allowance"}

        current = _as_utc(now or datetime.now(BERLIN))
        key = _night_key(current)
        current_iso = iso_utc(current)
        lease_until = iso_utc(current + timedelta(minutes=lease_minutes))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,owner,lease_until FROM nightly_runs WHERE night_key=?", (key,)
            ).fetchone()
            if row and row["status"] == "completed":
                return {"started": False, "reason": "already_completed", "night_key": key}
            if row and row["status"] == "running" and row["lease_until"] and row["lease_until"] > current_iso:
                return {"started": False, "reason": "run_active", "night_key": key}
            conn.execute(
                """
                INSERT INTO nightly_runs(night_key,status,owner,lease_until,started_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(night_key) DO UPDATE SET
                    status='running',owner=excluded.owner,lease_until=excluded.lease_until,
                    started_at=excluded.started_at,updated_at=excluded.updated_at
                """,
                (key, "running", owner, lease_until, current_iso, current_iso),
            )
        return {"started": True, "reason": "started", "night_key": key, "owner": owner}

    def can_continue(
        self,
        owner: str,
        remaining_percent: float | None,
        *,
        now: datetime | None = None,
        forced: bool = False,
        scheduled: bool = True,
        lease_minutes: int = 90,
    ) -> dict[str, Any]:
        current_local = (now or datetime.now(BERLIN)).astimezone(BERLIN)
        key = _night_key(current_local)
        if scheduled and current_local.hour < 12 and current_local.time().replace(tzinfo=None) > SCHEDULED_CUTOFF:
            return {"continue": False, "reason": "scheduled_cutoff", "night_key": key}
        if not forced and remaining_percent is None:
            return {"continue": False, "reason": "usage_unavailable", "night_key": key}
        if not forced and float(remaining_percent) < CONTINUE_THRESHOLD:
            return {
                "continue": False,
                "reason": "allowance_below_continuation_threshold",
                "night_key": key,
            }

        current = _as_utc(current_local)
        lease_until = iso_utc(current + timedelta(minutes=lease_minutes))
        with self.connection() as conn:
            changed = conn.execute(
                """
                UPDATE nightly_runs SET lease_until=?,updated_at=?
                WHERE night_key=? AND status='running' AND owner=?
                """,
                (lease_until, iso_utc(current), key, owner),
            ).rowcount
        if changed != 1:
            return {"continue": False, "reason": "lease_not_owned", "night_key": key}
        return {"continue": True, "reason": "continue", "night_key": key}

    def finish(
        self,
        owner: str,
        status: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "partial", "failed"}:
            raise ValueError("invalid final run status")
        current = _as_utc(now or datetime.now(BERLIN))
        key = _night_key(current)
        with self.connection() as conn:
            changed = conn.execute(
                """
                UPDATE nightly_runs SET status=?,owner=NULL,lease_until=NULL,updated_at=?
                WHERE night_key=? AND status='running' AND owner=?
                """,
                (status, iso_utc(current), key, owner),
            ).rowcount
        return {"finished": changed == 1, "status": status, "night_key": key}


def build_generation_manifest(
    profile_id: str,
    jobs: Iterable[dict[str, Any]],
    candidate_facts: dict[str, Any],
) -> dict[str, Any]:
    if profile_id not in PROFILE_ORDER:
        raise ValueError("unknown profile")
    prepared = [dict(job) for job in jobs]
    if len(prepared) > MAX_JOBS_PER_PROFILE:
        raise ValueError("a profile manifest may contain at most 40 jobs")
    if any(job.get("profile_id") not in {None, profile_id} for job in prepared):
        raise ValueError("job profile does not match manifest profile")
    evidence_version, prompt_version = PROFILE_VERSIONS[profile_id]
    return {
        "profile_id": profile_id,
        "generation_context": {
            "evidence_version": evidence_version,
            "prompt_version": prompt_version,
            "candidate_facts": candidate_facts,
            "target_words": [300, 350],
            "accepted_words": [285, 375],
            "maximum_body_paragraphs": 4,
            "maximum_repairs_per_job": 1,
        },
        "chunks": [prepared[index:index + CHUNK_SIZE] for index in range(0, len(prepared), CHUNK_SIZE)],
    }


def _remaining(value: str | None) -> float | None:
    if value is None or value.lower() in {"unknown", "unavailable", "none"}:
        return None
    return float(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Coordinate the shared nightly cover-letter run")
    parser.add_argument("--state-db", default="runtime/nightly_cover_letters.db")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--owner", required=True)
    start.add_argument("--remaining-percent", default="unavailable")
    start.add_argument("--force", action="store_true")
    check = sub.add_parser("continue")
    check.add_argument("--owner", required=True)
    check.add_argument("--remaining-percent", default="unavailable")
    check.add_argument("--force", action="store_true")
    check.add_argument("--manual", action="store_true")
    finish = sub.add_parser("finish")
    finish.add_argument("--owner", required=True)
    finish.add_argument("--status", required=True, choices=("completed", "partial", "failed"))
    args = parser.parse_args()
    coordinator = NightlyCoordinator(args.state_db)
    if args.command == "start":
        result = coordinator.start(
            args.owner, _remaining(args.remaining_percent), forced=args.force
        )
        print(json.dumps(result))
        return 0 if result["started"] else 75
    if args.command == "continue":
        result = coordinator.can_continue(
            args.owner,
            _remaining(args.remaining_percent),
            forced=args.force,
            scheduled=not args.manual,
        )
        print(json.dumps(result))
        return 0 if result["continue"] else 75
    result = coordinator.finish(args.owner, args.status)
    print(json.dumps(result))
    return 0 if result["finished"] else 75


if __name__ == "__main__":
    raise SystemExit(main())
