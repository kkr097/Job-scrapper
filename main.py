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
import argparse
from datetime import datetime

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import JobScraper
from exporter import export_to_excel
from resume_parser import get_resume_summary


def load_config():
    """Load configuration from config.json."""
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description='Job Scraper - Find jobs matching your resume')
    parser.add_argument('--no-rate', action='store_true', help='Skip AI rating (test mode)')
    parser.add_argument('--output', '-o', default=None, help='Output Excel file path')
    args = parser.parse_args()
    
    print("=" * 60)
    print("🔍 JOB SCRAPER")
    print(f"   Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    
    # Load config
    config = load_config()
    
    # Get output path
    output_file = args.output or config['output'].get('excel_file', 'daily_jobs.xlsx')
    
    # Step 1: Parse resume
    print("\n📄 Step 1: Reading resume...")
    try:
        resume_summary = get_resume_summary(os.path.dirname(__file__))
        print("   ✓ Resume loaded successfully")
    except Exception as e:
        print(f"   ✗ Error reading resume: {e}")
        print("   Using default skills from config...")
        resume_summary = "Mechanical Design Engineer with CATIA V5, Autodesk Inventor, automotive and railway experience."
    
    # Step 2: Scrape jobs
    print("\n🌐 Step 2: Scraping job sites...")
    scraper = JobScraper(config)
    jobs = scraper.scrape_all()
    
    if not jobs:
        print("\n⚠ No jobs found. Try adjusting your search keywords in config.json")
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
            print("\n🤖 Step 3: Rating jobs with AI...")
            try:
                from rater import JobRater
                rater = JobRater(config, resume_summary)
                jobs = rater.rate_jobs(jobs)
                print("   ✓ All jobs rated")
            except Exception as e:
                print(f"   ⚠ Rating failed: {e}")
                import traceback
                traceback.print_exc()
                print("   Assigning default scores...")
                for job in jobs:
                    job['score'] = 5
                    job['match_reasons'] = 'AI rating unavailable'
                    job['missing_skills'] = ''
        else:
            print("\n⚠ Step 3: No API key configured")
            print("   Add an API key to config.json (DeepSeek, Groq, or Gemini)")
            print("   Assigning default scores for now...")
            for job in jobs:
                job['score'] = 5
                job['match_reasons'] = 'Add API key to enable rating'
                job['missing_skills'] = ''
    else:
        print("\n⏭ Step 3: Skipping AI rating (--no-rate flag)")
        for job in jobs:
            job['score'] = 5
            job['match_reasons'] = 'Rating skipped'
            job['missing_skills'] = ''
    
    # Step 4: Export to Excel
    print("\n📊 Step 4: Exporting to Excel...")
    output_path = os.path.join(os.path.dirname(__file__), output_file)
    export_to_excel(jobs, output_path)
    
    # Summary
    great_matches = len([j for j in jobs if j.get('score', 0) >= 8])
    good_matches = len([j for j in jobs if 5 <= j.get('score', 0) < 8])
    
    print("\n" + "=" * 60)
    print("✅ COMPLETE!")
    print(f"   Total jobs found: {len(jobs)}")
    print(f"   🟢 Great matches (8-10): {great_matches}")
    print(f"   🟡 Good matches (5-7): {good_matches}")
    print(f"   📁 Output: {output_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
