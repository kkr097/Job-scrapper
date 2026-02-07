"""
Job Scraper - Scrapes jobs from multiple sources including:
- LinkedIn (reliable)
- Arbeitnow (Germany-focused, scraper-friendly)
- Direct company career pages (BMW, VW, Bosch, etc.)
"""

import re
import os
import json
import hashlib
import math
import time
import random
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlencode, quote_plus, urljoin, urlparse, parse_qs, urlunparse

import requests
import csv
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None


class JobScraper:
    """Scrapes job postings from multiple sources."""
    
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    ]
    
    def __init__(self, config: dict):
        self.config = config
        search_cfg = config.get('search', {})
        self.keywords = search_cfg.get('keywords', [])
        self.location = search_cfg.get('location', [])
        if isinstance(self.location, list):
            self.locations = self.location
        else:
            self.locations = [self.location]
        self.max_days = search_cfg.get('max_days_old', 2)
        self.exclude = config.get('exclude_keywords', [])
        self.exclude_desc = config.get('exclude_description_keywords', [])
        self.include_desc = config.get('include_description_keywords', [])
        self.sources = config.get('sources', {})
        self.seen_jobs = set()
        # LLM routing state
        self._llm_state = {
            "preferred": config.get("rating_preferred", "groq"),
            "groq_cooldown_until": 0,
            "gemini_cooldown_until": 0
        }
        self._on_job = None
    
    def _get_headers(self, source: Optional[str] = None) -> dict:
        if source == "linkedin":
            ua = self.config.get("linkedin_user_agent") or self.USER_AGENTS[0]
        else:
            ua = random.choice(self.USER_AGENTS)
        return {
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5,de;q=0.3',
        }
    
    def _random_delay(self, min_sec: float = 0.5, max_sec: float = 2.0):
        time.sleep(random.uniform(min_sec, max_sec))

    def _linkedin_delay(self):
        min_sec = float(self.config.get("linkedin_delay_min", 5))
        max_sec = float(self.config.get("linkedin_delay_max", 5))
        time.sleep(random.uniform(min_sec, max_sec))

    def _linkedin_card_delay(self):
        min_sec = float(self.config.get("linkedin_card_delay_min", 0.8))
        max_sec = float(self.config.get("linkedin_card_delay_max", 2.0))
        time.sleep(random.uniform(min_sec, max_sec))

    def _log_event(self, kind: str, payload: Dict) -> None:
        log_map = {
            "zero_results": self.config.get("zero_results_log_file", "zero_results_log.csv"),
            "timeout": self.config.get("timeout_log_file", "timeout_log.csv"),
            "failed_request": self.config.get("failed_requests_log_file", "failed_requests_log.csv"),
            "daily_summary": self.config.get("daily_summary_log_file", "daily_summary_log.csv"),
        }
        path = log_map.get(kind)
        if not path:
            return
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(base_dir, path)
            write_header = not os.path.exists(log_path)
            fieldnames = ["timestamp", "source", "company", "url", "details"]
            with open(log_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "source": payload.get("source", ""),
                    "company": payload.get("company", ""),
                    "url": payload.get("url", ""),
                    "details": payload.get("details", "")
                })
        except Exception:
            pass

    def _log_failed_request(self, source: str, url: str, details: str) -> None:
        self._log_event("failed_request", {
            "source": source,
            "company": "",
            "url": url,
            "details": details
        })

    def _log_daily_summary(self, jobs: List[Dict], elapsed_sec: float) -> None:
        counts = {}
        for j in jobs:
            src = (j.get("source") or "Unknown").strip()
            counts[src] = counts.get(src, 0) + 1
        details = f"total={len(jobs)} elapsed_sec={int(elapsed_sec)} by_source={counts}"
        self._log_event("daily_summary", {
            "source": "summary",
            "company": "",
            "url": "",
            "details": details
        })

    def _maybe_screenshot(self, page, company: str, label: str) -> None:
        if not self.config.get("screenshot_on_failure", True):
            return
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            out_dir = self.config.get("screenshot_dir", "logs/screens")
            out_path = os.path.join(base_dir, out_dir)
            os.makedirs(out_path, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_company = re.sub(r"[^a-zA-Z0-9_-]+", "_", company)
            safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)
            file_path = os.path.join(out_path, f"{safe_company}_{safe_label}_{ts}.png")
            page.screenshot(path=file_path, full_page=True)
        except Exception:
            pass
    
    def scrape_all(self, on_job=None) -> List[Dict]:
        """Scrape jobs from all enabled sources."""
        self._on_job = on_job
        all_jobs = []
        started = time.time()
        
        # 1. LinkedIn (public guest search only)
        if self.sources.get('linkedin', True):
            print("\nðŸ“Œ Scraping LinkedIn...")
            linkedin_url = self.config.get("linkedin_search_url", "").strip()
            linkedin_urls = []
            if self.config.get("linkedin_search_urls_enabled", True):
                linkedin_urls = self.config.get("linkedin_search_urls", []) or []
                linkedin_urls = [u for u in linkedin_urls if isinstance(u, str) and u.strip()]
            if linkedin_url:
                linkedin_urls = [linkedin_url] + linkedin_urls
            use_playwright = bool(self.config.get("linkedin_use_playwright", False))
            if use_playwright:
                print("   âš  Playwright disabled for LinkedIn. Using public guest search only.")
            if linkedin_urls:
                for custom_url in linkedin_urls:
                    jobs = self._scrape_linkedin(None, None, base_url=custom_url)
                    all_jobs.extend(jobs)
                    print(f"   LinkedIn custom URL: {len(jobs)} jobs")
                    if not jobs:
                        self._log_event("zero_results", {
                            "source": "linkedin",
                            "company": "",
                            "url": custom_url,
                            "details": "No jobs returned"
                        })
                    if self.config.get("linkedin_print_links", False):
                        for j in jobs:
                            if j.get("url"):
                                print(f"   â€¢ LinkedIn link: {j['url']}")
                    self._linkedin_delay()
            else:
                for keyword in self.keywords:
                    for location in self.locations:
                        jobs = self._scrape_linkedin(keyword, location)
                        all_jobs.extend(jobs)
                        print(f"   {keyword} ({location}): {len(jobs)} jobs")
                        if not jobs:
                            self._log_event("zero_results", {
                                "source": "linkedin",
                                "company": "",
                                "url": "",
                                "details": f"No jobs for {keyword} ({location})"
                            })
                        if self.config.get("linkedin_print_links", False):
                            for j in jobs:
                                if j.get("url"):
                                    print(f"   â€¢ LinkedIn link: {j['url']}")
                        self._linkedin_delay()

        # 2. XING (public search)
        if self.sources.get('xing', False):
            print("\nðŸ“Œ Scraping XING...")
            xing_url = self.config.get("xing_search_url", "").strip()
            xing_urls = self.config.get("xing_search_urls", []) or []
            xing_urls = [u for u in xing_urls if isinstance(u, str) and u.strip()]
            if xing_url:
                xing_urls = [xing_url] + xing_urls
            if xing_urls:
                use_playwright = bool(self.config.get("xing_use_playwright", True))
                for custom_url in xing_urls:
                    if use_playwright and sync_playwright:
                        jobs = self._scrape_xing_playwright(base_url=custom_url)
                    else:
                        jobs = self._scrape_xing(base_url=custom_url)
                    all_jobs.extend(jobs)
                    print(f"   XING custom URL: {len(jobs)} jobs")
                    if not jobs:
                        self._log_event("zero_results", {
                            "source": "xing",
                            "company": "",
                            "url": custom_url,
                            "details": "No jobs returned"
                        })
                    if self.config.get("xing_print_links", False):
                        for j in jobs:
                            if j.get("url"):
                                print(f"   â€¢ XING link: {j['url']}")
                    self._linkedin_delay()

        # 3. Arbeitnow (Germany-focused, scraper-friendly)
        if self.sources.get('arbeitnow', True):
            print("\nðŸ“Œ Scraping Arbeitnow...")
            jobs = self._scrape_arbeitnow()
            all_jobs.extend(jobs)
            print(f"   Found {len(jobs)} jobs")
            if not jobs:
                self._log_event("zero_results", {
                    "source": "arbeitnow",
                    "company": "",
                    "url": "https://www.arbeitnow.com/api/job-board-api",
                    "details": "No jobs returned"
                })
        
        # 4. SimplyHired (alternative aggregator)
        if self.sources.get('simplyhired', True):
            print("\nðŸ“Œ Scraping SimplyHired...")
            for keyword in self.keywords[:3]:  # Limit to top 3 keywords
                for location in self.locations:
                    jobs = self._scrape_simplyhired(keyword, location)
                    all_jobs.extend(jobs)
                    print(f"   {keyword} ({location}): {len(jobs)} jobs")
                    if not jobs:
                        self._log_event("zero_results", {
                            "source": "simplyhired",
                            "company": "",
                            "url": "",
                            "details": f"No jobs for {keyword} ({location})"
                        })
                    self._linkedin_delay()

        # 5. Direct company career pages (Playwright-only)
        if self.sources.get('company_careers', False):
            print("\nðŸ“Œ Scraping company career pages (Playwright)...")
            jobs = self._scrape_company_careers_playwright()
            all_jobs.extend(jobs)
            print(f"   Found {len(jobs)} jobs from company sites")
        
        # Deduplicate
        unique_jobs = self._deduplicate(all_jobs)
        print(f"\nâœ“ Total unique jobs: {len(unique_jobs)}")
        
        self._log_daily_summary(unique_jobs, time.time() - started)
        return unique_jobs

    def _emit_job(self, job: Dict) -> None:
        if self._on_job and isinstance(job, dict) and job.get("url"):
            try:
                self._on_job(job)
            except Exception:
                pass
    
    def _scrape_linkedin(self, keyword: Optional[str], location: Optional[str], base_url: Optional[str] = None) -> List[Dict]:
        """Scrape LinkedIn public guest search (logged-out)."""
        jobs = []
        # Guest search endpoint (no auth, no cookies)
        if base_url:
            url = self._normalize_linkedin_guest_url(base_url)
        else:
            keyword_enc = quote_plus(keyword or "")
            location_enc = quote_plus(location or "")
            url = (
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?keywords={keyword_enc}&location={location_enc}&f_TPR=r172800"
            )
        max_pages = int(self.config.get("linkedin_max_pages", 20))
        page_size = int(self.config.get("linkedin_page_size", 25))
        fetch_all = bool(self.config.get("linkedin_fetch_all", False))
        scroll_step = int(self.config.get("linkedin_scroll_step", page_size))
        if scroll_step <= 0:
            scroll_step = page_size

        def _extract_total_jobs(soup: BeautifulSoup) -> Optional[int]:
            # Try specific counters first
            count_el = soup.find(class_=re.compile(r"(job-count|results-count|results-context-header__job-count)"))
            if count_el:
                m = re.search(r"(\d[\d,\.]*)", count_el.get_text(" ", strip=True))
                if m:
                    return int(m.group(1).replace(",", "").replace(".", ""))
            # Fallback: scan text for "jobs"
            text = soup.get_text(" ", strip=True)
            m = re.search(r"(\d[\d,\.]*)\s+jobs?\b", text, re.IGNORECASE)
            if m:
                return int(m.group(1).replace(",", "").replace(".", ""))
            return None
        
        try:
            if fetch_all and max_pages < 10:
                max_pages = 10
            seen_urls = set()
            empty_pages = 0
            total_items = max_pages * page_size
            start_values = list(range(0, total_items, scroll_step))
            for start in start_values:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                qs["start"] = [str(start)]
                qs["count"] = [str(page_size)]
                page_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
                response = requests.get(page_url, headers=self._get_headers("linkedin"), timeout=15, cookies={})
                if response.status_code != 200:
                    self._log_failed_request("linkedin", page_url, f"status={response.status_code}")
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'lxml')
                if start == 0 and (fetch_all or max_pages <= 0):
                    total = _extract_total_jobs(soup)
                    if total:
                        max_pages = max(1, math.ceil(total / page_size))
                        total_items = max_pages * page_size
                        start_values = list(range(0, total_items, scroll_step))

                cards = soup.find_all(class_=re.compile(r'base-card|job-search-card|result-card|job-result-card'))
                if not cards:
                    break

                new_found = 0
                for card in cards:
                    job = self._parse_linkedin_card(card)
                    if job and not self._should_exclude(job):
                        url_l = (job.get("url") or "").lower()
                        if url_l and url_l in seen_urls:
                            continue
                        if url_l:
                            seen_urls.add(url_l)
                        jobs.append(job)
                        self._emit_job(job)
                        new_found += 1
                    self._linkedin_card_delay()
                if new_found == 0:
                    empty_pages += 1
                    if empty_pages >= 3:
                        break
                else:
                    empty_pages = 0
                # Slow down between pages to mimic human scrolling pace
                self._linkedin_delay()
                    
        except Exception as e:
            print(f"   âš  LinkedIn error: {e}")
            self._log_failed_request("linkedin", url, f"error={e}")
        
        return jobs

    def _normalize_linkedin_guest_url(self, url: str) -> str:
        """Convert a LinkedIn jobs search URL into the logged-out guest endpoint."""
        parsed = urlparse(url)
        if "jobs-guest/jobs/api/seeMoreJobPostings/search" in parsed.path:
            return url
        qs = parse_qs(parsed.query)
        # Drop params that can conflict with pagination
        for k in ["pageNum", "position", "currentJobId", "start", "count"]:
            qs.pop(k, None)
        return urlunparse(parsed._replace(
            scheme="https",
            netloc="www.linkedin.com",
            path="/jobs-guest/jobs/api/seeMoreJobPostings/search",
            query=urlencode(qs, doseq=True)
        ))

    def _scrape_linkedin_playwright(
        self,
        keyword: Optional[str] = None,
        location: Optional[str] = None,
        base_url: Optional[str] = None
    ) -> List[Dict]:
        """Scrape LinkedIn public job search using Playwright to reduce 429s."""
        if not sync_playwright:
            print("   âš  Playwright not available. Falling back to requests.")
            return self._scrape_linkedin(keyword, location, base_url=base_url)

        jobs = []
        if base_url:
            url = base_url
        else:
            keyword_enc = quote_plus(keyword or "")
            location_enc = quote_plus(location or "")
            url = f"https://www.linkedin.com/jobs/search?keywords={keyword_enc}&location={location_enc}&f_TPR=r172800"

        max_pages = int(self.config.get("linkedin_max_pages", 20))
        page_size = int(self.config.get("linkedin_page_size", 25))
        max_scrolls = int(self.config.get("linkedin_max_scrolls", 6))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            for page_idx in range(max_pages):
                start = page_idx * page_size
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                qs["start"] = [str(start)]
                page_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    self._log_failed_request("linkedin", page_url, f"playwright_error={e}")
                    break

                # Scroll to load more cards on the page
                try:
                    for _ in range(max_scrolls):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1200)
                except Exception:
                    pass

                try:
                    html = page.content()
                except Exception:
                    break
                soup = BeautifulSoup(html, 'lxml')
                cards = soup.find_all(class_=re.compile(r'base-card|job-search-card|result-card|job-result-card'))
                if not cards:
                    break
                for card in cards:
                    job = self._parse_linkedin_card(card)
                    if job and not self._should_exclude(job):
                        jobs.append(job)
                        self._emit_job(job)
                self._linkedin_delay()
            browser.close()

        return jobs
    
    def _parse_linkedin_card(self, card) -> Optional[Dict]:
        try:
            title_elem = card.find(['h3', 'span'], class_=re.compile(
                r'base-search-card__title|result-card__title|job-search-card__title'
            ))
            title = title_elem.get_text(strip=True) if title_elem else None
            
            company_elem = card.find(['h4', 'span'], class_=re.compile(
                r'base-search-card__subtitle|result-card__subtitle|job-search-card__subtitle'
            ))
            company = company_elem.get_text(strip=True) if company_elem else "Unknown"
            
            loc_elem = card.find('span', class_=re.compile(
                r'job-search-card__location|result-card__location'
            ))
            location = loc_elem.get_text(strip=True) if loc_elem else self.locations[0]
            
            link = card.find('a', class_=re.compile(
                r'base-card__full-link|result-card__full-card-link|job-result-card__full-card-link|result-card__full-link'
            ))
            url = link.get('href', '').split('?')[0] if link else ""
            
            if not title:
                return None
            
            return {
                'title': title, 'company': company, 'location': location,
                'url': url, 'source': 'LinkedIn', 'date_found': datetime.now().isoformat()
            }
        except:
            return None

    def _scrape_xing(self, base_url: str) -> List[Dict]:
        """Scrape XING public job search (basic, list-page only)."""
        jobs = []
        try:
            response = requests.get(base_url, headers=self._get_headers(), timeout=15)
            if response.status_code != 200:
                self._log_failed_request("xing", base_url, f"status={response.status_code}")
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')

            # Prefer anchors that look like job detail links
            anchors = soup.find_all('a', href=re.compile(r'/jobs/'))
            if anchors:
                for a in anchors:
                    href = a.get('href', '').strip()
                    if not href:
                        continue
                    if "/jobs/search" in href or "/jobs/directory" in href:
                        continue
                    if not re.search(r"/jobs/[^/?]+-\d+", href):
                        continue
                    url = href if href.startswith("http") else "https://www.xing.com" + href
                    title = (
                        a.get_text(strip=True)
                        or a.get("title")
                        or a.get("aria-label")
                        or None
                    )
                    if not title:
                        continue
                    # Try to find nearby company/location info
                    card = a.find_parent(['article', 'div']) or a.parent
                    company_elem = card.find(['span', 'div'], class_=re.compile(r'company|employer', re.IGNORECASE)) if card else None
                    company = company_elem.get_text(strip=True) if company_elem else "Unknown"
                    loc_elem = card.find(['span', 'div'], class_=re.compile(r'location|city', re.IGNORECASE)) if card else None
                    location = loc_elem.get_text(strip=True) if loc_elem else ""

                    job = {
                        'title': title,
                        'company': company,
                        'location': location,
                        'url': url,
                        'source': 'XING',
                        'date_found': datetime.now().isoformat()
                    }
                    if not self._should_exclude(job):
                        jobs.append(job)
                        self._emit_job(job)
                    self._random_delay()
            else:
                # Fallback: XING job cards (best-effort selectors)
                cards = soup.find_all(['article', 'div'], class_=re.compile(r'job|result|card', re.IGNORECASE))
                for card in cards:
                    title_elem = card.find(['h2', 'h3', 'a'])
                    title = title_elem.get_text(strip=True) if title_elem else None
                    if not title:
                        continue
                    company_elem = card.find(['span', 'div'], class_=re.compile(r'company|employer', re.IGNORECASE))
                    company = company_elem.get_text(strip=True) if company_elem else "Unknown"
                    loc_elem = card.find(['span', 'div'], class_=re.compile(r'location|city', re.IGNORECASE))
                    location = loc_elem.get_text(strip=True) if loc_elem else ""
                    link = card.find('a', href=True)
                    url = link['href'] if link else ""
                    if "/jobs/search" in url or "/jobs/directory" in url:
                        continue
                    if not re.search(r"/jobs/[^/?]+-\d+", url):
                        continue
                    if url.startswith("/"):
                        url = "https://www.xing.com" + url

                    job = {
                        'title': title,
                        'company': company,
                        'location': location,
                        'url': url,
                        'source': 'XING',
                        'date_found': datetime.now().isoformat()
                    }
                    if not self._should_exclude(job):
                        jobs.append(job)
                        self._emit_job(job)
                    self._random_delay()
        except Exception as e:
            print(f"   âš  XING error: {e}")
            self._log_failed_request("xing", base_url, f"error={e}")
        return jobs

    def _scrape_xing_playwright(self, base_url: str) -> List[Dict]:
        """Scrape XING job search using Playwright to click 'Show more'."""
        jobs = []
        if not sync_playwright:
            return self._scrape_xing(base_url=base_url)

        max_clicks = int(self.config.get("xing_max_clicks", 25))
        headless = bool(self.config.get("xing_headless", True))
        show_more_selector = "button:has-text('Show more'), span:has-text('Show more')"

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                page = browser.new_page()
                page.goto(base_url, wait_until="domcontentloaded", timeout=60000)

                stagnant = 0
                for _ in range(max_clicks):
                    try:
                        page.wait_for_timeout(1200)
                        # Scroll to bottom to trigger lazy load
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1200)

                        before = page.locator("a[href*='/jobs/']").count()
                        if page.locator(show_more_selector).count() == 0:
                            break
                        page.locator(show_more_selector).first.click(timeout=5000)
                        page.wait_for_timeout(1500)
                        after = page.locator("a[href*='/jobs/']").count()
                        if after <= before:
                            stagnant += 1
                            if stagnant >= 2:
                                break
                        else:
                            stagnant = 0
                    except Exception:
                        break

                html = page.content()
                soup = BeautifulSoup(html, 'lxml')
                anchors = soup.find_all('a', href=re.compile(r'/jobs/'))
                for a in anchors:
                    href = a.get('href', '').strip()
                    if not href:
                        continue
                    if "/jobs/search" in href or "/jobs/directory" in href:
                        continue
                    if not re.search(r"/jobs/[^/?]+-\d+", href):
                        continue
                    url = href if href.startswith("http") else "https://www.xing.com" + href
                    title = (
                        a.get_text(strip=True)
                        or a.get("title")
                        or a.get("aria-label")
                        or None
                    )
                    if not title:
                        continue
                    card = a.find_parent(['article', 'div']) or a.parent
                    company_elem = card.find(['span', 'div'], class_=re.compile(r'company|employer', re.IGNORECASE)) if card else None
                    company = company_elem.get_text(strip=True) if company_elem else "Unknown"
                    loc_elem = card.find(['span', 'div'], class_=re.compile(r'location|city', re.IGNORECASE)) if card else None
                    location = loc_elem.get_text(strip=True) if loc_elem else ""

                    job = {
                        'title': title,
                        'company': company,
                        'location': location,
                        'url': url,
                        'source': 'XING',
                        'date_found': datetime.now().isoformat()
                    }
                    if not self._should_exclude(job):
                        jobs.append(job)
                        self._emit_job(job)
                browser.close()
        except Exception as e:
            print(f"   ⚠ XING error (playwright): {e}")
            self._log_failed_request("xing", base_url, f"playwright_error={e}")
        return jobs
    
    def _scrape_arbeitnow(self) -> List[Dict]:
        """Scrape Arbeitnow - Germany-focused job board with API."""
        jobs = []
        
        try:
            # Arbeitnow has a public JSON API!
            url = "https://www.arbeitnow.com/api/job-board-api"
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            if response.status_code != 200:
                self._log_failed_request("arbeitnow", url, f"status={response.status_code}")
            response.raise_for_status()
            data = response.json()
            
            for job_data in data.get('data', [])[:50]:
                title = job_data.get('title', '')
                raw_desc = job_data.get('description', '') or ''
                clean_desc = BeautifulSoup(raw_desc, 'lxml').get_text(' ', strip=True) if raw_desc else ''
                
                # Filter for engineering jobs
                if any(kw.lower() in title.lower() for kw in ['engineer', 'design', 'cad', 'mechanical', 'konstrukteur']):
                    job = {
                        'title': title,
                        'company': job_data.get('company_name', 'Unknown'),
                        'location': job_data.get('location', 'Germany'),
                        'url': job_data.get('url', ''),
                        'description': clean_desc,
                        'source': 'Arbeitnow',
                        'date_found': datetime.now().isoformat()
                    }
                    if not self._should_exclude(job):
                        jobs.append(job)
                        self._emit_job(job)
                        
        except Exception as e:
            print(f"   âš  Arbeitnow error: {e}")
            self._log_failed_request("arbeitnow", url, f"error={e}")
        
        return jobs
    
    def _scrape_simplyhired(self, keyword: str, location: str) -> List[Dict]:
        """Scrape SimplyHired Germany."""
        jobs = []
        keyword_enc = quote_plus(keyword)
        location_enc = quote_plus(location)
        url = f"https://www.simplyhired.de/search?q={keyword_enc}&l={location_enc}"
        
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            if response.status_code != 200:
                self._log_failed_request("simplyhired", url, f"status={response.status_code}")
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
                            self._emit_job(job)
                except:
                    continue
                    
        except Exception as e:
            print(f"   âš  SimplyHired error: {e}")
            self._log_failed_request("simplyhired", url, f"error={e}")
        
        return jobs
    
    
    def _load_career_pages(self) -> List[Dict]:
        pages_file = self.config.get('company_careers_file', 'career_pages.json')
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base_dir, pages_file)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            pages = data.get("pages", [])
            # Default type to "page" if missing
            for p in pages:
                if "type" not in p:
                    p["type"] = "page"
            # Test-only filter
            test_only = self.config.get("company_careers_test_only", [])
            if isinstance(test_only, str):
                test_only = [test_only]
            if test_only:
                test_only_l = [t.lower() for t in test_only]
                pages = [p for p in pages if (p.get("name","").lower() in test_only_l)]
            return pages
        except Exception as e:
            print(f"   âš  Failed to load career pages: {e}")
            return []

    def _fetch_with_cookie_accept(self, session: requests.Session, url: str) -> str:
        """Best-effort cookie accept handling with simple link follow."""
        try:
            resp = session.get(url, headers=self._get_headers(), timeout=30)
            text = resp.text
        except requests.exceptions.RequestException as e:
            print(f"   âš  Request failed for {url}: {e}")
            self._log_failed_request("company_careers", url, f"error={e}")
            return ""
        soup = BeautifulSoup(text, 'lxml')

        accept_texts = ["accept", "agree", "akzept", "zustimmen"]
        for a in soup.find_all('a', href=True):
            label = a.get_text(strip=True).lower()
            if any(t in label for t in accept_texts):
                accept_url = urljoin(url, a['href'])
                try:
                    session.get(accept_url, headers=self._get_headers(), timeout=30)
                    resp = session.get(url, headers=self._get_headers(), timeout=30)
                    return resp.text
                except requests.exceptions.RequestException as e:
                    print(f"   âš  Cookie accept failed for {url}: {e}")
                    return ""

        return text

    def _find_next_page(self, soup: BeautifulSoup, base_url: str) -> Optional[str]:
        link = soup.find('a', rel=lambda x: x and 'next' in x)
        if link and link.get('href'):
            return urljoin(base_url, link['href'])

        next_texts = ["next", "weiter", "nÃ¤chste", ">"]
        for a in soup.find_all('a', href=True):
            label = a.get_text(strip=True).lower()
            if label in next_texts:
                return urljoin(base_url, a['href'])
        return None

    def _extract_job_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        links = []
        keywords = [k.lower() for k in self.keywords]
        for a in soup.find_all('a', href=True):
            text = a.get_text(" ", strip=True).lower()
            href = a['href']
            href_l = href.lower()
            if any(k in text for k in keywords) or any(k in href_l for k in keywords):
                links.append(urljoin(base_url, href))
                continue
            if re.search(r"job|career|vacan|stellen|position|external/job|/job/", href_l, re.IGNORECASE):
                links.append(urljoin(base_url, href))
        normalized = []
        for l in links:
            if not l:
                continue
            if not l.startswith(("http://", "https://")):
                l = urljoin(base_url, l)
            normalized.append(l)
        return list(dict.fromkeys(normalized))

    def _accept_cookies_playwright(self, page) -> None:
        labels = [
            "Accept", "Accept all", "Agree", "Akzeptieren", "Alle akzeptieren",
            "I Agree", "Zustimmen"
        ]
        for label in labels:
            try:
                btn = page.get_by_role("button", name=label)
                if btn.count() > 0:
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(1000)
                    return
            except Exception:
                continue

    def _goto_with_retries(self, page, url: str, company: str) -> bool:
        strategies = [("domcontentloaded", 30000), ("load", 60000), ("networkidle", 30000)]
        if company == "bertrandt":
            strategies = [("domcontentloaded", 30000), ("load", 45000)]
        for wait_until, timeout in strategies:
            try:
                page.goto(url, wait_until=wait_until, timeout=timeout)
                return True
            except Exception:
                continue
        return False

    def _collect_job_links_playwright(
        self,
        page,
        base_url: str,
        link_patterns: Optional[List[str]] = None,
        allow_external: bool = False,
        require_keyword: bool = True,
        site_type: str = "",
        site_id: str = ""
    ) -> List[str]:
        links = []
        keywords = [k.lower() for k in self.keywords]
        anchors = page.query_selector_all("a[href]")
        patterns = [re.compile(p, re.IGNORECASE) for p in (link_patterns or [])]
        allow_query_links = site_type == "ashby" or any("ashby_jid" in p.pattern.lower() for p in patterns)
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            text = (a.inner_text() or "").lower()
            href_l = href.lower().strip()

            if href and not href.startswith(("http://", "https://", "/", "//")):
                # Only prefix scheme for domain-like strings, not relative filenames
                if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/|$)", href):
                    href = "https://" + href
                    href_l = href.lower().strip()

            if site_type == "hensoldt":
                if "/job/" in href_l:
                    if href.startswith("//"):
                        href = "https:" + href
                    links.append(href if allow_external else urljoin(base_url, href))
                continue

            if patterns and any(p.search(href) for p in patterns):
                # Skip pure filter/search links
                if "?" in href_l and not ("ashby_jid=" in href_l or allow_query_links):
                    continue
                # Skip incomplete job URLs like /jobs/12345- (missing slug)
                if re.search(r"/jobs/\d+-?$", href_l):
                    continue
                # Skip base listing pages
                if href_l.rstrip("/").endswith("/career/jobs"):
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                if not href.startswith(("http://", "https://")):
                    href = urljoin(base_url, href)
                links.append(href if allow_external else urljoin(base_url, href))
                continue

            keyword_hit = any(k in text for k in keywords) or any(k in href_l for k in keywords)
            if not keyword_hit:
                if not require_keyword and patterns:
                    # If keyword not required, we already handled pattern matches above
                    continue
                continue

            # Require job-like URL patterns to avoid nav links
            if re.search(r"/job/|jobid|jr_|job-|/stellen/|/career/jobs/[^/?#]+", href_l, re.IGNORECASE):
                if href.startswith("//"):
                    href = "https:" + href
                links.append(href if allow_external else urljoin(base_url, href))
        # Ashby embeds sometimes store job IDs in data attributes
        if site_type == "ashby" and allow_query_links:
            try:
                jids = page.eval_on_selector_all(
                    "[data-ashby-job-id], [data-job-id], [data-ashby-id]",
                    "els => els.map(e => e.getAttribute('data-ashby-job-id') || e.getAttribute('data-job-id') || e.getAttribute('data-ashby-id')).filter(Boolean)"
                )
                for jid in jids or []:
                    links.append(urljoin(base_url, f"/career?ashby_jid={jid}"))
            except Exception:
                pass
            try:
                # Ashby cards often render titles inside anchors
                hrefs = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.filter(a => a.querySelector('.ashby-job-posting-brief-title')).map(a => a.getAttribute('href')).filter(Boolean)"
                )
                for href in hrefs or []:
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        href = urljoin(base_url, href)
                    links.append(href)
            except Exception:
                pass
            try:
                # Some Ashby embeds render only titles; use count as signal
                title_count = page.locator(".ashby-job-posting-brief-title").count()
                if title_count and not links:
                    print(f"         • Ashby titles found: {title_count} (no links yet)")
            except Exception:
                pass
            # If Ashby content is inside an iframe, collect from frames too
            try:
                for frame in page.frames:
                    try:
                        frame_url = (frame.url or "").lower()
                        if "ashby" not in frame_url:
                            # Still try if it contains Ashby titles
                            if frame.query_selector(".ashby-job-posting-brief-title") is None:
                                continue
                        jids = frame.eval_on_selector_all(
                            "[data-ashby-job-id], [data-job-id], [data-ashby-id]",
                            "els => els.map(e => e.getAttribute('data-ashby-job-id') || e.getAttribute('data-job-id') || e.getAttribute('data-ashby-id')).filter(Boolean)"
                        )
                        for jid in jids or []:
                            links.append(urljoin(base_url, f"/career?ashby_jid={jid}"))
                        hrefs = frame.eval_on_selector_all(
                            "a[href]",
                            "els => els.filter(a => a.querySelector('.ashby-job-posting-brief-title')).map(a => a.getAttribute('href')).filter(Boolean)"
                        )
                        for href in hrefs or []:
                            if href.startswith("//"):
                                href = "https:" + href
                            elif href.startswith("/"):
                                href = urljoin(base_url, href)
                            links.append(href)
                        try:
                            html_f = frame.content()
                            for jid in re.findall(r"\"jobId\"\\s*:\\s*\"([a-f0-9-]{36})\"", html_f, re.IGNORECASE):
                                links.append(urljoin(base_url, f"/career?ashby_jid={jid}"))
                        except Exception:
                            pass
                    except Exception:
                        continue
            except Exception:
                pass

        # Workday: job cards use specific anchors; avoid generic "/careers/job/" base link
        if site_type == "workday":
            try:
                wd_links = page.eval_on_selector_all(
                    "a[data-automation-id='jobTitle'], a[href*='/careers/job/']",
                    "els => els.map(a => a.getAttribute('href')).filter(Boolean)"
                )
                # Infer site path (e.g., /en-US/Airbus) for relative Workday links
                site_path = ""
                try:
                    if site_id:
                        site_path = f"/en-US/{site_id}"
                    else:
                        parsed_page = urlparse(base_url)
                        parts = [p for p in parsed_page.path.split("/") if p]
                        if parts:
                            site_path = f"/en-US/{parts[0]}"
                except Exception:
                    site_path = ""
                for href in wd_links or []:
                    href = href.strip()
                    if not href or href.endswith("/careers/job/"):
                        continue
                    if href.startswith("http"):
                        # Normalize Workday absolute links missing /en-US/{siteId}
                        try:
                            parsed = urlparse(href)
                            if "/job/" in parsed.path and "/en-US/" not in parsed.path and site_path:
                                href = f"{parsed.scheme}://{parsed.netloc}{site_path}{parsed.path}"
                        except Exception:
                            pass
                    elif href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/en-US/"):
                        href = urljoin(base_url, href)
                    elif href.startswith("/"):
                        if href.startswith("/job/") and site_path:
                            href = urljoin(parsed_page.scheme + "://" + parsed_page.netloc + site_path, href)
                        else:
                            href = urljoin(base_url, href)
                    links.append(href)
                if links:
                    return list(dict.fromkeys(links))
            except Exception:
                pass

        # Reinhausen: prefer visible result links only
        if site_type == "reinhausen":
            try:
                visible_links = []
                for a in anchors:
                    try:
                        href = (a.get_attribute("href") or "").strip()
                        if not href:
                            continue
                        if "job.html" not in href.lower():
                            continue
                        if not a.is_visible():
                            continue
                        if href.startswith("//"):
                            href = "https:" + href
                        elif href.startswith("/") and base_url:
                            href = urljoin(base_url, href)
                        elif not href.startswith(("http://", "https://")) and "." in href:
                            href = "https://" + href
                        visible_links.append(href)
                    except Exception:
                        continue
                if visible_links:
                    return list(dict.fromkeys(visible_links))
            except Exception:
                pass

        # Fallback: regex scan page HTML for job URLs
        try:
            html = page.content()
            if patterns:
                for p in patterns:
                    for m in p.findall(html):
                        if isinstance(m, tuple):
                            m = m[0]
                        if isinstance(m, str):
                            m = m.strip()
                            if m.startswith("//"):
                                m = "https:" + m
                            elif m.startswith("/"):
                                m = urljoin(base_url, m)
                        links.append(m)
            # Ashby job links (query-based)
            for m in re.findall(r"https?://[^\"'\\s]+ashby_jid=[^\"'\\s]+", html, re.IGNORECASE):
                links.append(m)
            for m in re.findall(r"/career\\?ashby_jid=[^\"'\\s]+", html, re.IGNORECASE):
                links.append(urljoin(base_url, m))
            # Ashby job IDs embedded in JSON/attributes
            for jid in re.findall(r"ashby_jid(?:=|\"?\\s*:\\s*\")([a-z0-9-]{10,})", html, re.IGNORECASE):
                links.append(urljoin(base_url, f"/career?ashby_jid={jid}"))
            for jid in re.findall(r"\"jobId\"\\s*:\\s*\"([a-f0-9-]{36})\"", html, re.IGNORECASE):
                links.append(urljoin(base_url, f"/career?ashby_jid={jid}"))
            for jid in re.findall(r"\"id\"\\s*:\\s*\"([a-f0-9-]{36})\"\\s*,\\s*\"title\"", html, re.IGNORECASE):
                links.append(urljoin(base_url, f"/career?ashby_jid={jid}"))
            # Absolute URL fallback for Kontron-like pages
            for m in re.findall(r"https?://[^\"'\\s]+/stellenangebote/\\d+", html, re.IGNORECASE):
                links.append(m)
        except Exception:
            pass

        # Final cleanup: drop incomplete job URLs like /jobs/12345-
        cleaned = []
        for link in links:
            if not link:
                continue
            link_l = link.lower().strip()
            if re.search(r"/jobs/\d+-?$", link_l):
                continue
            cleaned.append(link)

        return list(dict.fromkeys(cleaned))

    def _extract_app_data_json(self, html: str) -> Optional[dict]:
        """Extract App.Data JSON object from FERCHAU HTML."""
        match = re.search(r"App\.Data\s*=", html)
        if not match:
            return None
        start = html.find("{", match.start())
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(html)):
            ch = html[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i+1])
                    except Exception:
                        return None
        return None

    def _scrape_ferchau_playwright(self, page, base_url: str, max_pages: int = 10) -> List[Dict]:
        """Fetch FERCHAU listings via HTML App.Data JSON (offset/limit paging) using Playwright."""
        jobs = []
        session = requests.Session()
        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query)
        offset = int(qs.get("offset", [0])[0] or 0)
        limit = int(qs.get("limit", [25])[0] or 25)

        page_count = 0
        while page_count < max_pages:
            qs["offset"] = [str(offset)]
            qs["limit"] = [str(limit)]
            url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                html = page.content()
                data = self._extract_app_data_json(html)
                if not data:
                    break
                offers = data.get("ControllerResponse", {}).get("Offers", [])
                offers_count = data.get("ControllerResponse", {}).get("OffersCount", {})
                total = int(offers_count.get("total", 0) or 0)
                if not offers:
                    break

                for offer in offers:
                    slug = offer.get("slug") or ""
                    if not slug:
                        continue
                    link = urljoin("https://touch.ferchau.com", slug)
                    job = self._extract_job_details(session, link)
                    if job:
                        jobs.append(job)

                offset += limit
                page_count += 1
                if offset >= total:
                    break
            except Exception:
                break

        return jobs

    def _scroll_to_load(self, page, max_scrolls: int = 8) -> None:
        """Scroll to load lazy content (infinite lists)."""
        try:
            last_height = page.evaluate("document.body.scrollHeight")
            for _ in range(max_scrolls):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height
        except Exception:
            return

    def _expand_hensoldt_results(self, page, max_clicks: int = 10) -> None:
        """Click 'More Search Results' until no more results are loaded."""
        try:
            for _ in range(max_clicks):
                btn = page.locator("button#tile-more-results, #tile-more-results")
                if btn.count() == 0:
                    break
                try:
                    if not btn.first.is_visible():
                        break
                except Exception:
                    pass
                try:
                    prev_count = page.locator("a[href*='/job/']").count()
                except Exception:
                    prev_count = 0
                try:
                    show_loc = page.locator("text=/Showing\\s+\\d+\\s+to\\s+\\d+\\s+of\\s+\\d+/i")
                    if show_loc.count() > 0:
                        print(f"     • Hensoldt results text: {show_loc.first.inner_text().strip()}")
                except Exception:
                    pass
                try:
                    try:
                        btn.first.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    btn.first.click(timeout=2000)
                except Exception:
                    break
                # Wait for new results to load
                try:
                    page.wait_for_function(
                        "(prev) => document.querySelectorAll(\"a[href*='/job/']\").length > prev",
                        prev_count,
                        timeout=8000
                    )
                except Exception:
                    pass
                page.wait_for_timeout(1000)
        except Exception:
            return

    def _extract_rohde_links_from_json(self, data) -> List[str]:
        """Extract Rohde & Schwarz job detail links from JSON-like data."""
        links = []
        try:
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for v in cur.values():
                        stack.append(v)
                elif isinstance(cur, list):
                    stack.extend(cur)
                elif isinstance(cur, str):
                    if "stellenangebot-detailansicht_" in cur and cur.endswith(".html"):
                        links.append(cur)
        except Exception:
            return []
        return links

    def _find_total_in_json(self, data) -> Optional[int]:
        """Best-effort extract total count from JSON."""
        try:
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for k, v in cur.items():
                        if isinstance(v, (int, float)) and str(k).lower() in [
                            "total", "totalcount", "count", "numberofresults", "totalresults"
                        ]:
                            return int(v)
                        stack.append(v)
                elif isinstance(cur, list):
                    stack.extend(cur)
        except Exception:
            return None
        return None

    def _collect_rohde_api_links(self, page, base_url: str, max_pages: int = 5) -> List[str]:
        """Attempt to fetch Rohde & Schwarz jobs via JSON API responses."""
        payloads = []

        def _on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "").lower()
                if "json" not in ct and not resp.url.endswith(".json"):
                    return
                data = resp.json()
                try:
                    print(f"     • Rohde API response: {resp.url}")
                except Exception:
                    pass
                payloads.append((resp.url, data))
            except Exception:
                pass

        try:
            page.on("response", _on_response)
            # Try to trigger XHR by clicking the next arrow (page is already loaded)
            try:
                page.evaluate(
                    """() => {
                        const btn = document.querySelector('.slick-next.page-arrow-next, .slick-next.slick-arrow, [aria-label*="Nächster"]');
                        if (btn && !btn.classList.contains('slick-disabled')) btn.click();
                    }"""
                )
            except Exception:
                pass
            page.wait_for_timeout(2000)
        except Exception:
            pass

        # Extract links from any captured JSON payloads
        links = []
        for url, data in payloads:
            for l in self._extract_rohde_links_from_json(data):
                links.append(l)

        # Try to paginate if we can infer paging params from the JSON URL
        if payloads:
            seed_url, seed_data = payloads[0]
            parsed = urlparse(seed_url)
            qs = parse_qs(parsed.query)
            page_param = None
            for key in ["page", "p", "pageNumber"]:
                if key in qs:
                    page_param = key
                    break
            offset_param = None
            for key in ["start", "offset", "from"]:
                if key in qs:
                    offset_param = key
                    break
            size_param = None
            for key in ["size", "limit", "rows", "pageSize"]:
                if key in qs:
                    size_param = key
                    break

            total = self._find_total_in_json(seed_data)
            page_size = None
            if size_param and qs.get(size_param):
                try:
                    page_size = int(qs[size_param][0])
                except Exception:
                    page_size = None
            if not page_size:
                page_size = max(1, len(self._extract_rohde_links_from_json(seed_data)))

            session = requests.Session()
            headers = self._get_headers()

            if page_param:
                try:
                    current_page = int(qs[page_param][0])
                except Exception:
                    current_page = 1
                for _ in range(1, max_pages):
                    current_page += 1
                    qs[page_param] = [str(current_page)]
                    if size_param and page_size:
                        qs[size_param] = [str(page_size)]
                    next_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
                    try:
                        resp = session.get(next_url, headers=headers, timeout=20)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception:
                        break
                    new_links = self._extract_rohde_links_from_json(data)
                    if not new_links:
                        break
                    links.extend(new_links)
                    if total and len(set(links)) >= total:
                        break
            elif offset_param:
                try:
                    current_offset = int(qs[offset_param][0])
                except Exception:
                    current_offset = 0
                for _ in range(1, max_pages):
                    current_offset += page_size
                    qs[offset_param] = [str(current_offset)]
                    if size_param and page_size:
                        qs[size_param] = [str(page_size)]
                    next_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
                    try:
                        resp = session.get(next_url, headers=headers, timeout=20)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception:
                        break
                    new_links = self._extract_rohde_links_from_json(data)
                    if not new_links:
                        break
                    links.extend(new_links)
                    if total and len(set(links)) >= total:
                        break

        # Normalize to absolute URLs
        normalized = []
        for l in links:
            if not l:
                continue
            if not l.startswith(("http://", "https://")):
                l = urljoin(base_url, l)
            normalized.append(l)
        return list(dict.fromkeys(normalized))

    def _collect_rohde_requests_links(self, base_url: str, max_pages: int = 5) -> List[str]:
        """Fetch Rohde & Schwarz listings via direct HTML requests using page= param."""
        links = []
        try:
            parsed = urlparse(base_url)
            qs = parse_qs(parsed.query)
            # Ensure page param exists
            if "page" not in qs:
                qs["page"] = ["1"]
            try:
                start_page = int(qs["page"][0])
            except Exception:
                start_page = 1

            session = requests.Session()
            headers = self._get_headers()
            seen = set()

            for i in range(max_pages):
                qs["page"] = [str(start_page + i)]
                url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
                try:
                    resp = session.get(url, headers=headers, timeout=20)
                    resp.raise_for_status()
                except Exception:
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                page_links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "stellenangebot-detailansicht_" not in href:
                        continue
                    # Skip non-slug variants
                    if "/stellenangebote/stellenangebot-detailansicht_" in href:
                        continue
                    page_links.append(urljoin(url, href))

                # Stop if no new links
                new_links = [l for l in page_links if l not in seen]
                if not new_links:
                    break
                seen.update(new_links)
                links.extend(new_links)
        except Exception:
            return []

        return list(dict.fromkeys(links))


    def _apply_filters_playwright(self, page, filters: dict) -> None:
        """Best-effort filter application using labels/placeholders."""
        if not filters:
            return
        for label, values in filters.items():
            if not isinstance(values, list):
                values = [values]
            try:
                # Try label-based input
                locator = page.get_by_label(label)
                if locator.count() == 0:
                    locator = page.get_by_placeholder(label)
            except Exception:
                locator = None

            for val in values:
                if not val:
                    continue
                try:
                    if locator and locator.count() > 0:
                        locator.first.fill(str(val))
                        locator.first.press("Enter")
                        page.wait_for_timeout(500)
                        # Try selecting dropdown option if present
                        try:
                            opt = page.get_by_role("option", name=str(val))
                            if opt.count() > 0:
                                opt.first.click(timeout=1000)
                        except Exception:
                            pass
                    else:
                        # Fallback: try clicking a filter option by text
                        opt = page.get_by_text(str(val), exact=False)
                        if opt.count() > 0:
                            opt.first.click(timeout=1000)
                except Exception:
                    continue

    def _apply_filters_valentum(self, page, filters: dict) -> None:
        """Valentum custom dropdowns + search input."""
        # Tactics: open dropdown by label text, click option text, fill search input, click search.
        try:
            tf_values = filters.get("tÃ¤tigkeitsfeld") or filters.get("taetigkeitsfeld") or []
            if tf_values:
                try:
                    page.get_by_role("button", name=re.compile("TÃ¤tigkeitsfeld", re.IGNORECASE)).click(timeout=2000)
                except Exception:
                    page.get_by_text(re.compile("TÃ¤tigkeitsfeld", re.IGNORECASE)).click(timeout=2000)
                for val in tf_values:
                    try:
                        label = page.locator("label", has_text=re.compile(str(val), re.IGNORECASE))
                        if label.count() > 0:
                            try:
                                cb = label.first.locator("input[type='checkbox']")
                                if cb.count() > 0:
                                    cb.first.check()
                                else:
                                    label.first.click(timeout=2000)
                            except Exception:
                                label.first.click(timeout=2000)
                        else:
                            page.get_by_text(re.compile(str(val), re.IGNORECASE)).click(timeout=2000)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            region_values = filters.get("region") or []
            if region_values:
                try:
                    page.get_by_role("button", name=re.compile("Region", re.IGNORECASE)).click(timeout=2000)
                except Exception:
                    page.get_by_text(re.compile("Region", re.IGNORECASE)).click(timeout=2000)
                for val in region_values:
                    try:
                        label = page.locator("label", has_text=re.compile(str(val), re.IGNORECASE))
                        if label.count() > 0:
                            try:
                                cb = label.first.locator("input[type='checkbox']")
                                if cb.count() > 0:
                                    cb.first.check()
                                else:
                                    label.first.click(timeout=2000)
                            except Exception:
                                label.first.click(timeout=2000)
                        else:
                            page.get_by_text(re.compile(str(val), re.IGNORECASE)).click(timeout=2000)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            search_terms = filters.get("suchbegriff") or filters.get("suche") or []
            if search_terms:
                term = search_terms[0]
                page.get_by_placeholder(re.compile("Suchbegriff", re.IGNORECASE)).fill(str(term))
        except Exception:
            pass

        try:
            page.get_by_role("button", name=re.compile("Suchen", re.IGNORECASE)).click(timeout=2000)
        except Exception:
            pass

    def _apply_filters_reinhausen(self, page, filters: dict) -> None:
        """Reinhausen filter panel with dropdowns for Land/Ort/Funktion."""
        def _select_by_label(label_text: str, value: str) -> None:
            # Try combobox/select by accessible name
            try:
                combo = page.get_by_role("combobox", name=re.compile(label_text, re.IGNORECASE))
                if combo.count() > 0:
                    try:
                        combo.first.select_option(label=value)
                        return
                    except Exception:
                        combo.first.click(timeout=2000)
            except Exception:
                pass
            # Try select element near label text
            try:
                label = page.get_by_text(re.compile(label_text, re.IGNORECASE)).first
                select = label.locator("xpath=following::select[1]")
                if select.count() > 0:
                    select.first.select_option(label=value)
                    return
            except Exception:
                pass
            # Fallback: click dropdown and option by text
            try:
                label = page.get_by_text(re.compile(label_text, re.IGNORECASE)).first
                field = label.locator("xpath=following::*[self::div or self::span][contains(@class,'select') or contains(@class,'dropdown')][1]")
                if field.count() > 0:
                    field.first.click(timeout=2000)
                page.get_by_role("option", name=re.compile(re.escape(value), re.IGNORECASE)).first.click(timeout=2000)
                return
            except Exception:
                pass
            # Last resort: click any option text
            try:
                page.get_by_text(re.compile(re.escape(value), re.IGNORECASE)).first.click(timeout=2000)
            except Exception:
                pass

        try:
            for key, values in filters.items():
                if not values:
                    continue
                val = values[0]
                _select_by_label(key, val)
                page.wait_for_timeout(800)
        except Exception:
            pass

    def _next_page_playwright(self, page, base_url: str) -> Optional[str]:
        # rel=next
        try:
            rel = page.query_selector("a[rel='next']")
            if rel:
                href = rel.get_attribute("href")
                if href:
                    return urljoin(base_url, href)
        except Exception:
            pass

        # common next labels
        next_labels = ["next", "weiter", "nÃ¤chste", ">"]
        try:
            anchors = page.query_selector_all("a[href]")
            for a in anchors:
                label = (a.inner_text() or "").strip().lower()
                if label in next_labels:
                    href = a.get_attribute("href")
                    if href:
                        return urljoin(base_url, href)
        except Exception:
            pass
        return None

    def _next_page_siemens(self, page) -> Optional[str]:
        """Siemens (Avature) uses pagination links with folderOffset param."""
        try:
            # Prefer explicit "Next" pagination link
            loc = page.locator("a[aria-label*='Next Page'], a:has-text('Next')")
            if loc.count() > 0:
                for i in range(min(loc.count(), 3)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            href = el.get_attribute("href")
                            if href:
                                return href
                    except Exception:
                        continue
            # Fallback: next pagination item by class
            loc = page.locator(".list-controls__pagination__item.paginationNextLink a[href]")
            if loc.count() > 0:
                href = loc.first.get_attribute("href")
                if href:
                    return href
        except Exception:
            pass
        # Fallback: compute from folderOffset + records per page + total
        try:
            html = page.content()
            total = None
            m = re.search(r"of\\s+(\\d+)\\s+results", html, re.IGNORECASE)
            if m:
                total = int(m.group(1))
            if total is None:
                m = re.search(r"aria-label=\\\"(\\d+) results\\\"", html, re.IGNORECASE)
                if m:
                    total = int(m.group(1))
            parsed = urlparse(page.url)
            qs = parse_qs(parsed.query)
            offset = int(qs.get("folderOffset", [0])[0] or 0)
            per_page = int(qs.get("folderRecordsPerPage", [0])[0] or 0)
            if per_page <= 0:
                m = re.search(r"jobRecordsPerPage\"\\s*:\\s*\"?(\\d+)\"?", html, re.IGNORECASE)
                if m:
                    per_page = int(m.group(1))
            if total is not None and per_page > 0 and offset + per_page < total:
                qs["folderOffset"] = [str(offset + per_page)]
                qs["folderRecordsPerPage"] = [str(per_page)]
                new_query = urlencode(qs, doseq=True)
                return urlunparse(parsed._replace(query=new_query))
        except Exception:
            pass
        # Fallback: parse pagination links and pick next higher folderOffset
        try:
            html = page.content()
            parsed = urlparse(page.url)
            qs = parse_qs(parsed.query)
            current_offset = int(qs.get("folderOffset", [0])[0] or 0)
            offsets = []
            for m in re.findall(r"folderOffset=(\\d+)", html, re.IGNORECASE):
                try:
                    offsets.append(int(m))
                except Exception:
                    pass
            offsets = sorted(set(offsets))
            for off in offsets:
                if off > current_offset:
                    qs["folderOffset"] = [str(off)]
                    if "folderRecordsPerPage" not in qs:
                        qs["folderRecordsPerPage"] = ["6"]
                    new_query = urlencode(qs, doseq=True)
                    return urlunparse(parsed._replace(query=new_query))
        except Exception:
            pass
        # Final fallback: increment offset by records per page
        try:
            parsed = urlparse(page.url)
            qs = parse_qs(parsed.query)
            offset = int(qs.get("folderOffset", [0])[0] or 0)
            per_page = int(qs.get("folderRecordsPerPage", [6])[0] or 6)
            qs["folderOffset"] = [str(offset + per_page)]
            qs["folderRecordsPerPage"] = [str(per_page)]
            new_query = urlencode(qs, doseq=True)
            return urlunparse(parsed._replace(query=new_query))
        except Exception:
            pass
        return None

    def _next_page_workday(self, page, current_url: str) -> Optional[str]:
        """Workday pagination via next button or page param."""
        try:
            loc = page.locator("a[data-automation-id='paginationViewNextButton']")
            if loc.count() > 0:
                href = loc.first.get_attribute("href")
                if href:
                    return href
        except Exception:
            pass
        try:
            # Sometimes it's a button without href; click and read URL
            loc = page.locator("button[data-automation-id='paginationViewNextButton'], button[aria-label*='Next'], button:has-text('Next')")
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000)
                page.wait_for_timeout(1500)
                return page.url
        except Exception:
            pass
        try:
            # Workday often uses numbered pagination; click the next number after aria-current
            next_loc = page.locator(
                "xpath=(//*[@aria-current='page']/parent::*/following-sibling::*//button | "
                "//*[@aria-current='page']/following-sibling::button | "
                "//*[@aria-current='page']/parent::*/following-sibling::*//a | "
                "//*[@aria-current='page']/following-sibling::a)[1]"
            )
            if next_loc.count() > 0 and next_loc.first.is_visible():
                next_loc.first.click(timeout=2000)
                page.wait_for_timeout(1500)
                return page.url
        except Exception:
            pass
        try:
            # Fallback: click right-arrow or ">" style control
            loc = page.locator("button:has-text('›'), button:has-text('>'), a:has-text('›'), a:has-text('>')")
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000)
                page.wait_for_timeout(1500)
                return page.url
        except Exception:
            pass
        return None

    def _advance_workday_page(self, page, target_page: Optional[int] = None) -> bool:
        """Advance Workday pagination in-place (no URL change)."""
        try:
            first_href = None
            try:
                first_href = page.locator("a[data-automation-id='jobTitle']").first.get_attribute("href")
            except Exception:
                first_href = None

            # If a target page is given, try clicking it directly
            if target_page is not None:
                try:
                    target_label = f"page {int(target_page)}"
                    clicked = page.evaluate(
                        """(label) => {
                            const btns = Array.from(document.querySelectorAll("button[data-uxi-widget-type='paginationPageButton']"));
                            const target = btns.find(b => (b.getAttribute('aria-label') || '').toLowerCase() === label);
                            if (target) { target.scrollIntoView({block:'center'}); target.click(); return true; }
                            return false;
                        }""",
                        target_label.lower()
                    )
                    if clicked:
                        page.wait_for_timeout(1500)
                        try:
                            page.wait_for_function(
                                """(label) => {
                                    const cur = document.querySelector("button[data-uxi-widget-type='paginationPageButton'][aria-current='page']");
                                    const curLabel = (cur && cur.getAttribute('aria-label')) || '';
                                    return curLabel.toLowerCase() === label;
                                }""",
                                target_label.lower(),
                                timeout=8000
                            )
                            return True
                        except Exception:
                            # If aria-current didn't update, still continue optimistically
                            return True
                except Exception:
                    pass

            # Workday (Airbus) pagination buttons
            try:
                current_btn = page.locator("button[data-uxi-widget-type='paginationPageButton'][aria-current='page']")
                if current_btn.count() > 0:
                    label = (current_btn.first.get_attribute("aria-label") or "").strip()
                    curr_num = None
                    m = re.search(r"\bpage\s+(\d+)\b", label, re.IGNORECASE)
                    if m:
                        curr_num = int(m.group(1))
                    else:
                        # Fallback to button text
                        try:
                            t = (current_btn.first.inner_text() or "").strip()
                            if t.isdigit():
                                curr_num = int(t)
                        except Exception:
                            curr_num = None
                    if curr_num is not None:
                        next_label = f"page {curr_num + 1}"
                        next_btn = page.locator(
                            f"button[data-uxi-widget-type='paginationPageButton'][aria-label='{next_label}']"
                        )
                        if next_btn.count() > 0 and next_btn.first.is_visible():
                            next_btn.first.click(timeout=2000)
                            page.wait_for_timeout(1500)
                            try:
                                page.wait_for_function(
                                    """(prev) => {
                                        const a = document.querySelector("a[data-automation-id='jobTitle']");
                                        if (!a) return false;
                                        const href = a.getAttribute("href") || "";
                                        return href && href !== prev;
                                    }""",
                                    first_href or "",
                                    timeout=8000
                                )
                                return True
                            except Exception:
                                return False
            except Exception:
                pass

            candidates = [
                "a[data-automation-id='paginationViewNextButton']",
                "button[data-automation-id='paginationViewNextButton']",
                "button[aria-label*='Next']",
                "a[aria-label*='Next']",
                "button:has-text('›')",
                "button:has-text('>')",
                "a:has-text('›')",
                "a:has-text('>')",
            ]
            for sel in candidates:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(1500)
                    # Wait for list to change
                    try:
                        page.wait_for_function(
                            """(prev) => {
                                const a = document.querySelector("a[data-automation-id='jobTitle']");
                                if (!a) return false;
                                const href = a.getAttribute("href") || "";
                                return href && href !== prev;
                            }""",
                            first_href or "",
                            timeout=8000
                        )
                        return True
                    except Exception:
                        # If href didn't change, treat as not advanced
                        return False

            # Last resort: click the button containing the Workday right-chevron SVG
            try:
                clicked = page.evaluate(
                    """() => {
                        const svg = document.querySelector("svg.wd-icon-chevron-right-small");
                        if (!svg) return false;
                        let el = svg;
                        for (let i = 0; i < 5; i++) {
                            if (!el) break;
                            if (el.tagName === "BUTTON" || el.tagName === "A") {
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                return true;
                            }
                            el = el.parentElement;
                        }
                        return false;
                    }"""
                )
                if clicked:
                    page.wait_for_timeout(1500)
                    try:
                        page.wait_for_function(
                            """(prev) => {
                                const a = document.querySelector("a[data-automation-id='jobTitle']");
                                if (!a) return false;
                                const href = a.getAttribute("href") || "";
                                return href && href !== prev;
                            }""",
                            first_href or "",
                            timeout=8000
                        )
                        return True
                    except Exception:
                        return False
            except Exception:
                pass
            # Try numbered pagination: click next sibling after aria-current
            try:
                next_loc = page.locator(
                    "xpath=(//*[@aria-current='page']/parent::*/following-sibling::*//button | "
                    "//*[@aria-current='page']/following-sibling::button | "
                    "//*[@aria-current='page']/parent::*/following-sibling::*//a | "
                    "//*[@aria-current='page']/following-sibling::a)[1]"
                )
                if next_loc.count() > 0 and next_loc.first.is_visible():
                    next_loc.first.click(timeout=2000)
                    page.wait_for_timeout(1500)
                    try:
                        page.wait_for_function(
                            """(prev) => {
                                const a = document.querySelector("a[data-automation-id='jobTitle']");
                                if (!a) return false;
                                const href = a.getAttribute("href") || "";
                                return href && href !== prev;
                            }""",
                            first_href or "",
                            timeout=8000
                        )
                        return True
                    except Exception:
                        return False
            except Exception:
                pass
        except Exception:
            return False
        return False

    def _collect_workday_api_links(self, page, page_url: str, site_id: str = "") -> List[str]:
        """Fetch Workday CXS jobs API and extract all job links."""
        links: List[str] = []
        try:
            html = page.content()
        except Exception:
            return links

        endpoint = None
        try:
            m = re.search(r"https?://[^\"']+/wday/cxs/[^\"']+/jobs", html)
            if m:
                endpoint = m.group(0)
            else:
                m = re.search(r"(/wday/cxs/[^\"']+/jobs)", html)
                if m:
                    endpoint = urljoin(page_url, m.group(1))
        except Exception:
            endpoint = None

        # If endpoint not in HTML, construct from window.workday config
        if not endpoint:
            try:
                tenant_m = re.search(r"tenant:\s*\"([^\"]+)\"", html)
                site_m = re.search(r"siteId:\s*\"([^\"]+)\"", html)
                if tenant_m and site_m:
                    base = urlparse(page_url).scheme + "://" + urlparse(page_url).netloc
                    endpoint = f"{base}/wday/cxs/{tenant_m.group(1)}/{site_m.group(1)}/jobs"
            except Exception:
                endpoint = None

        if not endpoint:
            print("     ⚠ Workday API endpoint not found in page HTML/config.")
            return links

        # Build payload from URL params (q, locationCountry, jobFamilyGroup, timeType, etc.)
        parsed = urlparse(page_url)
        qs = parse_qs(parsed.query)
        search_text = (qs.get("q") or [""])[0]
        applied = {}
        for key in ["locationCountry", "jobFamilyGroup", "timeType", "locationRegion", "locationHierarchy2", "locationHierarchy3"]:
            vals = qs.get(key)
            if vals:
                applied[key] = vals

        # Try paging via offset/limit using POST (Workday CXS)
        parsed_page = urlparse(page_url)
        host = parsed_page.scheme + "://" + parsed_page.netloc
        # Workday external job pages typically include /en-US/{siteId}
        site_path = ""
        try:
            if site_id:
                site_path = f"/en-US/{site_id}"
            else:
                site_m = re.search(r"siteId:\s*\"([^\"]+)\"", html)
                if site_m:
                    site_path = f"/en-US/{site_m.group(1)}"
        except Exception:
            site_path = ""
        if not site_path:
            # Fallback: infer siteId from URL path (e.g., /Airbus)
            try:
                parts = [p for p in parsed_page.path.split("/") if p]
                if parts:
                    site_path = f"/en-US/{parts[0]}"
            except Exception:
                site_path = ""
        seen = set()
        offset = 0
        limit = 50
        for _ in range(10):
            try:
                headers = dict(self._get_headers())
                headers["Accept"] = "application/json"
                headers["Content-Type"] = "application/json"
                headers["Origin"] = host
                headers["Referer"] = page_url

                payloads = []
                payload = {
                    "offset": offset,
                    "limit": limit,
                    "searchText": search_text,
                }
                if applied:
                    payload["appliedFacets"] = applied
                payloads.append(payload)

                # Some Workday tenants expect "count" instead of "limit"
                payload2 = dict(payload)
                payload2.pop("limit", None)
                payload2["count"] = limit
                payloads.append(payload2)

                resp = None
                for pl in payloads:
                    resp = requests.post(endpoint, headers=headers, json=pl, timeout=30)
                    if resp.status_code == 200:
                        break

                if not resp or resp.status_code != 200:
                    status = resp.status_code if resp else "no response"
                    print(f"     ⚠ Workday API status {status} for {endpoint}")
                    try:
                        print(f"     ⚠ Workday API response: {resp.text[:200]}")  # brief
                    except Exception:
                        pass
                    break
                text = resp.text
            except Exception:
                break

            # Extract any Workday job paths from JSON text
            paths = re.findall(r"\"(\/en-US\/[^\"/]+\/job\/[^\"\s]+)\"", text)
            for p in paths:
                if p.startswith("http"):
                    full = p
                elif p.startswith("/en-US/"):
                    full = urljoin(host, p)
                elif p.startswith("/job/") and site_path:
                    full = urljoin(host + site_path, p)
                else:
                    full = urljoin(host, p)
                if full not in seen:
                    seen.add(full)
                    links.append(full)

            # Also try JSON fields if present
            items_count = 0
            total_count = None
            try:
                data = resp.json()
                if isinstance(data, dict):
                    total_count = data.get("total")
                for key in ["jobPostings", "jobs", "searchResults", "items"]:
                    items = data.get(key)
                    if isinstance(items, list):
                        items_count = max(items_count, len(items))
                        for it in items:
                            if not isinstance(it, dict):
                                continue
                            path = it.get("externalPath") or it.get("path") or it.get("url")
                            if isinstance(path, str) and "/job/" in path:
                                if path.startswith("http"):
                                    full = path
                                elif path.startswith("/en-US/"):
                                    full = urljoin(host, path)
                                elif path.startswith("/job/") and site_path:
                                    full = urljoin(host + site_path, path)
                                else:
                                    full = urljoin(host, path)
                                if full not in seen:
                                    seen.add(full)
                                    links.append(full)
            except Exception:
                pass

            if items_count == 0 and not paths:
                break
            # If API reports total, use it to decide when to stop
            try:
                if total_count is not None:
                    total_int = int(total_count)
                    print(f"     • Workday API page: offset={offset}, items={items_count}, total={total_int}")
                    # Some responses return total=0 on subsequent pages; ignore non-positive totals
                    if total_int > 0 and items_count > 0 and (offset + items_count) >= total_int:
                        break
            except Exception:
                pass
            # Advance by actual items returned (fallback to limit)
            offset += items_count if items_count > 0 else limit

        return list(dict.fromkeys(links))

    def _expand_workday_results(self, page) -> None:
        """Workday often loads more jobs on same page via 'Load more'."""
        try:
            # Repeatedly click "Load more" / "Show more"
            for _ in range(10):
                btn = page.locator(
                    "button[data-automation-id='paginationViewMoreButton'], "
                    "button[data-automation-id='moreButton'], "
                    "button:has-text('Load more'), "
                    "button:has-text('Show more')"
                )
                if btn.count() == 0:
                    break
                try:
                    if not btn.first.is_visible():
                        break
                except Exception:
                    pass
                try:
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(1500)
                except Exception:
                    break
        except Exception:
            pass

    def _next_page_ferchau(self, page) -> Optional[str]:
        """Ferchau uses in-page pagination buttons (e.g., '26-35 von 35 >')."""
        try:
            # Prefer API-style paging if OffersCount is available in JS context
            try:
                offers = page.evaluate("""
                    () => {
                        try {
                            const oc = App?.Data?.ControllerResponse?.OffersCount;
                            return oc || null;
                        } catch (e) {
                            return null;
                        }
                    }
                """)
            except Exception:
                offers = None
            if offers and isinstance(offers, dict):
                offset = int(offers.get("offset", 0))
                limit = int(offers.get("limit", 25))
                total = int(offers.get("total", 0))
                print(f"     â€¢ Ferchau offers: offset={offset}, limit={limit}, total={total}")
                if offset + limit < total:
                    parsed = urlparse(page.url)
                    qs = parse_qs(parsed.query)
                    qs["offset"] = [str(offset + limit)]
                    qs["limit"] = [str(limit)]
                    new_query = urlencode(qs, doseq=True)
                    return urlunparse(parsed._replace(query=new_query))
            else:
                try:
                    print("     • Ferchau offers: not found in page JS context")
                except Exception:
                    pass
            # Fallback: click in-page pagination button like "26-35 von 35"
            try:
                text_loc = page.locator("text=/\\d+\\s*-\\s*\\d+\\s*(?:von|of)\\s*\\d+/i")
                if text_loc.count() > 0:
                    t = (text_loc.first.inner_text() or "").strip()
                    try:
                        print(f"     • Ferchau pager text: {t}")
                    except Exception:
                        pass
                    m = re.search(r"(\d+)\s*-\s*(\d+)\s*(?:von|of)\s*([\d,\.]+)", t, re.IGNORECASE)
                    if m:
                        end = int(m.group(2))
                        total = int(re.sub(r"[^\d]", "", m.group(3)))
                        if end < total:
                            # Capture current first job link to detect change after click
                            before_links = []
                            try:
                                before_links = page.eval_on_selector_all(
                                    "a[href*='/de/de/job/']",
                                    "els => els.map(e => e.getAttribute('href') || '').filter(Boolean)"
                                ) or []
                            except Exception:
                                before_links = []
                            try:
                                # Try clicking the element itself
                                try:
                                    text_loc.first.scroll_into_view_if_needed(timeout=2000)
                                except Exception:
                                    pass
                                text_loc.first.click(timeout=2000)
                            except Exception:
                                try:
                                    # Click closest button/anchor if needed
                                    page.evaluate(
                                        "(el) => { const b = el.closest('button,a'); if (b) b.click(); }",
                                        text_loc.first
                                    )
                                except Exception:
                                    pass
                            # Some pages only advance when clicking the arrow icon
                            try:
                                page.evaluate(
                                    """() => {
                                        const btns = Array.from(document.querySelectorAll('button'))
                                          .filter(b => /\\d+\\s*-\\s*\\d+\\s*(von|of)\\s*\\d+/i.test(b.innerText || ''));
                                        if (!btns.length) return;
                                        const btn = btns[0];
                                        const svg = btn.querySelector('svg, path');
                                        if (svg) {
                                          (svg.closest('svg') || svg).dispatchEvent(new MouseEvent('click', {bubbles: true}));
                                        } else {
                                          btn.click();
                                        }
                                    }"""
                                )
                            except Exception:
                                pass
                            # Wait for the job list to change (URL may stay the same)
                            try:
                                page.wait_for_function(
                                    """(prev) => {
                                        const links = Array.from(document.querySelectorAll("a[href*='/de/de/job/']"))
                                          .map(a => a.getAttribute('href') || '')
                                          .filter(Boolean);
                                        if (!links.length) return false;
                                        if (!prev || !prev.length) return false;
                                        if (links.length !== prev.length) return true;
                                        const setPrev = new Set(prev);
                                        return links.some(l => !setPrev.has(l));
                                    }""",
                                    before_links,
                                    timeout=8000
                                )
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            try:
                                page._ferchau_inplace = True
                            except Exception:
                                pass
                            return page.url
                else:
                    try:
                        print("     • Ferchau pager text: not found")
                    except Exception:
                        pass
            except Exception:
                pass

            # Final fallback: increment offset directly from URL
            try:
                parsed = urlparse(page.url)
                qs = parse_qs(parsed.query)
                cur_offset = int(qs.get("offset", [0])[0] or 0)
                cur_limit = int(qs.get("limit", [25])[0] or 25)
                qs["offset"] = [str(cur_offset + cur_limit)]
                qs["limit"] = [str(cur_limit)]
                new_query = urlencode(qs, doseq=True)
                return urlunparse(parsed._replace(query=new_query))
            except Exception:
                pass

            # Try common "next" buttons/links
            candidates = [
                "button:has-text('>')",
                "a:has-text('>')",
                "button[aria-label*='Next']",
                "a[aria-label*='Next']",
                "button[title*='Next']",
                "a[title*='Next']",
                "button[aria-label*='Weiter']",
                "a[aria-label*='Weiter']"
            ]
            for sel in candidates:
                loc = page.locator(sel)
                if loc.count() > 0:
                    # Prefer visible/active
                    for i in range(min(loc.count(), 5)):
                        el = loc.nth(i)
                        try:
                            if el.is_visible():
                                el.click(timeout=2000)
                                page.wait_for_timeout(1500)
                                return page.url
                        except Exception:
                            continue
            # Fallback: button containing "von" (e.g. "26-35 von 35")
            loc = page.locator("button:has-text('von')")
            if loc.count() > 0:
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            el.click(timeout=2000)
                            page.wait_for_timeout(1500)
                            return page.url
                    except Exception:
                        continue
            # Fallback: click last button in pagination-like containers
            for sel in ["[class*='pagination'] button", "[class*='pager'] button", "[class*='pages'] button"]:
                loc = page.locator(sel)
                if loc.count() > 0:
                    el = loc.nth(loc.count() - 1)
                    try:
                        if el.is_visible():
                            el.click(timeout=2000)
                            page.wait_for_timeout(1500)
                            return page.url
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _next_page_rohde_schwarz(self, page) -> Optional[str]:
        """Rohde & Schwarz uses slick slider pagination with in-page arrows."""
        try:
            # Read result range like "Ergebnisse 1 - 30 von 38"
            range_text = ""
            total = None
            end = None
            try:
                range_loc = page.locator("text=/Ergebnisse\\s*\\d+\\s*[-–]\\s*\\d+\\s*von\\s*\\d+/i")
                if range_loc.count() > 0:
                    range_text = (range_loc.first.inner_text() or "").strip()
            except Exception:
                range_text = ""
            if not range_text:
                try:
                    body_text = (page.inner_text("body") or "").replace("\u00a0", " ")
                    m = re.search(r"Ergebnisse\\s*\\d+\\s*[-–]\\s*\\d+\\s*von\\s*\\d+", body_text, re.IGNORECASE)
                    if m:
                        range_text = m.group(0)
                except Exception:
                    range_text = ""
            if range_text:
                m = re.search(r"(\\d+)\\s*[-–]\\s*(\\d+)\\s*von\\s*(\\d+)", range_text)
                if m:
                    end = int(m.group(2))
                    total = int(m.group(3))
                    if end >= total:
                        try:
                            print(f"     • Rohde pager: end={end} total={total} (stop)")
                        except Exception:
                            pass
                        return None
            else:
                try:
                    print("     • Rohde pager: range text not found")
                except Exception:
                    pass

            # Prefer URL-based pagination if page param is supported
            try:
                parsed = urlparse(page.url)
                qs = parse_qs(parsed.query)
                cur_page = 1
                if "page" in qs and qs["page"]:
                    try:
                        cur_page = int(qs["page"][0])
                    except Exception:
                        cur_page = 1
                if total and end and end < total:
                    qs["page"] = [str(cur_page + 1)]
                    new_query = urlencode(qs, doseq=True)
                    next_url = urlunparse(parsed._replace(query=new_query))
                    try:
                        print(f"     • Rohde next url: {next_url}")
                    except Exception:
                        pass
                    return next_url
            except Exception:
                pass

            # Capture current first job link to detect change after click
            before_links = []
            try:
                before_links = page.eval_on_selector_all(
                    "a[href*='stellenangebot-detailansicht_']",
                    "els => els.map(e => e.getAttribute('href') || '').filter(Boolean)"
                ) or []
            except Exception:
                before_links = []

            # Click the "next" arrow (slick)
            try:
                next_btn = page.locator(".slick-next.page-arrow-next, .slick-next.slick-arrow, [aria-label*='Nächster']")
                if next_btn.count() > 0:
                    try:
                        if "slick-disabled" in (next_btn.first.get_attribute("class") or ""):
                            try:
                                print("     • Rohde pager: next disabled")
                            except Exception:
                                pass
                            return None
                    except Exception:
                        pass
                    next_btn.first.scroll_into_view_if_needed(timeout=2000)
                    next_btn.first.click(timeout=2000)
                else:
                    # Try clicking via JS as a fallback
                    clicked = False
                    try:
                        clicked = page.evaluate(
                            """() => {
                                const btn = document.querySelector('.slick-next.page-arrow-next, .slick-next.slick-arrow, [aria-label*="Nächster"]');
                                if (!btn) return false;
                                if (btn.classList.contains('slick-disabled')) return false;
                                btn.click();
                                return true;
                            }"""
                        )
                    except Exception:
                        clicked = False
                    if not clicked:
                        # Try clicking page "2" dot as fallback
                        try:
                            dot = page.locator(".slick-dots li button")
                            if dot.count() >= 2:
                                dot.nth(1).click(timeout=2000)
                            else:
                                clicked2 = False
                                try:
                                    clicked2 = page.evaluate(
                                        """() => {
                                            const buttons = Array.from(document.querySelectorAll('.slick-dots li button'));
                                            const target = buttons.find(b => (b.textContent || '').trim() === '2');
                                            if (!target) return false;
                                            target.click();
                                            return true;
                                        }"""
                                    )
                                except Exception:
                                    clicked2 = False
                                if not clicked2:
                                    try:
                                        print("     • Rohde pager: no next/dot found")
                                    except Exception:
                                        pass
                                    return None
                        except Exception:
                            return None
            except Exception:
                return None

            # Wait for the job list to change (URL may stay the same)
            try:
                page.wait_for_function(
                    """(prev) => {
                        const links = Array.from(document.querySelectorAll("a[href*='stellenangebot-detailansicht_']"))
                          .map(a => a.getAttribute('href') || '')
                          .filter(Boolean);
                        if (!links.length || !prev || !prev.length) return false;
                        if (links.length !== prev.length) return true;
                        const setPrev = new Set(prev);
                        return links.some(l => !setPrev.has(l));
                    }""",
                    before_links,
                    timeout=8000
                )
            except Exception:
                pass
            # Also wait for result range text to change if present
            if range_text:
                try:
                    page.wait_for_function(
                        "(prev) => { const el = Array.from(document.querySelectorAll('*')).find(e => /Ergebnisse\\s+\\d+\\s*-\\s*\\d+\\s+von\\s+\\d+/i.test(e.innerText||'')); return el && (el.innerText||'') !== prev; }",
                        range_text,
                        timeout=8000
                    )
                except Exception:
                    pass
            page.wait_for_timeout(1000)
            try:
                page._rohde_inplace = True
            except Exception:
                pass
            return page.url
        except Exception:
            return None

    def _extract_job_details_playwright(self, page, url: str) -> Optional[Dict]:
        try:
            # Normalize Workday job URLs that miss /en-US/{siteId}
            try:
                parsed = urlparse(url)
                if "myworkdayjobs.com" in parsed.netloc and "/job/" in parsed.path and "/en-US/" not in parsed.path:
                    # Infer siteId from current page if possible
                    site_id = ""
                    try:
                        cur = urlparse(page.url)
                        parts = [p for p in cur.path.split("/") if p]
                        if parts:
                            site_id = parts[0]
                    except Exception:
                        site_id = ""
                    if site_id:
                        url = f"{parsed.scheme}://{parsed.netloc}/en-US/{site_id}{parsed.path}"
            except Exception:
                pass
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("/"):
                # Use current page origin for relative links
                origin = page.url.split("/", 3)[:3]
                base = "/".join(origin)
                url = urljoin(base, url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self._accept_cookies_playwright(page)
            page.wait_for_timeout(1000)

            title = None
            for sel in ["h1", "h2", "h3", "h3.ashby-job-posting-brief-title"]:
                el = page.query_selector(sel)
                if el:
                    t = (el.inner_text() or "").strip()
                    if t:
                        title = t
                        break
            if not title:
                # Fallback: meta title/og:title or document.title
                try:
                    og = page.query_selector("meta[property='og:title']") or page.query_selector("meta[name='og:title']")
                    if og:
                        t = (og.get_attribute("content") or "").strip()
                        if t:
                            title = t
                except Exception:
                    pass
            if not title:
                try:
                    meta = page.query_selector("meta[name='title']") or page.query_selector("meta[property='title']")
                    if meta:
                        t = (meta.get_attribute("content") or "").strip()
                        if t:
                            title = t
                except Exception:
                    pass
            if not title:
                try:
                    t = (page.title() or "").strip()
                    if t:
                        title = t
                except Exception:
                    pass
            if not title:
                print(f"         âš  No title found on job page: {url}")
                return None

            # Grab description text from main/article/body
            desc_el = page.query_selector("main") or page.query_selector("article") or page.query_selector("section") or page.query_selector("body")
            description = ""
            if desc_el:
                description = (desc_el.inner_text() or "").strip()

            return {
                "title": title,
                "company": "Unknown",
                "location": "Germany",
                "url": url,
                "description": description,
                "source": "Company",
                "date_found": datetime.now().isoformat()
            }
        except Exception:
            print(f"         âš  Failed to parse job page: {url}")
            return None

    def _extract_job_details(self, session: requests.Session, url: str) -> Optional[Dict]:
        try:
            resp = session.get(url, headers=self._get_headers(), timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'lxml')

            title = None
            for tag in ['h1', 'h2', 'h3']:
                el = soup.find(tag)
                if el and el.get_text(strip=True):
                    title = el.get_text(strip=True)
                    break

            if not title:
                return None

            container = soup.find(['main', 'article', 'section']) or soup.body
            description = container.get_text(" ", strip=True) if container else ""

            return {
                "title": title,
                "company": "Unknown",
                "location": "Germany",
                "url": url,
                "description": description,
                "source": "Company",
                "date_found": datetime.now().isoformat()
            }
        except Exception:
            return None

    def _score_with_groq(self, description: str, allow_switch: bool = True) -> Optional[Dict]:
        api_key = self.config.get('groq_api_key', '')
        if not api_key:
            return None
        wait_seconds = int(self.config.get('groq_wait_seconds', 60))

        prompt = f"""
You are scoring a job description for match.
Positive keywords (should match): {', '.join(self.include_desc)}
Negative keywords (should penalize): {', '.join(self.exclude_desc)}
Negative title keywords (should penalize if present): {', '.join(self.exclude)}

Score 1-10 (10 = strong match, 1 = wrong field).
Return ONLY JSON:
{{"score": 1-10, "reason": "short reason"}}

JOB DESCRIPTION:
{description[:4000]}
"""
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.2
                },
                timeout=60
            )
            if response.status_code == 429:
                print(f"         âš  Groq rate limit hit. Cooling down for {wait_seconds}s.")
                self._llm_state["groq_cooldown_until"] = time.time() + wait_seconds
                return None

            response.raise_for_status()
            text = response.json()['choices'][0]['message']['content']
            raw_text = text
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0].strip()

            # Fallback: try to extract JSON object from text
            try:
                data = json.loads(text)
            except Exception:
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                else:
                    print(f"         âš  Groq parse error. Raw response: {raw_text[:300]}")
                    return None
            return {
                "score": int(data.get("score", 0)),
                "reason": data.get("reason", "")
            }
        except Exception as e:
            print(f"         âš  Groq request/parse error: {e}")
            return None

    def _score_with_gemini(self, description: str, allow_switch: bool = True) -> Optional[Dict]:
        api_key = self.config.get('gemini_api_key', '')
        if not api_key:
            return None
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = f"""
You are scoring a job description for match.
Positive keywords (should match): {', '.join(self.include_desc)}
Negative keywords (should penalize): {', '.join(self.exclude_desc)}
Negative title keywords (should penalize if present): {', '.join(self.exclude)}

Score 1-10 (10 = strong match, 1 = wrong field).
Return ONLY JSON:
{{"score": 1-10, "reason": "short reason"}}

JOB DESCRIPTION:
{description[:4000]}
"""
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            text = response.text or ""
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0].strip()
            data = json.loads(text)
            return {
                "score": int(data.get("score", 0)),
                "reason": data.get("reason", "")
            }
        except Exception as e:
            print(f"         âš  Gemini request/parse error: {e}")
            if "429" in str(e):
                wait_seconds = int(self.config.get('groq_wait_seconds', 30))
                self._llm_state["gemini_cooldown_until"] = time.time() + wait_seconds
                print(f"         âš  Gemini rate limit hit. Cooling down for {wait_seconds}s.")
            return None

    def _score_with_best_llm(self, description: str) -> Optional[Dict]:
        now = time.time()
        pref = self._llm_state.get("preferred", "groq")
        groq_ready = now >= self._llm_state.get("groq_cooldown_until", 0)
        gemini_ready = now >= self._llm_state.get("gemini_cooldown_until", 0)

        if pref == "groq" and groq_ready:
            result = self._score_with_groq(description, allow_switch=True)
            if result:
                return result
        if pref == "gemini" and gemini_ready:
            result = self._score_with_gemini(description, allow_switch=True)
            if result:
                return result

        # Fallback to whichever is ready
        if groq_ready:
            return self._score_with_groq(description, allow_switch=True)
        if gemini_ready:
            return self._score_with_gemini(description, allow_switch=True)

        # Both cooling down: wait for the shorter one
        wait = min(self._llm_state["groq_cooldown_until"], self._llm_state["gemini_cooldown_until"]) - now
        if wait > 0:
            wait_seconds = int(wait)
            print(f"         âš  Both models cooling down. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
        return self._score_with_groq(description, allow_switch=True) or self._score_with_gemini(description, allow_switch=True)

    def _scrape_company_careers_playwright(self) -> List[Dict]:
        if not sync_playwright:
            print("   âš  Playwright not available. Install it to enable career page scraping.")
            return []

        pages = self._load_career_pages()
        if not pages:
            return []

        jobs = []
        testing_mode = bool(self.config.get("scrape_test_mode", False))
        queue_only = bool(self.config.get("scrape_queue_only", False))
        max_pages = int(self.config.get("company_careers_max_pages", 5))
        output_file = self.config.get("output", {}).get("excel_file", "daily_jobs.csv")
        nonmatch_file = self.config.get("output_nonmatch_file", "daily_jobs_nonmatch.csv")

        def _load_urls(path: str) -> set:
            urls = set()
            if not os.path.exists(path):
                return urls
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = (row.get("URL") or "").strip().lower()
                    if url:
                        urls.add(url)
            return urls

        base_dir = os.path.dirname(os.path.abspath(__file__))
        cache_file = self.config.get("cache_file", "seen_jobs_cache.json")
        cache_path = os.path.join(base_dir, cache_file)
        scored_urls = set()
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                for url, meta in cache.items():
                    if isinstance(meta, dict) and meta.get("score") is not None:
                        scored_urls.add(url.strip().lower())
        except Exception:
            scored_urls = set()
        existing_urls = scored_urls

        headless = bool(self.config.get("company_careers_headless", True))
        print("   â€¢ Starting Playwright...")
        startup_timeout = int(self.config.get("company_careers_startup_timeout", 30))
        with sync_playwright() as p:
            browser = None
            start_ts = time.time()
            try:
                browser = p.chromium.launch(headless=headless)
            except Exception as e:
                print(f"   âš  Playwright launch failed: {e}")
                return []
            if time.time() - start_ts > startup_timeout:
                print(f"   âš  Playwright startup exceeded {startup_timeout}s. Skipping company careers.")
                try:
                    browser.close()
                except Exception:
                    pass
                return []
            print("   â€¢ Playwright ready")
            page = browser.new_page()

            for page_cfg in pages:
                url = page_cfg.get("url")
                company = page_cfg.get("name", "Company")
                page_type = page_cfg.get("type", "page")
                site_type = (page_cfg.get("site_type") or "").strip().lower()
                if not url:
                    continue
                site_start = time.time()
                site_timeout = int(self.config.get("company_careers_site_timeout", 180))
                company_found = 0

                print(f"   â€¢ Opening {company}: {url}")
                if company.startswith("ferchau"):
                    print("     â€¢ Ferchau API mode enabled; using App.Data pagination.")
                    ferchau_jobs = self._scrape_ferchau_playwright(page, url, max_pages=max_pages)
                    if not ferchau_jobs:
                        print("     â€¢ Ferchau API mode returned 0 jobs; falling back to UI pagination.")
                    else:
                        for job in ferchau_jobs:
                            link = (job.get("url", "") or "").strip().lower()
                            if link and link in existing_urls:
                                print(f"         â€¢ Skipping already-seen job: {link}")
                                continue
                            job["company"] = company
                            if testing_mode:
                                jobs.append(job)
                                continue
                            score_data = self._score_with_best_llm(job.get("description", ""))
                            if not score_data:
                                print("         âš  Groq scoring failed (rate limit or parse error)")
                                continue
                            job["score"] = score_data["score"]
                            job["match_reasons"] = score_data["reason"]
                            job["missing_skills"] = ""
                            jobs.append(job)
                        continue
                if company == "bertrandt":
                    # Block heavy assets to avoid timeouts
                    try:
                        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font"] else route.continue_())
                    except Exception:
                        pass
                if company.startswith("ferchau"):
                    print("     â€¢ Ferchau API mode disabled; using standard Playwright link scrape.")
                if page_type == "job":
                    if url and url.strip().lower() in existing_urls:
                        print(f"     â€¢ Skipping already-seen job: {url}")
                        continue
                    job = self._extract_job_details_playwright(page, url)
                    if job:
                        job["company"] = company
                        score_data = self._score_with_best_llm(job.get("description", ""))
                        if score_data:
                            job["score"] = score_data["score"]
                            job["match_reasons"] = score_data["reason"]
                            job["missing_skills"] = ""
                            jobs.append(job)
                    continue

                # Retry opening page (some sites reset connections)
                opened = False
                backoff = self.config.get("company_careers_retry_backoff_seconds", [2, 5, 10])
                for attempt in range(1, 4):
                    try:
                        page.set_extra_http_headers(self._get_headers())
                        if self._goto_with_retries(page, url, company):
                            pass
                        else:
                            raise RuntimeError("All navigation strategies failed")
                        self._accept_cookies_playwright(page)
                        opened = True
                        break
                    except Exception as e:
                        print(f"   âš  Failed to open {url} (attempt {attempt}/3): {e}")
                        self._maybe_screenshot(page, company, f"open_attempt_{attempt}")
                        try:
                            wait_s = backoff[min(attempt - 1, len(backoff) - 1)]
                        except Exception:
                            wait_s = 2
                        page.wait_for_timeout(int(wait_s * 1000))
                if not opened:
                    # Requests fallback for simple pages
                    try:
                        session = requests.Session()
                        html = self._fetch_with_cookie_accept(session, url)
                        if html:
                            soup = BeautifulSoup(html, 'lxml')
                            link_patterns = page_cfg.get("job_link_patterns")
                            links = self._extract_job_links(soup, url)
                            if link_patterns:
                                pats = [re.compile(p, re.IGNORECASE) for p in link_patterns]
                                links = [l for l in links if any(p.search(l) for p in pats)]
                            for l in links:
                                job = {
                                    "title": "Unknown",
                                    "company": company,
                                    "location": "",
                                    "url": l,
                                    "description": "",
                                    "source": "Company",
                                    "date_found": datetime.now().isoformat()
                                }
                                jobs.append(job)
                                self._emit_job(job)
                    except Exception:
                        pass
                    continue

                next_url = url
                visited = set()
                ferchau_signatures = set()
                workday_inplace = False
                workday_signatures = set()
                workday_page_num = 1
                for _ in range(max_pages):
                    if time.time() - site_start > site_timeout:
                        print(f"     âš  {company} timed out after {site_timeout}s. Skipping.")
                        self._log_event("timeout", {
                            "source": "company_careers",
                            "company": company,
                            "url": url,
                            "details": f"Timed out after {site_timeout}s"
                        })
                        self._maybe_screenshot(page, company, "timeout")
                        break
                    if not next_url:
                        break
                    if not (company.startswith("ferchau") or company == "rohde_schwarz"):
                        if next_url in visited:
                            break
                        visited.add(next_url)

                    try:
                        if not (site_type == "workday" and workday_inplace):
                            if (company.startswith("ferchau") and getattr(page, "_ferchau_inplace", False)) or (
                                company == "rohde_schwarz" and getattr(page, "_rohde_inplace", False)
                            ):
                                # Ferchau/Rohde pagination updates in-place without URL change
                                try:
                                    page._ferchau_inplace = False
                                    page._rohde_inplace = False
                                except Exception:
                                    pass
                        else:
                            page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
                        if company == "rohde_schwarz":
                            try:
                                rs_range = page.locator("text=/Ergebnisse\\s+\\d+\\s*-\\s*\\d+\\s+von\\s+\\d+/i")
                                if rs_range.count() > 0:
                                    print(f"     • Rohde results text: {rs_range.first.inner_text().strip()}")
                            except Exception:
                                pass
                            self._accept_cookies_playwright(page)
                            page.wait_for_timeout(1500)
                            self._scroll_to_load(page, int(self.config.get("company_careers_max_scrolls", 8)))
                            if company == "hensoldt":
                                self._expand_hensoldt_results(page, max_clicks=int(self.config.get("company_careers_max_pages", 5)))
                            # Ashby embeds often load asynchronously
                            if site_type == "ashby":
                                try:
                                    page.wait_for_selector(".ashby-job-posting-brief-title, [data-ashby-job-id], [data-job-id], [data-ashby-id]", timeout=15000)
                                except Exception:
                                    pass
                            if site_type == "workday":
                                self._expand_workday_results(page)
                        else:
                            workday_inplace = False
                    except Exception as e:
                        print(f"   âš  Failed page {next_url}: {e}")
                        break

                    filters = page_cfg.get("filters", {})
                    apply_filters = page_cfg.get("apply_filters", True)
                    if filters and apply_filters:
                        print(f"     â€¢ Applying filters: {filters}")
                        if company == "valentum":
                            self._apply_filters_valentum(page, filters)
                        elif site_type == "reinhausen":
                            self._apply_filters_reinhausen(page, filters)
                        else:
                            self._apply_filters_playwright(page, filters)
                        if company == "bertrandt":
                            page.wait_for_timeout(10000)
                        else:
                            page.wait_for_timeout(1500)

                    link_patterns = page_cfg.get("job_link_patterns")
                    allow_external = bool(page_cfg.get("allow_external_job_links", False))
                    require_keyword = bool(page_cfg.get("require_keyword", True))
                    site_id = (page_cfg.get("workday_site_id") or "").strip()
                    if company == "rohde_schwarz":
                        req_links = self._collect_rohde_requests_links(next_url, max_pages=max_pages)
                        dom_links = self._collect_job_links_playwright(
                            page, next_url, link_patterns, allow_external, require_keyword, site_type, site_id
                        )
                        api_links = self._collect_rohde_api_links(page, next_url, max_pages=max_pages)
                        try:
                            print(f"     • Rohde links: req={len(req_links)} dom={len(dom_links)} api={len(api_links)}")
                        except Exception:
                            pass
                        job_links = req_links + dom_links + api_links
                    else:
                        job_links = self._collect_job_links_playwright(
                            page, next_url, link_patterns, allow_external, require_keyword, site_type, site_id
                        )
                    # Normalize any remaining relative links and drop non-URLs
                    try:
                        cleaned_links = []
                        for l in job_links:
                            if not l:
                                continue
                            if not l.startswith(("http://", "https://")):
                                l = urljoin(next_url, l)
                            if l.startswith(("http://", "https://")):
                                cleaned_links.append(l)
                        job_links = list(dict.fromkeys(cleaned_links))
                    except Exception:
                        pass
                    if company == "rohde_schwarz":
                        # Drop non-slug detail URLs like /stellenangebote/stellenangebot-detailansicht_...
                        job_links = [
                            l for l in job_links
                            if "/stellenangebote/stellenangebot-detailansicht_" not in l
                        ]
                    if site_type == "workday":
                        api_links = self._collect_workday_api_links(page, next_url, site_id)
                        if api_links:
                            job_links = api_links
                        # Normalize any remaining Workday links to include /en-US/{siteId}
                        if site_id:
                            normalized = []
                            for l in job_links:
                                if not l:
                                    continue
                                try:
                                    p = urlparse(l)
                                    if "/job/" in p.path and "/en-US/" not in p.path:
                                        scheme = p.scheme or "https"
                                        netloc = p.netloc or urlparse(next_url).netloc
                                        l = f"{scheme}://{netloc}/en-US/{site_id}{p.path}"
                                except Exception:
                                    pass
                                normalized.append(l)
                            job_links = normalized
                    if site_type == "workday":
                        signature = "|".join(sorted(set(job_links)))
                        if signature in workday_signatures:
                            break
                        workday_signatures.add(signature)
                    next_url_candidate = None
                    if site_type == "siemens":
                        # Compute next page BEFORE navigating to job details (page.url changes there)
                        next_url_candidate = self._next_page_siemens(page)
                    elif site_type == "workday":
                        # If API provided all links, no need to paginate UI
                        if api_links:
                            next_url_candidate = None
                        else:
                            advanced = self._advance_workday_page(page, target_page=workday_page_num + 1)
                            if advanced:
                                workday_inplace = True
                                next_url_candidate = page.url
                                workday_page_num += 1
                            else:
                                next_url_candidate = None
                    if company == "valentum":
                        job_links = [
                            l for l in job_links
                            if re.search(r"/stellenanzeige/\d+/.*\.html$", (l or "").lower())
                        ]
                    if company.startswith("ferchau"):
                        cleaned = []
                        for l in job_links:
                            if not l:
                                continue
                            ll = l.lower().rstrip("/")
                            # Keep only slugged URLs like /job/12345/some-title
                            if re.search(r"/job/\d+/.+", ll):
                                cleaned.append(ll)
                        job_links = cleaned
                    # Final de-dup per page (after all transformations)
                    try:
                        job_links = list(dict.fromkeys(job_links))
                    except Exception:
                        pass
                    print(f"     â€¢ Found {len(job_links)} job links on page")
                    if job_links:
                        company_found += len(job_links)
                    if company.startswith("ferchau"):
                        signature = "|".join(sorted(set(job_links)))
                        if signature in ferchau_signatures:
                            print("     â€¢ Ferchau pagination stalled (same links). Stopping.")
                            break
                        ferchau_signatures.add(signature)
                    for link in job_links:
                        print(f"       â€¢ Job link: {link}")
                        if link and link.strip().lower() in existing_urls:
                            print(f"         â€¢ Skipping already-seen job: {link}")
                            continue
                        if testing_mode or queue_only:
                            jobs.append({
                                "title": "Unknown",
                                "company": company,
                                "location": "Unknown",
                                "url": link,
                                "description": "",
                                "source": "Company",
                                "date_found": datetime.now().isoformat()
                            })
                            self._emit_job({
                                "title": "Unknown",
                                "company": company,
                                "location": "Unknown",
                                "url": link,
                                "description": "",
                                "source": "Company",
                                "date_found": datetime.now().isoformat()
                            })
                            continue
                        job = self._extract_job_details_playwright(page, link)
                        if not job:
                            continue
                        job["company"] = company
                        score_data = self._score_with_best_llm(job.get("description", ""))
                        if not score_data:
                            print("         âš  Groq scoring failed (rate limit or parse error)")
                            continue
                        job["score"] = score_data["score"]
                        job["match_reasons"] = score_data["reason"]
                        job["missing_skills"] = ""
                        print(f"         â€¢ Score: {job['score']}")
                        jobs.append(job)
                        self._emit_job(job)

                    if company.startswith("ferchau"):
                        next_url = self._next_page_ferchau(page)
                        if next_url:
                            print(f"     â€¢ Next page (ferchau): {next_url}")
                    elif company == "rohde_schwarz":
                        next_url = self._next_page_rohde_schwarz(page)
                        if next_url:
                            print(f"     â€¢ Next page (rohde_schwarz): {next_url}")
                    else:
                        next_url = self._next_page_playwright(page, next_url)
                    if site_type in ["siemens", "workday"] and next_url_candidate:
                        next_url = next_url_candidate
                    if next_url:
                        print(f"     â€¢ Next page: {next_url}")

                if company_found == 0:
                    self._log_event("zero_results", {
                        "source": "company_careers",
                        "company": company,
                        "url": url,
                        "details": "No job links found"
                    })
                print(f"   â€¢ Finished {company}")
            browser.close()

        return jobs

    def _should_exclude(self, job: Dict) -> bool:
        # Only basic sanity check during scraping (keywords/location are applied at source level).
        title = (job.get('title', '') or '').strip()
        return not bool(title)

    def _deduplicate(self, jobs: List[Dict]) -> List[Dict]:
        unique = []
        for job in jobs:
            key = (job.get("url", "") or "").lower().strip()
            if not key:
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
        print(f"â€¢ {job['title']} at {job['company']} [{job['source']}]")

