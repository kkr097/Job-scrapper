"""
Job Scraper - Scrapes jobs from multiple sources including:
- LinkedIn (reliable)
- Arbeitnow (Germany-focused, scraper-friendly)
- Direct company career pages (BMW, VW, Bosch, etc.)
"""

import re
import json
import hashlib
import time
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from urllib.parse import urlencode, quote_plus

import requests
from bs4 import BeautifulSoup


# German automotive companies with their career page patterns
GERMAN_AUTOMOTIVE_COMPANIES = [
    {
        'name': 'BMW',
        'url': 'https://www.bmwgroup.jobs/de/de/jobfinder.html?keywords={keyword}',
        'pattern': 'automotive',
    },
    {
        'name': 'Mercedes-Benz',
        'url': 'https://jobs.mercedes-benz.com/de/de/search-results?keywords={keyword}',
        'pattern': 'automotive',
    },
    {
        'name': 'Volkswagen',
        'url': 'https://www.volkswagen-karriere.de/de/stellensuche.html?search={keyword}',
        'pattern': 'automotive',
    },
    {
        'name': 'Bosch',
        'url': 'https://www.bosch.de/karriere/stellensuche/?keywords={keyword}&country=DE',
        'pattern': 'automotive',
    },
    {
        'name': 'Continental',
        'url': 'https://jobs.continental.com/en/search-results?keywords={keyword}',
        'pattern': 'automotive',
    },
    {
        'name': 'ZF Friedrichshafen',
        'url': 'https://jobs.zf.com/go/All-open-positions/8769601/?q={keyword}',
        'pattern': 'automotive',
    },
]


