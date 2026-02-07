#!/usr/bin/env python3
"""
Job Scraper - Main Runner
Finds jobs matching your resume, rates them 1-10, saves to Excel.

Usage:
    python main.py              # Run with AI rating (needs API key)
    python main.py --no-rate    # Run without AI rating (test mode)
"""

import os
import sys
import json
import csv
import argparse
import time
from datetime import datetime
from dotenv import load_dotenv

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import JobScraper
from exporter import append_new_to_excel, append_new_to_csv, load_existing_urls, load_cache, save_cache, prune_cache
from resume_parser import get_resume_summary, generate_config_from_resume, parse_resume


def _deep_update(base: dict, override: dict) -> dict:
    """Recursively update dict keys without replacing nested dicts."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _load_defaults() -> dict:
    defaults_path = os.path.join(os.path.dirname(__file__), 'config.defaults.json')
    if not os.path.exists(defaults_path):
        print("   âš  config.defaults.json not found. Using built-in minimal defaults.")
        return {}
    with open(defaults_path, encoding="utf-8") as f:
        return json.load(f)


def _apply_env_keys(config: dict) -> dict:
    """Override API keys from environment (.env) without writing them to config.json."""
    mapping = {
        "groq_api_key": "GROQ_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
    }
    for key, env in mapping.items():
        value = os.getenv(env)
        if value:
            config[key] = value
    return config


def load_config(force_regen: bool = False):
    """Load configuration from config.json, generate it from resume if missing."""
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    # Ensure .env values override any pre-set shell environment variables.
    load_dotenv(env_path, override=True)
    if force_regen or not os.path.exists(config_path):
        print("   âš  config.json not found. Generating from resume PDF...")
        defaults = _load_defaults()
        config = generate_config_from_resume(
            folder=os.path.dirname(__file__),
            default_locations=defaults.get("search", {}).get("location", ["Germany"]),
            exclude_keywords=defaults.get("exclude_keywords", []),
            exclude_description_keywords=defaults.get("exclude_description_keywords", []),
            include_description_keywords=defaults.get("include_description_keywords", [])
        )
        _deep_update(config, defaults)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        print(f"   âœ“ Wrote new config: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    return _apply_env_keys(config)


def _load_pending_rows(path: str) -> list:
    if not os.path.exists(path):
        print(f"   ⚠ CSV not found: {path}")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"   ⚠ CSV empty: {path}")
    return rows


def _load_pending_urls(path: str) -> set:
    urls = set()
    for row in _load_pending_rows(path):
        url = (row.get("url") or "").strip().lower()
        if url:
            urls.add(url)
    return urls


def _append_pending_row(path: str, row: dict) -> None:
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "title", "company", "source", "first_seen", "description"])
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def _remove_pending_url(path: str, url: str) -> None:
    if not os.path.exists(path):
        return
    url_l = (url or "").strip().lower()
    rows = _load_pending_rows(path)
    rows = [r for r in rows if (r.get("url") or "").strip().lower() != url_l]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "title", "company", "source", "first_seen", "description"])
        writer.writeheader()
        writer.writerows(rows)


def _within_window(start_str: str, end_str: str) -> bool:
    try:
        now = datetime.now().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        if start <= end:
            return start <= now <= end
        # window crosses midnight
        return now >= start or now <= end
    except Exception:
        return True


def _load_daily_count(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
            return int(data.get("count", 0))
    except Exception:
        return 0
    return 0


def _save_daily_count(path: str, count: int) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d"), "count": count}, f)
    except Exception:
        pass


def _score_success(job: dict) -> bool:
    reason = (job.get("match_reasons") or "").lower()
    failure_markers = [
        "api call failed",
        "rating error",
        "parse error",
        "add api key",
        "ai rating unavailable"
    ]
    if any(m in reason for m in failure_markers):
        return False
    return job.get("score") is not None


def main():
    parser = argparse.ArgumentParser(description='Job Scraper - Find jobs matching your resume')
    parser.add_argument('--no-rate', action='store_true', help='Skip AI rating (test mode)')
    parser.add_argument('--scrape-only', action='store_true', help='Scrape and queue jobs only (no scoring)')
    parser.add_argument('--score-only', action='store_true', help='Score only pending jobs (scheduled mode)')
    parser.add_argument('--score-only-manual', action='store_true', help='Score pending jobs now (no limits)')
    parser.add_argument('--output', '-o', default=None, help='Output Excel file path')
    parser.add_argument('--regen-config', action='store_true', help='Regenerate config.json from resume and defaults')
    args = parser.parse_args()
    print(f"   • Args: scrape_only={args.scrape_only}, score_only={args.score_only}, score_only_manual={args.score_only_manual}, no_rate={args.no_rate}")
    
    print("=" * 60)
    print("ðŸ” JOB SCRAPER")
    print(f"   Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    
    # Load config
    config = load_config(force_regen=args.regen_config)
    scrape_test_mode = bool(config.get("scrape_test_mode", False))
    
    # Get output path
    output_file = args.output or config['output'].get('excel_file', 'daily_jobs.xlsx')
    
    # Step 1: Load candidate profile (prefer explicit JSON, fallback to resume)
    candidate_path = os.path.join(os.path.dirname(__file__), "candidate_profile.json")
    if os.path.exists(candidate_path):
        print("\nðŸ“„ Step 1: Loading candidate_profile.json...")
        try:
            with open(candidate_path, "r", encoding="utf-8") as f:
                candidate_profile = json.load(f)
            print("   âœ“ Candidate profile loaded")
        except Exception as e:
            print(f"   ⚠ Failed to load candidate_profile.json: {e}")
            candidate_profile = None
    else:
        candidate_profile = None

    if candidate_profile is None:
        print("\nðŸ“„ Step 1: Reading resume...")
        try:
            resume_summary = get_resume_summary(os.path.dirname(__file__))
            resume_data = parse_resume(os.path.dirname(__file__))
            candidate_profile = {
                "summary": resume_summary,
                "skills": resume_data.get("skills", []),
                "years_experience": resume_data.get("years_experience", "unknown")
            }
            print("   âœ“ Resume loaded successfully")
        except Exception as e:
            print(f"   âœ— Error reading resume: {e}")
            print("   Using default skills from config...")
            resume_summary = "Mechanical Design Engineer with CATIA V5, Autodesk Inventor, automotive and railway experience."
            candidate_profile = {
                "summary": resume_summary,
                "skills": [],
                "years_experience": "unknown"
            }

    # Keywords to hard-skip jobs BEFORE any CSV writes (manual filtering guard).
    # If a job description contains any of these, we skip it entirely to avoid LLM overload.
    skip_kw = (
        candidate_profile.get("matching_rules", {}).get("negative_title_keywords", [])
        if isinstance(candidate_profile, dict) else []
    )
    skip_kw = [k.strip().lower() for k in skip_kw if isinstance(k, str) and k.strip()]

    def _desc_has_skip_keyword(job: dict) -> bool:
        desc = (job.get("description") or "").lower()
        return any(k in desc for k in skip_kw)

    def _append_rejected(job: dict) -> None:
        # Keep a FIFO list (max 100) of rejected jobs for review.
        reject_path = os.path.join(os.path.dirname(__file__), "rejected_jobs.csv")
        rows = []
        if os.path.exists(reject_path):
            with open(reject_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        rows.append({
            "url": job.get("url", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "source": job.get("source", ""),
            "first_seen": datetime.now().isoformat(timespec="seconds"),
            "description": job.get("description", "")
        })
        rows = rows[-100:]
        with open(reject_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["url", "title", "company", "location", "source", "first_seen", "description"]
            )
            writer.writeheader()
            writer.writerows(rows)
    
    print("   • Step 1 complete, moving to Step 2...", flush=True)
    try:
        with open(os.path.join(os.path.dirname(__file__), "debug_step2.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} reached-after-step1\n")
    except Exception as e:
        print(f"   ⚠ Debug log write failed: {e}", flush=True)
    # Step 2: Scrape jobs or score-only
    print("\nStep 2: Scraping job sites...", flush=True)
    try:
        with open(os.path.join(os.path.dirname(__file__), "debug_step2.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} reached-step2-print\n")
    except Exception as e:
        print(f"   ⚠ Debug log write failed: {e}", flush=True)
    scraper = JobScraper(config)

    pending_file = config.get("score_pending_file", "score_pending_jobs.csv")
    pending_path = os.path.join(os.path.dirname(__file__), pending_file)

    daily_file = args.output or config['output'].get('excel_file', 'daily_jobs.csv')
    daily_path = os.path.join(os.path.dirname(__file__), daily_file)
    nonmatch_file = config.get('output_nonmatch_file', 'daily_jobs_nonmatch.csv')
    nonmatch_path = os.path.join(os.path.dirname(__file__), nonmatch_file)

    if not scrape_test_mode:
        scored_urls = load_existing_urls(daily_path) | load_existing_urls(nonmatch_path)
        pending_urls = _load_pending_urls(pending_path)
    else:
        scored_urls = set()
        pending_urls = set()

    def _queue_job(job: dict):
        if scrape_test_mode:
            return
        # Manual filter: skip jobs whose DESCRIPTION contains any negative title keyword.
        if _desc_has_skip_keyword(job):
            _append_rejected(job)
            return
        url = (job.get("url") or "").strip()
        if not url:
            return
        url_l = url.lower()
        if url_l in scored_urls or url_l in pending_urls:
            return
        row = {
            "url": url,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "source": job.get("source", ""),
            "first_seen": datetime.now().isoformat(timespec="seconds"),
            "description": job.get("description", "")
        }
        _append_pending_row(pending_path, row)
        pending_urls.add(url_l)

    # Score-only path
    if args.score_only or args.score_only_manual:
        if scrape_test_mode:
            print("   ⚠ Scrape test mode is enabled; score-only requires CSVs. Disable scrape_test_mode.")
            return
        # Check window unless manual
        if not args.score_only_manual:
            start = config.get("scoring_window_start", "01:00")
            end = config.get("scoring_window_end", "07:00")
            if not _within_window(start, end):
                print(f"   â° Outside scoring window ({start}-{end}). Exiting.")
                return

        max_per_day = int(config.get("scoring_max_per_day", 40))
        cooldown = int(config.get("scoring_cooldown_seconds", 300))
        daily_log = config.get("scoring_daily_log", "scoring_daily_log.json")
        daily_log_path = os.path.join(os.path.dirname(__file__), daily_log)
        scored_today = 0 if args.score_only_manual else _load_daily_count(daily_log_path)

        # Prepare rater
        from rater import JobRater
        rater = JobRater(config, candidate_profile)

        if not os.path.exists(pending_path):
            print(f"   âš  Pending file not found: {pending_path}")
            print("   âš  No job links available to score.")
            return
        rows = _load_pending_rows(pending_path)
        if not rows:
            print(f"   âš  Pending file is empty: {pending_path}")
            print("   âš  No job links available to score.")
            return
        rows_with_urls = [r for r in rows if (r.get("url") or "").strip()]
        if not rows_with_urls:
            print(f"   âš  Pending file has no valid job URLs: {pending_path}")
            print("   âš  No job links available to score.")
            return
        print(f"   â€¢ Pending rows: {len(rows)}")
        skipped_scored = 0
        attempted = 0
        for row in rows:
            if not args.score_only_manual and scored_today >= max_per_day:
                print(f"   â¸ Reached daily limit ({max_per_day}).")
                break

            url = (row.get("url") or "").strip()
            if not url:
                continue
            url_l = url.lower()
            # Refresh scored URLs in case other runs updated the CSVs
            scored_urls = load_existing_urls(daily_path) | load_existing_urls(nonmatch_path)
            if url_l in scored_urls:
                _remove_pending_url(pending_path, url)
                skipped_scored += 1
                continue

            job = {
                "title": row.get("title", ""),
                "company": row.get("company", ""),
                "location": row.get("location", ""),
                "source": row.get("source", ""),
                "url": url,
                "description": row.get("description", "")
            }
            if _desc_has_skip_keyword(job):
                _append_rejected(job)
                _remove_pending_url(pending_path, url)
                continue
            attempted += 1
            rated = rater.rate_jobs([job], batch_size=1)
            job = rated[0]

            if _score_success(job):
                if job.get("score", 0) >= config.get("min_score", 5):
                    append_new_to_csv([job], daily_path)
                else:
                    append_new_to_csv([job], nonmatch_path)
                scored_urls.add(url_l)
                _remove_pending_url(pending_path, url)
                if not args.score_only_manual:
                    scored_today += 1
                    _save_daily_count(daily_log_path, scored_today)
                    time.sleep(cooldown)
            else:
                print(f"   âš  Scoring failed for: {url}")
                # keep in pending

        print(f"   â€¢ Skipped (already scored): {skipped_scored}")
        print(f"   â€¢ Attempted scoring: {attempted}")
        print("   âœ“ Score-only run complete.")
        return

    # Scrape-only path
    if args.scrape_only:
        if scrape_test_mode:
            print("   • Scrape-only test mode: scraping without CSV output")
            jobs = scraper.scrape_all(on_job=None)
            print(f"\nâœ“ Scrape-only test complete. Jobs scraped: {len(jobs)}")
        else:
            print("   • Scrape-only mode: queueing jobs to score_pending_jobs.csv")
            config["scrape_queue_only"] = True
            jobs = scraper.scrape_all(on_job=_queue_job)
            print(f"\nâœ“ Scrape-only complete. Queued: {len(pending_urls)} jobs.")
        return

    # Default: scrape + queue + score
    jobs = scraper.scrape_all(on_job=None if scrape_test_mode else _queue_job)
    if skip_kw:
        filtered = []
        for j in jobs:
            if _desc_has_skip_keyword(j):
                _append_rejected(j)
            else:
                filtered.append(j)
        jobs = filtered

    if scrape_test_mode:
        print("\nðŸ§ª Scrape test mode enabled: skipping rating and export.")
        print(f"   Total jobs scraped: {len(jobs)}")
        return
    
    if not jobs:
        print("\nâš  No jobs found. Try adjusting your search keywords in config.json")
        return
    
    # Step 3: Rate jobs with AI
    if not args.no_rate:
        # Check if any API key is configured
        has_api = any([
            config.get('deepseek_api_key', '').startswith('sk-'),
            config.get('groq_api_key', '').startswith('gsk_'),
            config.get('gemini_api_key', '').startswith('AIza')
        ])
        
        if has_api:
            print("\nðŸ¤– Step 3: Rating jobs with AI...")
            try:
                from rater import JobRater
                rater = JobRater(config, candidate_profile)
                output_file = args.output or config['output'].get('excel_file', 'daily_jobs.csv')
                nonmatch_file = config.get('output_nonmatch_file', 'daily_jobs_nonmatch.csv')
                cache_file = config.get('cache_file', 'seen_jobs_cache.json')
                cache_path = os.path.join(os.path.dirname(__file__), cache_file)
                cache = load_cache(cache_path)
                cache = prune_cache(cache, int(config.get("cache_days", 45)))

                # Only skip jobs that have already been scored at least once
                scored_urls = {
                    url for url, meta in cache.items()
                    if isinstance(meta, dict) and meta.get("score") is not None
                }
                jobs = [j for j in jobs if (j.get('url', '') or '').strip().lower() not in scored_urls]

                already_scored = [j for j in jobs if 'score' in j]
                to_rate = [j for j in jobs if 'score' not in j]
                if to_rate:
                    rated = rater.rate_jobs(to_rate)
                    jobs = already_scored + rated
                else:
                    jobs = already_scored

                # Update cache with newly scored jobs
                for j in jobs:
                    url = (j.get('url', '') or '').strip().lower()
                    if not url:
                        continue
                    cache[url] = {
                        "score": j.get("score", None),
                        "last_seen": datetime.now().isoformat(timespec="seconds"),
                        "title": j.get("title", ""),
                        "company": j.get("company", "")
                    }
                save_cache(cache_path, cache)
                print("   âœ“ All jobs rated")
            except Exception as e:
                print(f"   âš  Rating failed: {e}")
                import traceback
                traceback.print_exc()
                print("   Assigning default scores...")
                for job in jobs:
                    if 'score' not in job:
                        job['score'] = 5
                        job['match_reasons'] = 'AI rating unavailable'
                        job['missing_skills'] = ''
        else:
            print("\nâš  Step 3: No API key configured")
            print("   Add an API key to config.json (DeepSeek, Groq, or Gemini)")
            print("   Assigning default scores for now...")
            for job in jobs:
                if 'score' not in job:
                    job['score'] = 5
                    job['match_reasons'] = 'Add API key to enable rating'
                    job['missing_skills'] = ''
    else:
        append_new_to_excel(matches, output_path)
        print("\nâ­ Step 3: Skipping AI rating (--no-rate flag)")
        for job in jobs:
            if 'score' not in job:
                job['score'] = 5
                job['match_reasons'] = 'Rating skipped'
                job['missing_skills'] = ''
    
    # Filter by location (drop obvious non-matching locations, e.g., USA)
    locations = config.get('search', {}).get('location', [])
    if isinstance(locations, str):
        locations = [locations]
    loc_tokens = [l.lower() for l in locations if l]
    def _location_matches(job: dict) -> bool:
        text = f"{job.get('location','')} {job.get('description','')}".lower()
        if any(tok in text for tok in loc_tokens):
            return True
        # common Germany tokens
        if "germany" in text or "deutschland" in text:
            return True
        # exclude obvious US-only locations
        if any(t in text for t in ["united states", "usa", "us ", "u.s."]):
            return False
        # if no location info at all, keep (avoid false negatives)
        return not bool(job.get('location'))

    jobs = [j for j in jobs if _location_matches(j)]

    # Split matches vs non-matches
    min_score = config.get('min_score', 5)
    matches = [j for j in jobs if j.get('score', 0) >= min_score]
    nonmatches = [j for j in jobs if j.get('score', 0) < min_score]


    # Step 4: Export to file (Excel/CSV)
    print("\nðŸ“Š Step 4: Exporting to file...")
    output_path = os.path.join(os.path.dirname(__file__), output_file)
    if output_path.lower().endswith(".csv"):
        append_new_to_csv(matches, output_path)
        nonmatch_file = config.get('output_nonmatch_file', 'daily_jobs_nonmatch.csv')
        nonmatch_path = os.path.join(os.path.dirname(__file__), nonmatch_file)
        append_new_to_csv(nonmatches, nonmatch_path)
    else:
        append_new_to_excel(matches, output_path)
    
    # Summary
    great_matches = len([j for j in jobs if j.get('score', 0) >= 8])
    good_matches = len([j for j in jobs if 5 <= j.get('score', 0) < 8])
    
    print("\n" + "=" * 60)
    print("âœ… COMPLETE!")
    print(f"   Total jobs found: {len(jobs)}")
    print(f"   ðŸŸ¢ Great matches (8-10): {great_matches}")
    print(f"   ðŸŸ¡ Good matches (5-7): {good_matches}")
    print(f"   ðŸ“ Output: {output_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()

