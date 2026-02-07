# Job Scraper - Automated Job Search and AI Rating

A Python-based job scraper that finds relevant job postings, rates them using AI, and exports results to CSV/Excel.

## Features

- Multi-source scraping: LinkedIn (public guest search), XING (Playwright show-more), Arbeitnow, company career pages
- AI scoring: Gemini / Groq / DeepSeek with batch requests and cooling
- Profile-driven scoring: `candidate_profile.json` (resume fallback optional)
- CSV/Excel output: description column included, color-coded Excel
- Scrape/score separation: queue scraped jobs and score later
- Scheduled scoring: time window + daily limits
- Manual guardrails: negative-title keyword skip + FIFO `rejected_jobs.csv`

## Requirements

- Python 3.10+
- API key from one of:
  - Groq
  - Gemini
  - DeepSeek

## Quick Start

### 1) Clone
```bash
git clone https://github.com/bharath1996-hub/Job-scrapper.git
cd Job-scrapper
```

### 2) Install
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3) Configure
Edit `config.json` and `candidate_profile.json` (required for scoring).

### 4) (Optional) Add resume
If you remove `candidate_profile.json`, place a resume PDF in the project folder as fallback.

### 5) Run
```bash
python main.py
```

## Flowchart

```mermaid
flowchart TD
    A[Start] --> B[Load config.json]
    B --> C{candidate_profile.json exists?}
    C -->|Yes| D[Load candidate_profile.json]
    C -->|No| E[Parse resume PDF]
    D --> F[Scrape sources]
    E --> F
    F --> G[Manual filter: negative title keywords]
    G -->|Rejected| H[rejected_jobs.csv FIFO]
    G -->|Keep| I[Queue or Score]
    I -->|--scrape-only| J[score_pending_jobs.csv]
    I -->|--score-only| K[Batch LLM scoring]
    I -->|default| K
    K --> L[Export daily_jobs.csv + daily_jobs_nonmatch.csv (+ .xlsx if configured)]
```

## Output

Primary outputs:
- `daily_jobs.csv` (matches)
- `daily_jobs_nonmatch.csv` (non-matches)
- `daily_jobs.xlsx` (if Excel output is enabled)

Queue:
- `score_pending_jobs.csv` (scraped jobs waiting to be scored)

Manual filter:
- `rejected_jobs.csv` (FIFO 100 rejected jobs)

Logs:
- `zero_results_log.csv` (sources that returned 0 jobs)
- `timeout_log.csv` (company page timeouts)
- `failed_requests_log.csv` (HTTP errors/non-200)
- `daily_summary_log.csv` (daily counts by source)
- `logs/screens/` (Playwright failure screenshots)

## Command Line Options

```bash
python main.py                  # Full run with scoring
python main.py --no-rate        # Skip AI rating
python main.py --scrape-only    # Scrape and queue only
python main.py --score-only     # Score pending jobs (scheduled mode)
python main.py --score-only-manual  # Score pending jobs now (no limits)
python main.py -o custom.csv    # Custom output file
```

## Key Configuration Options

```json
{
  "sources": {
    "linkedin": true,
    "xing": true,
    "arbeitnow": true
  },
  "scrape_test_mode": false,
  "score_pending_file": "score_pending_jobs.csv",
  "scoring_window_start": "01:00",
  "scoring_window_end": "07:00",
  "scoring_max_per_day": 40,
  "scoring_cooldown_seconds": 300,
  "scoring_daily_log": "scoring_daily_log.json",
  "linkedin_search_url": "https://www.linkedin.com/jobs/search/?...",
  "linkedin_search_urls": [],
  "linkedin_search_urls_enabled": true,
  "linkedin_fetch_all": true,
  "linkedin_page_size": 50,
  "linkedin_scroll_step": 10,
  "linkedin_use_playwright": false,
  "linkedin_print_links": true,
  "xing_search_url": "https://www.xing.com/jobs/search/ki?...",
  "xing_use_playwright": true,
  "xing_headless": false,
  "xing_max_clicks": 12,
  "llm_batch_sleep_seconds": 60
}
```

## Scheduling (Windows)

Use the provided scripts:
- `scripts/score_only_scheduled.ps1`
- `scripts/schedule_score_only.xml` (Task Scheduler import)

This runs:
```bash
python main.py --score-only
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | Main runner script |
| `scraper.py` | Scraping logic |
| `rater.py` | AI rating |
| `exporter.py` | CSV/Excel export |
| `candidate_profile.json` | Candidate profile + rules |
| `resume_parser.py` | Resume parsing (fallback) |
| `config.json` | Configuration |

## License

MIT
