#!/usr/bin/env python3
"""One-way, verified migration into ignored MatchAtlas profile workspaces."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cover_letters import CoverLetterStore


SANDRA_SOURCE = Path(r"D:\Job Scraper\Sandra-Job-Scraper")
BACKUP = ROOT / "migration-backup-20260926-multiprofile"
STATE_FILES = (
    "daily_jobs.csv", "daily_jobs_nonmatch.csv", "score_pending_jobs.csv",
    "rejected_jobs.csv", "daily_summary_log.csv", "failed_requests_log.csv",
    "zero_results_log.csv", "seen_jobs_cache.json", "scoring_daily_log.json",
    "cover_letters.db",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_count(path: Path) -> int:
    if not path.exists() or path.suffix.lower() != ".csv":
        return 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return sum(1 for _ in csv.DictReader(stream))


def copy_if_present(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def migrate_private(profile: str, source: Path) -> None:
    private = ROOT / "profiles" / profile / "private"
    private.mkdir(parents=True, exist_ok=True)
    for name in ("candidate_profile.json", ".env"):
        copy_if_present(source / name, private / name)
    config = json.loads((source / "config.json").read_text(encoding="utf-8-sig"))
    config["profile_id"] = profile
    (private / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if profile == "sandra":
        copy_if_present(source / "Sandra_S_Menon_Resume.pdf", private / "Sandra_S_Menon_Resume.pdf")


def migrate_state(profile: str, source: Path) -> dict:
    state = ROOT / "profiles" / profile / "state"
    state.mkdir(parents=True, exist_ok=True)
    report = {}
    for name in STATE_FILES:
        src, dst = source / name, state / name
        copy_if_present(src, dst)
        if src.exists() and dst.exists():
            report[name] = {
                "source_sha256": sha256(src), "destination_sha256": sha256(dst),
                "source_rows": row_count(src), "destination_rows": row_count(dst),
            }
            if report[name]["source_sha256"] != report[name]["destination_sha256"]:
                raise RuntimeError(f"copy verification failed: {profile}/{name}")
    return report


def retain_newest_sandra_nonmatches() -> dict:
    path = ROOT / "profiles" / "sandra" / "state" / "daily_jobs_nonmatch.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames, rows = reader.fieldnames, list(reader)
    retained = rows[-2000:]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(retained)
    return {"before": len(rows), "after": len(retained)}


def main() -> int:
    if not BACKUP.is_dir():
        raise RuntimeError(f"verified backup directory is required: {BACKUP}")
    migrate_private("kk", ROOT)
    migrate_private("sandra", SANDRA_SOURCE)
    report = {
        "kk": migrate_state("kk", ROOT),
        "sandra": migrate_state("sandra", SANDRA_SOURCE),
    }
    report["sandra_nonmatch_retention"] = retain_newest_sandra_nonmatches()
    sandra_store = CoverLetterStore(
        ROOT / "profiles" / "sandra" / "state" / "cover_letters.db", "sandra"
    )
    report["sandra_legacy_imported"] = sandra_store.sync_csv(
        ROOT / "profiles" / "sandra" / "state" / "daily_jobs.csv",
        auto_letter_eligible=False,
        first_seen="2026-09-26T00:00:00+00:00",
    )
    with sandra_store.connection() as conn:
        report["sandra_db_jobs"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        report["sandra_auto_eligible"] = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE auto_letter_eligible=1"
        ).fetchone()[0]
    report_path = BACKUP / "migration-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
