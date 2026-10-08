"""Shared source-record store: what we know about each scraped job page, per source.

Independent of any profile's scoring decisions. It lets the scraper skip description
fetches it already did well (and serve the stored text to the other profile), retry
incomplete ones with bounded backoff, and refresh old ones.

States of `status`: ok | empty | malformed | throttled | error | gone.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

OK, EMPTY, MALFORMED, THROTTLED, ERROR, GONE = "ok", "empty", "malformed", "throttled", "error", "gone"
RETRYABLE = {EMPTY, MALFORMED, THROTTLED, ERROR}

MIN_DESCRIPTION_CHARS = 80
_MOJIBAKE = re.compile(r"Ã[\u0080-\u00bf]|ÃŸ|Â[\u00a0-\u00bf+]|â€|â¬|\ufffd")
_LINKEDIN_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})")
_XING_ID = re.compile(r"/jobs/(?:[^/?#]*-)?(\d{5,})(?:[/?#]|$)")


def canonical_url(url: str) -> Tuple[str, str, str]:
    """Return (source, source_id, canonical_key). Tracking params, locale and slug are ignored."""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    host, path = parts.netloc.lower(), parts.path
    if "linkedin." in host:
        m = _LINKEDIN_ID.search(path)
        if m:
            return "linkedin", m.group(1), f"linkedin:{m.group(1)}"
    if "xing." in host:
        m = _XING_ID.search(path + "/")
        if m:
            return "xing", m.group(1), f"xing:{m.group(1)}"
    return "other", "", f"{host}{path.rstrip('/')}".lower()


def assess_description(text: str) -> str:
    """Classify extracted text: ok | empty | malformed."""
    text = (text or "").strip()
    if len(text) < MIN_DESCRIPTION_CHARS:
        return EMPTY
    if len(_MOJIBAKE.findall(text)) >= 3:
        return MALFORMED
    return OK


def classify_http(status: Optional[int]) -> str:
    if status is None:
        return ERROR
    if status == 429 or status >= 500:
        return THROTTLED
    if status in (404, 410):
        return GONE
    return ERROR


class RecordStore:
    def __init__(self, path: str, refresh_days: int = 14, max_retries: int = 3,
                 backoff_hours: Tuple[int, ...] = (6, 24, 72)):
        self.refresh_days = refresh_days
        self.max_retries = max_retries
        self.backoff_hours = backoff_hours
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "key TEXT PRIMARY KEY, source TEXT, source_id TEXT, aliases TEXT, first_seen TEXT, last_seen TEXT,"
            "status TEXT, last_success TEXT, failure_class TEXT, retry_count INTEGER DEFAULT 0,"
            "next_retry TEXT, content_hash TEXT, description TEXT)"
        )
        self.db.commit()

    # -- helpers
    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    def _row(self, key: str):
        cur = self.db.execute(
            "SELECT status, last_success, retry_count, next_retry, description, aliases FROM records WHERE key=?", (key,)
        )
        return cur.fetchone()

    def observe(self, url: str, now: Optional[datetime] = None) -> str:
        """Note that a URL was seen; returns its canonical key."""
        now = now or datetime.now()
        source, source_id, key = canonical_url(url)
        row = self._row(key)
        if row is None:
            self.db.execute(
                "INSERT INTO records (key, source, source_id, aliases, first_seen, last_seen, status) VALUES (?,?,?,?,?,?,?)",
                (key, source, source_id, json.dumps([url]), self._iso(now), self._iso(now), None),
            )
        else:
            aliases = json.loads(row[5] or "[]")
            if url not in aliases and len(aliases) < 20:
                aliases.append(url)
            self.db.execute("UPDATE records SET last_seen=?, aliases=? WHERE key=?",
                            (self._iso(now), json.dumps(aliases), key))
        self.db.commit()
        return key

    def decide(self, url: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
        """(fetch?, reason). Reasons: new, fresh, stale, retry, backoff, retries_exhausted, gone."""
        now = now or datetime.now()
        row = self._row(canonical_url(url)[2])
        if row is None or row[0] is None:
            return True, "new"
        status, last_success, retry_count, next_retry, _desc, _al = row
        if status == GONE:
            return False, "gone"
        if status == OK:
            if last_success and now - datetime.fromisoformat(last_success) < timedelta(days=self.refresh_days):
                return False, "fresh"
            if retry_count >= self.max_retries:
                return False, "retries_exhausted"  # failed refreshes: keep serving the stored text
            if next_retry and now < datetime.fromisoformat(next_retry):
                return False, "backoff"
            return True, "stale"
        if retry_count >= self.max_retries:
            return False, "retries_exhausted"
        if next_retry and now < datetime.fromisoformat(next_retry):
            return False, "backoff"
        return True, "retry"

    def description(self, url: str) -> str:
        row = self._row(canonical_url(url)[2])
        return (row[4] or "") if row else ""

    def record_result(self, url: str, description: str = "", http_status: Optional[int] = None,
                      now: Optional[datetime] = None, status_override: Optional[str] = None) -> str:
        """Store the outcome of one fetch attempt; returns the resulting status."""
        now = now or datetime.now()
        key = self.observe(url, now)
        if http_status is not None and http_status != 200:
            status = classify_http(http_status)
        else:
            status = assess_description(description)
        if status_override and status != OK:
            status = status_override  # e.g. low_quality: a source-specific verdict, retryable
        if status == OK:
            self.db.execute(
                "UPDATE records SET status=?, last_success=?, failure_class=NULL, retry_count=0, next_retry=NULL,"
                "content_hash=?, description=? WHERE key=?",
                (OK, self._iso(now), hashlib.sha1(description.encode("utf-8")).hexdigest(), description, key),
            )
        else:
            retry_count = (self._row(key)[2] or 0) + (0 if status == GONE else 1)
            delay = self.backoff_hours[min(retry_count - 1, len(self.backoff_hours) - 1)] if retry_count else 0
            next_retry = None if status == GONE else self._iso(now + timedelta(hours=delay))
            # keep a previous good description if a refresh attempt fails
            self.db.execute(
                "UPDATE records SET status=CASE WHEN status='ok' AND ?!='gone' THEN status ELSE ? END,"
                "failure_class=?, retry_count=?, next_retry=?, last_seen=? WHERE key=?",
                (status, status, status, retry_count, next_retry, self._iso(now), key),
            )
        self.db.commit()
        return status

    def prune(self, max_age_days: int = 90, now: Optional[datetime] = None) -> int:
        cutoff = self._iso((now or datetime.now()) - timedelta(days=max_age_days))
        cur = self.db.execute("DELETE FROM records WHERE last_seen < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.close()
