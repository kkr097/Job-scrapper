#!/usr/bin/env python3
"""Priority-aware profile orchestration for MatchAtlas."""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
BLOCKING_MARKERS = ("throttl", "incomplete", "blocked", "source_error", "captcha")
RETRYABLE_PUBLICATION_EXIT = 75


def kk_has_blocking_signal() -> bool:
    path = ROOT / "profiles" / "kk" / "state" / "daily_summary_log.csv"
    if not path.exists():
        return True
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    latest = lines[-1].lower() if lines else ""
    return any(marker in latest for marker in BLOCKING_MARKERS)


def default_run(profile: str, phase: str) -> int:
    if phase == "publish":
        command = [
            sys.executable,
            str(ROOT / "cover_letters.py"),
            "--profile",
            profile,
            "publish-pending",
        ]
    else:
        flag = "--scrape-only" if phase == "scrape" else "--score-only-manual"
        command = [sys.executable, str(ROOT / "main.py"), "--profile", profile, flag]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
    )
    return completed.returncode


def publication_status(exit_code: int) -> str:
    if exit_code == 0:
        return "published"
    if exit_code == RETRYABLE_PUBLICATION_EXIT:
        return "pending"
    return "escalated"


def orchestrate(
    run_phase: Callable[[str, str], int] = default_run,
    blocking_signal: Callable[[], bool] = kk_has_blocking_signal,
) -> dict[str, str]:
    result = {
        "kk": "pending",
        "sandra": "pending",
        "kk_publication": "pending",
        "sandra_publication": "pending",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        scrapes = {profile: pool.submit(run_phase, profile, "scrape") for profile in ("kk", "sandra")}
        kk_scrape = scrapes["kk"].result()
        if kk_scrape != 0 or blocking_signal():
            scrapes["sandra"].cancel()
            result["kk"] = "blocked"
            result["sandra"] = "paused"
            result["kk_publication"] = "skipped"
            result["sandra_publication"] = "skipped"
            return result
        sandra_scrape = scrapes["sandra"].result()

    if run_phase("kk", "score") != 0:
        result["kk"] = "failed"
        result["sandra"] = "paused"
        result["kk_publication"] = "skipped"
        result["sandra_publication"] = "skipped"
        return result
    result["kk"] = "success"
    result["kk_publication"] = publication_status(run_phase("kk", "publish"))
    if sandra_scrape != 0:
        result["sandra"] = "failed"
        result["sandra_publication"] = "skipped"
        return result
    if run_phase("sandra", "score") != 0:
        result["sandra"] = "failed"
        result["sandra_publication"] = "skipped"
        return result
    result["sandra"] = "success"
    result["sandra_publication"] = publication_status(run_phase("sandra", "publish"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("kk", "sandra", "all"), required=True)
    args = parser.parse_args()
    if args.profile == "all":
        result = orchestrate()
        print(result)
        publication_escalated = any(
            result[key] == "escalated" for key in ("kk_publication", "sandra_publication")
        )
        return 0 if result["kk"] == "success" and not publication_escalated else 1
    for phase in ("scrape", "score", "publish"):
        exit_code = default_run(args.profile, phase)
        if phase == "publish" and exit_code == RETRYABLE_PUBLICATION_EXIT:
            return 0
        if exit_code != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
