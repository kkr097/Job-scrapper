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


def kk_has_blocking_signal() -> bool:
    path = ROOT / "profiles" / "kk" / "state" / "daily_summary_log.csv"
    if not path.exists():
        return True
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    latest = lines[-1].lower() if lines else ""
    return any(marker in latest for marker in BLOCKING_MARKERS)


def default_run(profile: str, phase: str) -> int:
    flag = "--scrape-only" if phase == "scrape" else "--score-only-manual"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--profile", profile, flag],
        cwd=ROOT,
        check=False,
    )
    return completed.returncode


def orchestrate(
    run_phase: Callable[[str, str], int] = default_run,
    blocking_signal: Callable[[], bool] = kk_has_blocking_signal,
) -> dict[str, str]:
    result = {"kk": "pending", "sandra": "pending"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        scrapes = {profile: pool.submit(run_phase, profile, "scrape") for profile in ("kk", "sandra")}
        kk_scrape = scrapes["kk"].result()
        if kk_scrape != 0 or blocking_signal():
            scrapes["sandra"].cancel()
            result["kk"] = "blocked"
            result["sandra"] = "paused"
            return result
        sandra_scrape = scrapes["sandra"].result()

    if run_phase("kk", "score") != 0:
        result["kk"] = "failed"
        result["sandra"] = "paused"
        return result
    result["kk"] = "success"
    if sandra_scrape != 0:
        result["sandra"] = "failed"
        return result
    result["sandra"] = "success" if run_phase("sandra", "score") == 0 else "failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("kk", "sandra", "all"), required=True)
    args = parser.parse_args()
    if args.profile == "all":
        result = orchestrate()
        print(result)
        return 0 if result["kk"] == "success" else 1
    for phase in ("scrape", "score"):
        if default_run(args.profile, phase) != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
