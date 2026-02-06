# 🔍 Job Scraper - Automated Job Search and AI Rating

A Python-based job scraper that finds relevant job postings, rates them using AI, and exports results to CSV/Excel.

## ✨ Features

- 🔎 Multi-source scraping: LinkedIn, Arbeitnow, company career pages
- 🤖 AI-powered rating: Groq, Gemini, or DeepSeek
- 📄 Smart filtering: auto-reads resume PDF and filters by keywords
- 📊 CSV/Excel output: color-coded Excel and CSV exports
- 🧵 Scrape/score separation: queue scraped jobs and score later
- ⏰ Scheduled scoring: score in a time window with daily limits
- 🛡️ Resilient career scraping: Playwright retries and requests fallback

## 📋 Requirements

- 🐍 Python 3.10+
- 🔑 API key from one of:
  - Groq (recommended)
  - Gemini
  - DeepSeek

## 🚀 Quick Start

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
Copy and edit config:
```bash
cp config.example.json config.json
```

### 4) Add resume
Place a resume PDF in the project folder.

### 5) Run
```bash
python main.py
```

## 📁 Output

Primary outputs:
- ✅ `daily_jobs.csv` (matches)
- ❌ `daily_jobs_nonmatch.csv` (non-matches)
- 📗 `daily_jobs.xlsx` (if Excel output is enabled)

Queue:
- 🧺 `score_pending_jobs.csv` (scraped jobs waiting to be scored)

Logs:
- 🧯 `zero_results_log.csv` (sources that returned 0 jobs)
- ⏱️ `timeout_log.csv` (company page timeouts)
- 🛑 `failed_requests_log.csv` (HTTP errors/non-200)
- 📊 `daily_summary_log.csv` (daily counts by source)
- 🖼️ `logs/screens/` (Playwright failure screenshots)

## 🔧 Command Line Options

```bash
python main.py                  # Full run with scoring
python main.py --no-rate        # Skip AI rating
python main.py --scrape-only    # Scrape and queue only
python main.py --score-only     # Score pending jobs (scheduled mode)
python main.py --score-only-manual  # Score pending jobs now (no limits)
python main.py -o custom.csv    # Custom output file
```

## ⚙️ Key Configuration Options

```json
{
  "sources": {
    "linkedin": true,
    "arbeitnow": true,
    "company_careers": true
  },
  "score_pending_file": "score_pending_jobs.csv",
  "scoring_window_start": "01:00",
  "scoring_window_end": "07:00",
  "scoring_max_per_day": 40,
  "scoring_cooldown_seconds": 300,
  "scoring_daily_log": "scoring_daily_log.json",
  "company_careers_startup_timeout": 30,
  "company_careers_site_timeout": 180,
  "company_careers_retry_backoff_seconds": [2, 5, 10],
  "screenshot_on_failure": true,
  "screenshot_dir": "logs/screens",
  "zero_results_log_file": "zero_results_log.csv",
  "timeout_log_file": "timeout_log.csv",
  "failed_requests_log_file": "failed_requests_log.csv",
  "daily_summary_log_file": "daily_summary_log.csv",
  "linkedin_search_url": "https://www.linkedin.com/jobs/search/?...",
  "linkedin_max_pages": 40,
  "linkedin_page_size": 25,
  "linkedin_use_playwright": false,
  "linkedin_print_links": false
}
```

## 🗓️ Scheduling (Windows)

Use the provided scripts:
- 📜 `scripts/score_only_scheduled.ps1`
- 🧩 `scripts/schedule_score_only.xml` (Task Scheduler import)

This runs:
```bash
python main.py --score-only
```

## 🧾 Files

| File | Purpose |
|------|---------|
| `main.py` | Main runner script |
| `scraper.py` | Scraping logic |
| `rater.py` | AI rating |
| `exporter.py` | CSV/Excel export |
| `resume_parser.py` | Resume parsing |
| `config.json` | Configuration |

## 📄 License

MIT
