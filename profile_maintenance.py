#!/usr/bin/env python3
"""Retention, monthly archives, and weekly health checks for profile state."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def maintain(profile: str, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    state = ROOT / "profiles" / profile / "state"
    archive = state / "archives"
    archive.mkdir(parents=True, exist_ok=True)
    report = {"profile": profile}

    fields, rows = read_rows(state / "daily_jobs_nonmatch.csv")
    if fields and len(rows) > 2000:
        write_rows(state / "daily_jobs_nonmatch.csv", fields, rows[-2000:])
    report["nonmatches"] = min(len(rows), 2000)

    fields, rows = read_rows(state / "rejected_jobs.csv")
    cutoff = now - timedelta(days=30)
    retained = []
    for row in rows:
        try:
            seen = datetime.fromisoformat(str(row.get("first_seen") or "")).astimezone()
        except ValueError:
            retained.append(row)
            continue
        if seen >= cutoff:
            retained.append(row)
    if fields:
        write_rows(state / "rejected_jobs.csv", fields, retained)
    report["rejected"] = len(retained)

    marker = state / "archive_state.json"
    previous_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    marker_data = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    source = state / "daily_jobs.csv"
    if now.day <= 7 and marker_data.get("last_archived") != previous_month and source.exists():
        destination = archive / f"daily_jobs_{previous_month}.csv"
        destination.write_bytes(source.read_bytes())
        fields, _ = read_rows(source)
        if fields:
            write_rows(source, fields, [])
        marker.write_text(json.dumps({"last_archived": previous_month}, indent=2), encoding="utf-8")
        report["archive"] = str(destination)
    return report


def audit(profile: str, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    _, rows = read_rows(ROOT / "profiles" / profile / "state" / "daily_summary_log.csv")
    cutoff = now - timedelta(days=7)
    recent = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row.get("timestamp") or "")).astimezone()
        except ValueError:
            continue
        if stamp >= cutoff:
            recent.append(row)
    details = " ".join(str(row.get("details") or "").lower() for row in recent)
    issues = []
    if len(recent) < 5:
        issues.append(f"only {len(recent)} summary records in the last seven days")
    for marker in ("error", "throttl", "incomplete", "captcha"):
        if marker in details:
            issues.append(f"summary contains {marker}")
    return {"profile": profile, "records": len(recent), "issues": issues}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("maintain", "audit"))
    parser.add_argument("--profile", choices=("kk", "sandra", "all"), default="all")
    args = parser.parse_args()
    profiles = ("kk", "sandra") if args.profile == "all" else (args.profile,)
    reports = [maintain(profile) if args.command == "maintain" else audit(profile) for profile in profiles]
    print(json.dumps(reports, indent=2))
    return 1 if args.command == "audit" and any(report["issues"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
