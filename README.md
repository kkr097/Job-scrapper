# 🔍 Job Scraper - Automated Job Search & AI Rating

A Python-based job scraper that finds relevant job postings, rates them using AI, and exports results to a color-coded Excel file.

## ✨ Features

- **Multi-source scraping**: LinkedIn, Arbeitnow, and more
- **AI-powered rating**: Uses Groq (free), Gemini, or DeepSeek to rate jobs 1-10
- **Smart filtering**: Auto-reads your resume PDF and filters by keywords
- **Color-coded Excel**: Green (8-10), Yellow (5-7), Red (1-4) matches
- **Date filtering**: Only shows jobs from the last 2 days

## 📋 Requirements

- Python 3.10+
- API key from one of:
  - [Groq](https://console.groq.com/keys) (FREE, recommended)
  - [Gemini](https://aistudio.google.com/app/apikey) (FREE 1500/day)
  - [DeepSeek](https://platform.deepseek.com/api_keys)

## 🚀 Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/bharath1996-hub/Job-scrapper.git
cd Job-scrapper
```

### 2. Install dependencies
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure
Copy the example config and add your API key:
```bash
cp config.example.json config.json
```

Edit `config.json`:
```json
{
    "groq_api_key": "YOUR_GROQ_API_KEY_HERE",
    "search": {
        "keywords": ["Mechanical Engineer", "CAD Engineer"],
        "location": "Germany",
        "max_days_old": 2
    }
}
```

### 4. Add your resume
Place your resume PDF in the project folder (any filename ending in `.pdf`).

### 5. Run
```bash
python main.py
```

## 📁 Output

Results are saved to `daily_jobs.xlsx` with:
- **Score**: AI match rating (1-10)
- **Title**: Job title
- **Company**: Company name
- **Location**: Job location
- **Source**: Where the job was found
- **Match Reasons**: Why AI rated it highly
- **Missing Skills**: What you might need
- **URL**: Direct link to apply

## 🎨 Color Coding

| Score | Color | Meaning |
|-------|-------|---------|
| 8-10 | 🟢 Green | Great match - Apply! |
| 5-7 | 🟡 Yellow | Good match - Worth reviewing |
| 1-4 | 🔴 Red | Poor match - Wrong field |

## ⚙️ Configuration Options

```json
{
    "search": {
        "keywords": ["..."],      // Job titles to search
        "location": "Germany",    // Geographic filter
        "max_days_old": 2         // Only jobs from last N days
    },
    "exclude_keywords": [         // Skip jobs containing these
        "Senior", "Lead", "Intern"
    ],
    "sources": {
        "linkedin": true,         // Enable/disable sources
        "arbeitnow": true,
        "company_careers": true
    }
}
```

## 🏢 Automotive Company Focus

The scraper specifically searches career pages of major German automotive companies:
- BMW, Mercedes-Benz, Volkswagen
- Bosch, Continental, ZF
- Bertrandt, EDAG, FEV
- Schaeffler, Mahle, HELLA

## 📝 Files

| File | Purpose |
|------|---------|
| `main.py` | Main runner script |
| `scraper.py` | Job scraping logic |
| `rater.py` | AI rating engine |
| `exporter.py` | Excel export with formatting |
| `resume_parser.py` | Auto-reads your resume PDF |
| `config.json` | Configuration settings |

## 🔧 Command Line Options

```bash
python main.py              # Full run with AI rating
python main.py --no-rate    # Skip AI rating (test mode)
python main.py -o custom.xlsx  # Custom output filename
```

## 📅 Daily Automation (macOS)

To run automatically every day at 8 AM:

```bash
crontab -e
# Add this line:
0 8 * * * cd /path/to/Scrapper && ./venv/bin/python main.py
```

## 🤖 AI Models Supported

| Provider | Model | Free Tier |
|----------|-------|-----------|
| Groq | Llama 3.3 70B | ✅ Very generous |
| Gemini | Gemini 2.0 Flash | ✅ 1500/day |
| DeepSeek | DeepSeek Chat | ⚠️ Requires credits |

## 📄 License

MIT License - Feel free to use and modify!

---

Made with ❤️ for job seekers