class JobScraper:
    """Scrapes job postings from multiple sources."""
    
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    ]
    
    def __init__(self, config: dict):
        self.config = config
        self.keywords = config['search']['keywords']
        self.location = config['search']['location']
        self.max_days = config['search'].get('max_days_old', 2)
        self.exclude = config.get('exclude_keywords', [])
        self.sources = config.get('sources', {})
        self.seen_jobs = set()
    
    def _get_headers(self) -> dict:
        return {
            'User-Agent': random.choice(self.USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5,de;q=0.3',
        }
    
    def _random_delay(self, min_sec: float = 0.5, max_sec: float = 2.0):
        time.sleep(random.uniform(min_sec, max_sec))
    
    def scrape_all(self) -> List[Dict]:
        """Scrape jobs from all enabled sources."""
        all_jobs = []
        
        # 1. LinkedIn (most reliable)
        if self.sources.get('linkedin', True):
            print("\n📌 Scraping LinkedIn...")
            for keyword in self.keywords:
                jobs = self._scrape_linkedin(keyword)
                all_jobs.extend(jobs)
                print(f"   {keyword}: {len(jobs)} jobs")
                self._random_delay()
        
        # 2. Arbeitnow (Germany-focused, scraper-friendly)
        if self.sources.get('arbeitnow', True):
            print("\n📌 Scraping Arbeitnow...")
            jobs = self._scrape_arbeitnow()
            all_jobs.extend(jobs)
            print(f"   Found {len(jobs)} jobs")
        
        # 3. SimplyHired (alternative aggregator)
        if self.sources.get('simplyhired', True):
            print("\n📌 Scraping SimplyHired...")
            for keyword in self.keywords[:3]:  # Limit to top 3 keywords
                jobs = self._scrape_simplyhired(keyword)
                all_jobs.extend(jobs)
                print(f"   {keyword}: {len(jobs)} jobs")
                self._random_delay()
        
        # 4. Direct company career pages
        if self.sources.get('company_careers', True):
            print("\n📌 Scraping automotive company career pages...")
            jobs = self._scrape_company_careers()
            all_jobs.extend(jobs)
            print(f"   Found {len(jobs)} jobs from company sites")
        
        # Deduplicate
        unique_jobs = self._deduplicate(all_jobs)
        print(f"\n✓ Total unique jobs: {len(unique_jobs)}")
        
        return unique_jobs
    
    def _scrape_linkedin(self, keyword: str) -> List[Dict]:
        """Scrape LinkedIn public job search."""
        jobs = []
        keyword_enc = quote_plus(keyword)
        location_enc = quote_plus(self.location)
        url = f"https://www.linkedin.com/jobs/search?keywords={keyword_enc}&location={location_enc}&f_TPR=r172800"
        
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            
            cards = soup.find_all('div', class_=re.compile(r'base-card|job-search-card'))
            
            for card in cards[:20]:
                job = self._parse_linkedin_card(card)
                if job and not self._should_exclude(job):
                    jobs.append(job)
                    
        except Exception as e:
            print(f"   ⚠ LinkedIn error: {e}")
        
        return jobs
    
    def _parse_linkedin_card(self, card) -> Optional[Dict]:
        try:
            title_elem = card.find('h3', class_=re.compile(r'base-search-card__title'))
            title = title_elem.get_text(strip=True) if title_elem else None
            
            company_elem = card.find('h4', class_=re.compile(r'base-search-card__subtitle'))
            company = company_elem.get_text(strip=True) if company_elem else "Unknown"
            
            loc_elem = card.find('span', class_=re.compile(r'job-search-card__location'))
            location = loc_elem.get_text(strip=True) if loc_elem else self.location
            
            link = card.find('a', class_=re.compile(r'base-card__full-link'))
            url = link.get('href', '').split('?')[0] if link else ""
            
            if not title:
                return None
            
            return {
                'title': title, 'company': company, 'location': location,
                'url': url, 'source': 'LinkedIn', 'date_found': datetime.now().isoformat()
            }
        except:
            return None
    
    def _scrape_arbeitnow(self) -> List[Dict]:
        """Scrape Arbeitnow - Germany-focused job board with API."""
        jobs = []
        
        try:
            # Arbeitnow has a public JSON API!
            url = "https://www.arbeitnow.com/api/job-board-api"
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            data = response.json()
            
            for job_data in data.get('data', [])[:50]:
                title = job_data.get('title', '')
                
                # Filter for engineering jobs
                if any(kw.lower() in title.lower() for kw in ['engineer', 'design', 'cad', 'mechanical', 'konstrukteur']):
                    job = {
                        'title': title,
                        'company': job_data.get('company_name', 'Unknown'),
                        'location': job_data.get('location', 'Germany'),
                        'url': job_data.get('url', ''),
                        'source': 'Arbeitnow',
                        'date_found': datetime.now().isoformat()
                    }
                    if not self._should_exclude(job):
                        jobs.append(job)
                        
        except Exception as e:
            print(f"   ⚠ Arbeitnow error: {e}")
        
        return jobs
    
    def _scrape_simplyhired(self, keyword: str) -> List[Dict]:
        """Scrape SimplyHired Germany."""
        jobs = []
        keyword_enc = quote_plus(keyword)
        url = f"https://www.simplyhired.de/search?q={keyword_enc}&l=Germany"
        
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Find job cards
            cards = soup.find_all('article', class_=re.compile(r'SerpJob'))
            if not cards:
                cards = soup.find_all('div', class_=re.compile(r'jobposting|result'))
            
            for card in cards[:15]:
                try:
                    title_elem = card.find(['h2', 'h3', 'a'], class_=re.compile(r'title|jobTitle'))
                    company_elem = card.find(['span', 'div'], class_=re.compile(r'company|employer'))
                    loc_elem = card.find(['span', 'div'], class_=re.compile(r'location|loc'))
                    link = card.find('a', href=True)
                    
                    if title_elem:
                        job = {
                            'title': title_elem.get_text(strip=True),
                            'company': company_elem.get_text(strip=True) if company_elem else "Unknown",
                            'location': loc_elem.get_text(strip=True) if loc_elem else "Germany",
                            'url': f"https://www.simplyhired.de{link['href']}" if link and link['href'].startswith('/') else (link['href'] if link else ''),
                            'source': 'SimplyHired',
                            'date_found': datetime.now().isoformat()
                        }
                        if not self._should_exclude(job):
                            jobs.append(job)
                except:
                    continue
                    
        except Exception as e:
            print(f"   ⚠ SimplyHired error: {e}")
        
        return jobs
    
    def _scrape_company_careers(self) -> List[Dict]:
        """Scrape career pages of major German automotive companies."""
        jobs = []
        
        # Use Google search to find jobs on company career pages
        companies = ['BMW', 'Mercedes-Benz', 'Volkswagen', 'Bosch', 'Continental', 'ZF', 
                     'Schaeffler', 'Mahle', 'HELLA', 'Bertrandt', 'EDAG', 'FEV']
        
        for company in companies:
            try:
                # Search LinkedIn for company-specific jobs
                search_term = f"{company} mechanical engineer Germany"
                keyword_enc = quote_plus(search_term)
                url = f"https://www.linkedin.com/jobs/search?keywords={keyword_enc}&f_TPR=r172800"
                
                response = requests.get(url, headers=self._get_headers(), timeout=10)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'lxml')
                    cards = soup.find_all('div', class_=re.compile(r'base-card'))[:5]
                    
                    for card in cards:
                        job = self._parse_linkedin_card(card)
                        if job and company.lower() in job['company'].lower():
                            job['source'] = f'LinkedIn ({company})'
                            if not self._should_exclude(job):
                                jobs.append(job)
                
                self._random_delay(0.3, 0.8)
                
            except Exception as e:
                continue
        
        return jobs
    
    def _should_exclude(self, job: Dict) -> bool:
        title = job.get('title', '').lower()
        for word in self.exclude:
            if word.lower() in title:
                return True
        return False
    
    def _deduplicate(self, jobs: List[Dict]) -> List[Dict]:
        unique = []
        for job in jobs:
            key = f"{job['company'].lower().strip()}|{job['title'].lower().strip()}"
            job_hash = hashlib.md5(key.encode()).hexdigest()
            
            if job_hash not in self.seen_jobs:
                self.seen_jobs.add(job_hash)
                job['job_id'] = job_hash[:8]
                unique.append(job)
        
        return unique


if __name__ == "__main__":
    import os
    
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path) as f:
        config = json.load(f)
    
    print("=" * 50)
    print("JOB SCRAPER TEST")
    print("=" * 50)
    
    scraper = JobScraper(config)
    jobs = scraper.scrape_all()
    
    print("\n--- Sample Jobs ---")
    for job in jobs[:5]:
        print(f"• {job['title']} at {job['company']} [{job['source']}]")
