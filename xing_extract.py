"""XING page extraction: decode, structured data first, body-only text, quality checks.

Fixes the observed XING problems separately:
  * encoding   - decode from raw bytes (header -> meta -> UTF-8 -> cp1252), never trust requests'
                 ISO-8859-1 default; repair text that was already decoded wrongly.
  * metadata   - company/location from schema.org JobPosting JSON-LD when present.
  * body       - when only page text is available, keep just the job body (drop header,
                 repeated titles, similar jobs and SEO link blocks).
  * validation - flag empty / malformed / low-quality text so it is never scored.
"""

from __future__ import annotations

import codecs
import json
import re
from typing import Dict, Optional, Tuple

from bs4 import BeautifulSoup

from source_records import EMPTY, MALFORMED, OK

LOW_QUALITY = "low_quality"

_MOJI_RUN = re.compile(r"[Â-ô][\u0080-¿]{1,3}")
_MOJI_MARK = re.compile(r"Ã[\u0080-¿]|ÃŸ|Â[ -¿+]|â€|â¬|�")
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", re.I)
_BODY_START = ("About this job", "Über den Job", "Über diesen Job")
_TAIL_MARKERS = ("Similar jobs", "Ähnliche Jobs", "Weitere Jobs")
_SEO_LINK = re.compile(r"(?:\S+ ){0,2}?Jobs in [A-ZÄÖÜ][\wäöüß.\-]+")
_SEO_TOKEN = re.compile(r"\bJobs in [A-ZÄÖÜ]")


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 text that was decoded as Latin-1/cp1252, run by run (safe on mixed text)."""
    def fix(match: "re.Match[str]") -> str:
        try:
            return match.group(0).encode("latin-1").decode("utf-8")
        except UnicodeError:
            return match.group(0)
    return _MOJI_RUN.sub(fix, text or "")


def decode_response(content: bytes, content_type: Optional[str] = None) -> str:
    """Decode HTML bytes using the declared charset, but never forced against the bytes."""
    declared = None
    m = _HEADER_CHARSET.search(content_type or "")
    if m:
        declared = m.group(1)
    if not declared:
        m2 = _META_CHARSET.search(content[:4096])
        if m2:
            declared = m2.group(1).decode("ascii", "ignore")
    if declared:
        try:
            codecs.lookup(declared)
        except LookupError:
            declared = None
    ascii_only = all(b < 0x80 for b in content)
    if declared and declared.lower().replace("_", "-") not in ("utf-8", "utf8"):
        # Sites (and requests' own default) often claim Latin-1 for UTF-8 bytes: valid UTF-8
        # with multi-byte sequences is overwhelmingly UTF-8, so prefer it.
        if not ascii_only:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return content.decode(declared, errors="replace")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _jobpostings(node):
    if isinstance(node, list):
        for item in node:
            yield from _jobpostings(item)
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "JobPosting" in kinds:
            yield node
        for value in node.values():
            if isinstance(value, (list, dict)):
                yield from _jobpostings(value)


def parse_jobposting(soup: BeautifulSoup) -> Optional[Dict[str, str]]:
    """Return title/company/location/description from schema.org JobPosting JSON-LD, if any."""
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            payload = json.loads(raw, strict=False)
        except (TypeError, ValueError):
            continue
        for posting in _jobpostings(payload):
            org = posting.get("hiringOrganization")
            company = (org.get("name") if isinstance(org, dict) else org) or ""
            loc = posting.get("jobLocation")
            loc = loc[0] if isinstance(loc, list) and loc else loc
            address = (loc or {}).get("address") if isinstance(loc, dict) else None
            if isinstance(address, dict):
                location = ", ".join(str(address[k]) for k in ("addressLocality", "addressRegion") if address.get(k))
            else:
                location = str(address or "")
            description = BeautifulSoup(str(posting.get("description") or ""), "lxml").get_text(" ", strip=True)
            return {
                "title": str(posting.get("title") or ""), "company": str(company).strip(),
                "location": location.strip(), "description": description,
            }
    return None


def clean_body(text: str) -> str:
    """Reduce whole-page text to the job body: repair encoding, drop header, similar jobs, SEO links."""
    text = repair_mojibake(text or "")
    for marker in _BODY_START:
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx + len(marker):]
            break
    cut = len(text)
    for marker in _TAIL_MARKERS:
        idx = text.find(marker)
        if 0 <= idx < cut:
            cut = idx
    if len(_SEO_TOKEN.findall(text)) >= 3:
        first = _SEO_TOKEN.search(text).start()
        # back up over the link label (1-2 words) that precedes "Jobs in <City>"
        label = re.search(r"(?:\S+ ){1,2}$", text[:first])
        cut = min(cut, label.start() if label else first)
    return re.sub(r"\s+", " ", text[:cut]).strip()


def assess_xing(description: str, title: str = "") -> Tuple[str, str]:
    """(status, flags) with status in ok | empty | malformed | low_quality."""
    text = (description or "").strip()
    if len(text) < 150:
        return EMPTY, "too_short"
    flags = []
    if len(_MOJI_MARK.findall(text)) >= 3:
        return MALFORMED, "mojibake"
    if title and len(title) > 8 and text.lower().count(title.lower()) >= 3:
        flags.append("repeated_title")
    if len(_SEO_TOKEN.findall(text)) >= 3:
        flags.append("seo_links")
    if any(m in text for m in ("Create search alert", "Save job", "Similar jobs")):
        flags.append("page_furniture")
    if len(text) > 20000:
        flags.append("too_long")
    return (LOW_QUALITY, ",".join(flags)) if flags else (OK, "")


_LOC_PAIR = re.compile(r"(?P<loc>[A-ZÄÖÜ][\wäöüß.\-]*(?: [A-ZÄÖÜ][\wäöüß.\-]*)*) (?P=loc)\s*\+ ?\d+ more")


def header_location(text: str) -> str:
    """Offline fallback: the header repeats the city before '+ N more' ('Kerpen Kerpen + 0 more')."""
    m = _LOC_PAIR.search(repair_mojibake(text or "")[:1500])
    return m.group("loc") if m else ""
