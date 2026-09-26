#!/usr/bin/env python3
"""Server-rendered public job list, private dashboard, and sync API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http import cookies
from pathlib import Path
from typing import Any, Callable
from wsgiref.simple_server import make_server

from cover_letters import CoverLetterStore, DEFAULT_DB, PUBLIC_SORT_MODES, prepare_trend_snapshot, utc_now


SESSION_COOKIE = "matchatlas_admin"
PROFILES = {"kk": "KK", "sandra": "Sandra"}
MAX_JSON_BYTES = 5_000_000
STATIC_DIR = Path(__file__).resolve().parent / "static"
DONATION_QR_PATH = STATIC_DIR / "donation-qr.jpeg"


def hash_password(password: str, iterations: int = 390_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


class CoverLetterWebApp:
    def __init__(self, store: Any, password_hash: str, secret_key: str, secure_cookie: bool = True, sync_token: str | dict[str, str] = ""):
        self.stores = store if isinstance(store, dict) else {"kk": store}
        self.store = self.stores.get("kk", next(iter(self.stores.values())))
        self.password_hash = password_hash
        self.secret_key = secret_key.encode("utf-8")
        self.secure_cookie = secure_cookie
        self.sync_tokens = sync_token if isinstance(sync_token, dict) else {"kk": sync_token}

    def _profile_store(self, profile_id: str) -> Any:
        if profile_id not in PROFILES or profile_id not in self.stores:
            raise KeyError(profile_id)
        return self.stores[profile_id]

    def _sign(self, payload: str) -> str:
        signature = hmac.new(self.secret_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def _new_session(self) -> str:
        expiry = int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp())
        payload = base64.urlsafe_b64encode(f"{expiry}:{secrets.token_hex(16)}".encode()).decode().rstrip("=")
        return self._sign(payload)

    def _session_valid(self, environ: dict) -> bool:
        jar = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        morsel = jar.get(SESSION_COOKIE)
        if not morsel:
            return False
        token = morsel.value
        try:
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(self.secret_key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            padded = payload + "=" * (-len(payload) % 4)
            expiry = int(base64.urlsafe_b64decode(padded).decode().split(":", 1)[0])
            return expiry > int(datetime.now(timezone.utc).timestamp())
        except (ValueError, UnicodeDecodeError):
            return False

    def _sync_authorized(self, environ: dict, profile_id: str = "kk") -> bool:
        supplied = environ.get("HTTP_AUTHORIZATION", "")
        token = self.sync_tokens.get(profile_id, "")
        expected = f"Bearer {token}" if token else ""
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _csrf(self, environ: dict) -> str:
        jar = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        token = jar.get(SESSION_COOKIE)
        value = token.value if token else ""
        return hmac.new(self.secret_key, (value + ":csrf").encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _read_bytes(environ: dict, maximum: int) -> bytes:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > maximum:
            raise ValueError("request body is too large")
        return environ["wsgi.input"].read(length)

    @classmethod
    def _read_form(cls, environ: dict) -> dict[str, str]:
        raw = cls._read_bytes(environ, 100_000).decode("utf-8", errors="replace")
        return dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))

    @classmethod
    def _read_json(cls, environ: dict) -> dict[str, Any]:
        if "application/json" not in environ.get("CONTENT_TYPE", "").lower():
            raise ValueError("Content-Type must be application/json")
        payload = json.loads(cls._read_bytes(environ, MAX_JSON_BYTES).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @staticmethod
    def _response(start_response: Callable, body: str, status: str = "200 OK", headers=None):
        encoded = body.encode("utf-8")
        base = [
            ("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("X-Robots-Tag", "noindex, nofollow"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
            ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
        ]
        start_response(status, base + list(headers or []))
        return [encoded]

    @staticmethod
    def _asset_response(start_response: Callable, body: bytes, content_type: str, status: str = "200 OK"):
        start_response(status, [
            ("Content-Type", content_type), ("Content-Length", str(len(body))),
            ("Cache-Control", "public, max-age=3600"),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("X-Robots-Tag", "noindex, nofollow"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Content-Security-Policy", "default-src 'none'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'"),
        ])
        return [body]

    @staticmethod
    def _json_response(start_response: Callable, payload: Any, status: str = "200 OK"):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start_response(status, [
            ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("X-Robots-Tag", "noindex, nofollow"),
        ])
        return [encoded]

    @staticmethod
    def _redirect(start_response: Callable, location: str, headers=None):
        start_response("303 See Other", [("Location", location), ("Content-Length", "0")] + list(headers or []))
        return [b""]

    @staticmethod
    def _layout(title: str, content: str, admin: bool = False, profile_id: str = "kk") -> str:
        profile_nav = '<a href="/jobs/kk">KK Jobs</a><a href="/jobs/sandra">Sandra Jobs</a>'
        admin_href = f"/admin/{profile_id}"
        nav = profile_nav + '<a href="/faq">FAQ</a>' + (f'<a href="{admin_href}">Private dashboard</a>' if admin else f'<a href="{admin_href}">Admin</a>')
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--ink:#0a0a0a;--paper:#fff;--line:#d9d9d9;--muted:#696969}}*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 Arial,sans-serif}}header{{display:flex;justify-content:space-between;align-items:center;padding:22px 5vw;border-bottom:1px solid var(--line)}}
header strong{{font-size:1.4rem;letter-spacing:.08em}}nav{{display:flex;gap:22px}}a{{color:inherit}}main{{max-width:1180px;margin:auto;padding:44px 5vw}}h1{{font-size:clamp(2.3rem,6vw,5.6rem);line-height:.98;max-width:900px;margin:0 0 38px}}h2{{margin-top:34px}}.meta{{color:var(--muted);font-size:.88rem}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:0 0 30px}}.stat{{border:1px solid var(--line);padding:22px}}.stat b{{display:block;font-size:2.5rem}}.support-tile{{width:100%;border:1px solid var(--line);border-radius:0;background:var(--paper);color:var(--ink);padding:22px;text-align:left;cursor:pointer}}.support-tile:hover,.support-tile:focus-visible{{background:#f5f5f5}}.support-tile b{{font-size:1.45rem;line-height:1.2}}.toolbar{{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 28px}}select,input,textarea,button{{font:inherit;padding:10px 12px;border:1px solid var(--ink);background:#fff}}button,.pill{{border-radius:999px;background:#0a0a0a;color:#fff;padding:10px 18px;text-decoration:none;display:inline-block}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:14px 10px;border-bottom:1px solid var(--line);vertical-align:top}}.expired{{color:#777}}pre{{white-space:pre-wrap;font:inherit;border:1px solid var(--line);padding:24px}}.card{{border:1px solid var(--line);padding:24px;margin:18px 0}}.trend-card{{border:1px solid var(--line);padding:22px;margin:0 0 30px}}.trend-card h2{{margin:0 0 4px}}.trend-card svg{{display:block;width:100%;height:auto;margin-top:14px}}dialog{{width:min(92vw,520px);border:1px solid var(--ink);padding:28px;background:var(--paper);color:var(--ink)}}dialog::backdrop{{background:rgba(0,0,0,.65)}}dialog h2{{margin:0 44px 10px 0}}dialog img{{display:block;width:min(100%,320px);height:auto;margin:22px auto 0}}.dialog-close{{float:right;margin:-8px -8px 8px 12px}}.error{{color:#9b1c1c}}textarea{{display:block;width:100%;min-height:70px;margin:10px 0}}@media(max-width:760px){{header{{align-items:flex-start;gap:18px}}nav{{gap:14px;flex-wrap:wrap;justify-content:flex-end}}table,thead,tbody,tr,th,td{{display:block}}thead{{display:none}}td{{padding:5px 0;border:0}}tr{{padding:18px 0;border-bottom:1px solid var(--line)}}dialog{{padding:22px}}}}
</style></head><body><header><strong>MATCHATLAS</strong><nav>{nav}</nav></header><main>{content}</main></body></html>"""

    @staticmethod
    def _trend_chart(points: list[dict[str, Any]]) -> str:
        today = utc_now().date()
        start = today - timedelta(days=89)
        clean: list[tuple[date, int]] = []
        for point in points:
            try:
                point_date = date.fromisoformat(str(point["snapshot_date"]))
                count = max(0, int(point["active_14d_count"]))
            except (KeyError, TypeError, ValueError):
                continue
            if start <= point_date <= today:
                clean.append((point_date, count))
        clean.sort(key=lambda item: item[0])
        heading = '<h2>3 Month Job Trend</h2>'
        if not clean:
            return f'<section class="trend-card">{heading}<p class="meta">Trend history starts with the next successful website synchronization.</p></section>'

        width, height = 800, 220
        left, right, top, bottom = 48, 18, 18, 34
        plot_width = width - left - right
        plot_height = height - top - bottom
        maximum = max(1, max(count for _, count in clean))

        def coordinates(point_date: date, count: int) -> tuple[float, float]:
            x = left + ((point_date - start).days / 89) * plot_width
            y = top + ((maximum - count) / maximum) * plot_height
            return x, y

        plotted = [(point_date, count, *coordinates(point_date, count)) for point_date, count in clean]
        polyline = " ".join(f"{x:.1f},{y:.1f}" for _, _, x, y in plotted)
        line = f'<polyline points="{polyline}" fill="none" stroke="#0a0a0a" stroke-width="3" vector-effect="non-scaling-stroke"/>' if len(plotted) > 1 else ""
        circles = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#0a0a0a"><title>{html.escape(point_date.isoformat())}: {count} jobs</title></circle>'
            for point_date, count, x, y in plotted
        )
        grid = "".join(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d9d9d9"/><text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-size="12" fill="#696969">{value}</text>'
            for value, y in ((maximum, top), (maximum // 2, top + plot_height / 2), (0, top + plot_height))
        )
        note = '<p class="meta">Trend history starts today and will fill during weekday synchronizations.</p>' if len(clean) == 1 else '<p class="meta">Recorded during successful weekday website synchronizations.</p>'
        svg = f'''<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="trend-title trend-desc">
<title id="trend-title">Jobs in the last 14 days over three months</title><desc id="trend-desc">{len(clean)} recorded snapshots. Latest value: {clean[-1][1]} jobs.</desc>
{grid}{line}{circles}<text x="{left}" y="{height-8}" font-size="12" fill="#696969">{start.strftime('%d %b')}</text><text x="{width-right}" y="{height-8}" text-anchor="end" font-size="12" fill="#696969">{today.strftime('%d %b')}</text></svg>'''
        return f'<section class="trend-card">{heading}{note}{svg}</section>'

    def _public_home(self, environ: dict, start_response: Callable, profile_id: str = "kk"):
        store = self._profile_store(profile_id)
        query = dict(urllib.parse.parse_qsl(environ.get("QUERY_STRING", "")))
        view = query.get("view", "all") if query.get("view") in {"all", "active", "expired"} else "all"
        try:
            min_score = float(query["score"]) if query.get("score") else None
        except ValueError:
            min_score = None
        primary_sort = query.get("sort", "date_desc")
        if primary_sort not in PUBLIC_SORT_MODES:
            primary_sort = "date_desc"
        secondary_raw = query.get("then", "score_desc")
        secondary_sort = None if secondary_raw == "none" else secondary_raw
        if secondary_sort not in PUBLIC_SORT_MODES:
            secondary_sort = "score_desc"
        if secondary_sort and PUBLIC_SORT_MODES[secondary_sort][0] == PUBLIC_SORT_MODES[primary_sort][0]:
            secondary_sort = None

        sort_labels = (
            ("date_desc", "Latest published"),
            ("date_asc", "Oldest published"),
            ("score_desc", "Highest score"),
            ("score_asc", "Lowest score"),
        )
        primary_options = "".join(
            f'<option value="{value}"{" selected" if value == primary_sort else ""}>{label}</option>'
            for value, label in sort_labels
        )
        secondary_options = '<option value="none"{}>None</option>'.format(" selected" if secondary_sort is None else "") + "".join(
            f'<option value="{value}"{" selected" if value == secondary_sort else ""}>{label}</option>'
            for value, label in sort_labels
        )
        summary_jobs = store.public_jobs("all")
        filtered_jobs = store.public_jobs("all", min_score, primary_sort, secondary_sort)
        jobs = filtered_jobs if view == "all" else [job for job in filtered_jobs if job["age_status"] == view]
        active_count = sum(job["age_status"] == "active" for job in summary_jobs)
        expired_count = sum(job["age_status"] == "expired" for job in summary_jobs)
        trend = self._trend_chart(store.trend_snapshots())
        rows = []
        for job in jobs:
            status_class = "expired" if job["age_status"] == "expired" else ""
            letter = f'<a href="/jobs/{profile_id}/{job["job_id"]}/cover-letter">Cover letter sample</a>' if job["status"] == "ready" and job["public_text"] else '<span class="meta">Cover letter unavailable</span>'
            date = str(job.get("posted_at") or job.get("first_seen") or "")[:10]
            rows.append(f'<tr class="{status_class}"><td><a href="{html.escape(job["url"], quote=True)}" rel="noopener noreferrer">{html.escape(job["title"])}</a><div class="meta">{html.escape(job["company"])}</div></td><td>{html.escape(date)}</td><td>{html.escape(job["source"])}</td><td>{html.escape(str(job["score"] or ""))}</td><td>{html.escape(job["age_status"].title())}</td><td>{letter}</td></tr>')
        content = f"""<p class="meta">CURATED JOBS · LAST 28 DAYS</p><h1>Relevant work, without the noise.</h1>
<section class="stats"><div class="stat"><span>Jobs in last 14 days</span><b>{active_count}</b></div><div class="stat"><span>Expired jobs, days 15–28</span><b>{expired_count}</b></div><button class="stat support-tile" type="button" id="open-donation" aria-haspopup="dialog" aria-controls="donation-dialog"><b>Buy me a coffee</b></button></section>
{trend}
<form class="toolbar" method="get"><label>Status <select name="view"><option value="all">All</option><option value="active" {'selected' if view=='active' else ''}>Last 14 days</option><option value="expired" {'selected' if view=='expired' else ''}>Days 15–28</option></select></label><label>Minimum score <input name="score" type="number" min="0" max="10" value="{html.escape(query.get('score',''))}"></label><label>Sort first <select name="sort">{primary_options}</select></label><label>Then by <select name="then">{secondary_options}</select></label><button>Apply</button></form>
<table><thead><tr><th>Job</th><th>Date</th><th>Source</th><th>Score</th><th>Status</th><th>Letter</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="6">No jobs match these filters.</td></tr>'}</tbody></table>
<dialog id="donation-dialog" aria-labelledby="donation-title"><button class="dialog-close" type="button" id="close-donation" aria-label="Close donation popup">Close</button><h2 id="donation-title">Buy me a coffee</h2><p>This website saves time by collecting, filtering, and scoring relevant jobs from LinkedIn, XING, and employer career pages. It provides direct application links and redacted cover-letter examples in one place.</p><p>Support is completely voluntary. If the website helps you, scan the PayPal QR code with your phone to support its running costs and continued improvement.</p><img src="/static/donation-qr.jpeg" alt="PayPal donation QR code for Krishnakumar Radhakrishna Panicker" loading="lazy" width="320" height="360"></dialog>
<script>(()=>{{const modal=document.getElementById('donation-dialog');const open=document.getElementById('open-donation');const close=document.getElementById('close-donation');open.addEventListener('click',()=>{{modal.showModal();close.focus();}});close.addEventListener('click',()=>modal.close());modal.addEventListener('click',event=>{{const box=modal.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)modal.close();}});}})();</script>"""
        return self._response(start_response, self._layout(f"{PROFILES[profile_id]} Jobs", content, profile_id=profile_id))

    def _faq(self, start_response: Callable):
        content = """<p class="meta">ABOUT THIS PROJECT</p><h1>Frequently asked questions</h1>
<p>KK created this project to turn a fragmented evening job search into one focused list. It gathers relevant vacancies, estimates their fit, links back to the original posting, and makes the useful results available to friends without requiring an account or ChatGPT.</p>
<section class="card"><h2>Purpose and workflow</h2>
<details><summary>What problem does this project solve?</summary><p>Relevant vacancies are spread across several platforms, repeated searches take time, and promising roles are easy to miss. The project brings recent results into one searchable place so visitors can spend more time evaluating and applying.</p></details>
<details><summary>How does a job reach this website?</summary><p>An automated weekday workflow collects vacancies from LinkedIn, XING, and selected employer career pages. A locally operated language model scores their relevance. Matching jobs are prepared for the website, optional cover letters are generated separately, and the approved public information is synchronized to the hosted site.</p></details>
<details><summary>How often is the website updated?</summary><p>Collection and scoring are scheduled Monday through Friday in the evening, followed by cover-letter processing and website synchronization. There are no scheduled weekend runs, and a failed source or interrupted run can delay an update.</p></details>
<details><summary>Is every available job guaranteed to appear?</summary><p>No. Search platforms can change their pages, limit automated access, omit results, or remove postings. This project reduces search effort but cannot guarantee complete coverage.</p></details></section>
<section class="card"><h2>Using the job list</h2>
<details><summary>Where do I apply?</summary><p>Select the job title to open the original LinkedIn, XING, or employer page. Applications are always completed on the original source, not on this website.</p></details>
<details><summary>How do filtering and sorting work?</summary><p>Status limits the age window and Minimum score removes results below a chosen score. Sort first controls the main order. Then by breaks ties using a second field. Date and score can therefore be sorted independently or together.</p></details>
<details><summary>Which date is displayed?</summary><p>The source-provided publication date is used when it is available. If a reliable publication date was not supplied, the website uses the date on which the project first discovered the job.</p></details>
<details><summary>What does the matching score mean?</summary><p>The score is an automated relevance estimate against the project’s configured candidate profile. It helps prioritize reading, but it does not predict interview selection, application success, job quality, or suitability for a different person.</p></details>
<details><summary>What do active and expired mean here?</summary><p>Active jobs are from the most recent 14-day window. “Expired” means the record is 15–28 days old within this project; it is not necessarily confirmed closed by the employer. The original link may remain open or may already have disappeared.</p></details>
<details><summary>What does the 3 Month Job Trend show?</summary><p>Each successful weekday synchronization records the number shown in “Jobs in last 14 days.” The graph uses only genuine snapshots, so weekends and failed runs can appear as gaps and unavailable history is not invented.</p></details></section>
<section class="card"><h2>Cover letters and privacy</h2>
<details><summary>What is a cover-letter sample?</summary><p>It is a redacted example generated for the selected candidate using evidence from that candidate's reviewed career documents and the specific job description. Visitors can use it for inspiration, but it is not a truthful application for another person and should not be copied unchanged.</p></details>
<details><summary>Why is a cover letter sometimes unavailable?</summary><p>A letter is produced only when the job description contains enough information for a grounded result. Missing or inadequate descriptions are marked unavailable instead of being filled with invented claims.</p></details>
<details><summary>What can guests see?</summary><p>Guests can view job information, original links, age and score data, the trend, and redacted letter samples. They do not need an account or ChatGPT. Complete letters, application status, private notes, and administrative actions remain available only in the authenticated dashboard.</p></details>
<details><summary>What personal information is protected?</summary><p>Public letter samples remove contact details, signatures, private notes, and application tracking. Authorization and redaction are enforced by the server rather than relying on the visitor’s browser.</p></details></section>
<section class="card"><h2>Technology, AI, and support</h2>
<details><summary>What technology powers the project?</summary><p>The workflow is written in Python. LM Studio runs the local scoring model, SQLite keeps the recoverable local copy, Supabase stores the hosted data, and Render serves the public website. Scheduled automation connects the stages.</p></details>
<details><summary>How were ChatGPT and Codex Cowork involved?</summary><p>KK developed the idea and made the product decisions with help from ChatGPT/Codex Cowork for planning, implementation, testing, automation, and cover-letter generation. Candidate claims remain grounded in each candidate's reviewed documents, while job and application decisions remain human choices.</p></details>
<details><summary>How long is information retained?</summary><p>Public jobs remain visible for no more than 28 days. Trend snapshots cover approximately three months. Each profile's private applied-job history can be preserved separately from the guest-facing list.</p></details>
<details><summary>How can I support the project?</summary><p>Support is completely voluntary. The Buy me a coffee tile on the jobs page opens a PayPal QR code for anyone who finds the project useful and wants to support its running costs and continued improvement.</p></details></section>"""
        return self._response(start_response, self._layout("FAQ", content))

    def _public_letter(self, profile_id: str, job_id: str, start_response: Callable):
        job = self._profile_store(profile_id).get_public_job(job_id)
        if not job or job["status"] != "ready" or not job["public_text"]:
            return self._response(start_response, self._layout("Not available", "<h1>Cover letter unavailable.</h1>", profile_id=profile_id), "404 Not Found")
        content = f'<p class="meta">REDACTED SAMPLE</p><h1>{html.escape(job["title"])}</h1><pre>{html.escape(job["public_text"])}</pre><p class="meta">Personal contact details and signature are intentionally omitted.</p>'
        return self._response(start_response, self._layout("Cover letter sample", content, profile_id=profile_id))

    def _login(self, environ: dict, start_response: Callable, profile_id: str = "kk"):
        if environ["REQUEST_METHOD"] == "POST":
            form = self._read_form(environ)
            if self.password_hash and verify_password(form.get("password", ""), self.password_hash):
                token = self._new_session()
                secure = "; Secure" if self.secure_cookie else ""
                cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}"
                return self._redirect(start_response, f"/admin/{profile_id}", [("Set-Cookie", cookie)])
            error = '<p class="error">Invalid password.</p>'
        else:
            error = ""
        content = f'<p class="meta">PRIVATE AREA</p><h1>Admin sign in</h1>{error}<form method="post"><label>Password <input type="password" name="password" required autocomplete="current-password"></label> <button>Sign in</button></form>'
        return self._response(start_response, self._layout("Admin sign in", content, profile_id=profile_id))

    def _admin(self, environ: dict, start_response: Callable, profile_id: str = "kk"):
        if not self._session_valid(environ):
            return self._redirect(start_response, f"/admin/{profile_id}/login")
        csrf = self._csrf(environ)
        jobs = self._profile_store(profile_id).admin_jobs()
        applied_count = sum(bool(job["applied"]) for job in jobs)
        cards = []
        for job in jobs:
            full = html.escape(job["full_text"] or "Cover letter unavailable")
            checked = "checked" if job["applied"] else ""
            cards.append(f"""<section class="card"><h2><a href="{html.escape(job['url'], quote=True)}">{html.escape(job['title'])}</a></h2>
<p class="meta">{html.escape(job['company'])} · score {html.escape(str(job['score'] or ''))} · letter {html.escape(job['status'])}</p>
<details><summary>Cover letter</summary><pre id="letter-{job['job_id']}">{full}</pre><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('letter-{job['job_id']}').innerText)">Copy</button></details>
<form method="post" action="/admin/{profile_id}/jobs/{job['job_id']}/application"><input type="hidden" name="csrf" value="{csrf}"><label><input type="checkbox" name="applied" value="1" {checked}> Applied</label><textarea name="notes" placeholder="Private notes">{html.escape(job['notes'] or '')}</textarea><button>Save</button></form>
<form method="post" action="/admin/{profile_id}/jobs/{job['job_id']}/regenerate"><input type="hidden" name="csrf" value="{csrf}"><button>Regenerate cover letter</button></form></section>""")
        stats = f'<section class="stats"><div class="stat"><span>Tracked jobs</span><b>{len(jobs)}</b></div><div class="stat"><span>Applications sent</span><b>{applied_count}</b></div></section>'
        return self._response(start_response, self._layout("Private dashboard", '<p class="meta">ADMIN ONLY</p><h1>Your application dashboard.</h1>' + stats + "".join(cards), admin=True, profile_id=profile_id))

    def _admin_action(self, environ: dict, start_response: Callable, profile_id: str, job_id: str, action: str):
        if not self._session_valid(environ):
            return self._response(start_response, "Unauthorized", "401 Unauthorized")
        form = self._read_form(environ)
        if not hmac.compare_digest(form.get("csrf", ""), self._csrf(environ)):
            return self._response(start_response, "Invalid CSRF token", "403 Forbidden")
        if action == "regenerate":
            self._profile_store(profile_id).regenerate(job_id)
        elif action == "application":
            self._profile_store(profile_id).set_application(job_id, form.get("applied") == "1", form.get("notes", ""))
        return self._redirect(start_response, f"/admin/{profile_id}")

    def _sync_api(self, environ: dict, start_response: Callable, profile_id: str = "kk"):
        if not self._sync_authorized(environ, profile_id):
            return self._json_response(start_response, {"error": "unauthorized"}, "401 Unauthorized")
        try:
            payload = self._read_json(environ)
            jobs = payload.get("jobs")
            snapshots = payload.get("trend_snapshots") or []
            if not isinstance(jobs, list):
                raise ValueError("jobs must be a list")
            if len(jobs) > 2500:
                raise ValueError("too many jobs in one request")
            if not isinstance(snapshots, list) or len(snapshots) > 120:
                raise ValueError("trend_snapshots must be a list of at most 120 items")
            prepared_snapshots = [prepare_trend_snapshot(item) for item in snapshots]
            store = self._profile_store(profile_id)
            synced_jobs = store.sync_jobs(jobs)
            synced_snapshots = store.sync_trend_snapshots(prepared_snapshots)
            current = store.record_trend_snapshot()
            return self._json_response(start_response, {
                "synced": synced_jobs,
                "trend_snapshots": synced_snapshots,
                "current_snapshot": current,
            })
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json_response(start_response, {"error": str(exc)}, "400 Bad Request")

    def __call__(self, environ: dict, start_response: Callable):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if method == "GET" and path == "/healthz":
            return self._json_response(start_response, {"status": "ok"})
        if method == "GET" and path == "/robots.txt":
            return self._response(start_response, "User-agent: *\nDisallow: /\n")
        if method == "GET" and path == "/static/donation-qr.jpeg":
            try:
                return self._asset_response(start_response, DONATION_QR_PATH.read_bytes(), "image/jpeg")
            except OSError:
                return self._response(start_response, "Asset not available", "404 Not Found")
        if path == "/api/v1/state" and method == "GET":
            if not self._sync_authorized(environ, "kk"):
                return self._json_response(start_response, {"error": "unauthorized"}, "401 Unauthorized")
            return self._json_response(start_response, self._profile_store("kk").cloud_state())
        if path == "/api/v1/sync" and method == "POST":
            return self._sync_api(environ, start_response, "kk")
        match = re.match(r"^/api/v1/profiles/(kk|sandra)/(state|sync)$", path)
        if match:
            profile_id, action = match.groups()
            if action == "state" and method == "GET":
                if not self._sync_authorized(environ, profile_id):
                    return self._json_response(start_response, {"error": "unauthorized"}, "401 Unauthorized")
                return self._json_response(start_response, self._profile_store(profile_id).cloud_state())
            if action == "sync" and method == "POST":
                return self._sync_api(environ, start_response, profile_id)
        if method == "GET" and path == "/":
            return self._public_home(environ, start_response, "kk")
        match = re.match(r"^/jobs/(kk|sandra)$", path)
        if method == "GET" and match:
            return self._public_home(environ, start_response, match.group(1))
        if method == "GET" and path == "/faq":
            return self._faq(start_response)
        match = re.match(r"^/jobs/([a-f0-9]{24})/cover-letter$", path)
        if method == "GET" and match:
            return self._public_letter("kk", match.group(1), start_response)
        match = re.match(r"^/jobs/(kk|sandra)/([a-f0-9]{24})/cover-letter$", path)
        if method == "GET" and match:
            return self._public_letter(match.group(1), match.group(2), start_response)
        if path == "/admin/login" and method in {"GET", "POST"}:
            return self._login(environ, start_response, "kk")
        if path == "/admin" and method == "GET":
            return self._admin(environ, start_response, "kk")
        match = re.match(r"^/admin/(kk|sandra)/login$", path)
        if match and method in {"GET", "POST"}:
            return self._login(environ, start_response, match.group(1))
        match = re.match(r"^/admin/(kk|sandra)$", path)
        if match and method == "GET":
            return self._admin(environ, start_response, match.group(1))
        match = re.match(r"^/admin/jobs/([a-f0-9]{24})/(regenerate|application)$", path)
        if method == "POST" and match:
            return self._admin_action(environ, start_response, "kk", match.group(1), match.group(2))
        match = re.match(r"^/admin/(kk|sandra)/jobs/([a-f0-9]{24})/(regenerate|application)$", path)
        if method == "POST" and match:
            return self._admin_action(environ, start_response, match.group(1), match.group(2), match.group(3))
        return self._response(start_response, self._layout("Not found", "<h1>Not found.</h1>"), "404 Not Found")


def create_application() -> CoverLetterWebApp:
    password_hash = os.getenv("COVER_LETTER_ADMIN_PASSWORD_HASH", "")
    secret_key = os.getenv("COVER_LETTER_SECRET_KEY", "")
    sync_token = os.getenv("COVER_LETTER_SYNC_TOKEN", "")
    sandra_sync_token = os.getenv("COVER_LETTER_SYNC_TOKEN_SANDRA", "")
    if not password_hash or not secret_key or not sync_token or not sandra_sync_token:
        raise RuntimeError("Admin password hash, session secret, and sync token are required")
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        from cloud_store import PostgresCoverLetterStore
        stores = {
            profile: PostgresCoverLetterStore(database_url, profile)
            for profile in PROFILES
        }
    else:
        stores = {
            "kk": CoverLetterStore(os.getenv("COVER_LETTER_DB", DEFAULT_DB), "kk"),
            "sandra": CoverLetterStore(os.getenv("COVER_LETTER_DB_SANDRA", "profiles/sandra/state/cover_letters.db"), "sandra"),
        }
    return CoverLetterWebApp(stores, password_hash, secret_key, secure_cookie=True, sync_token={"kk": sync_token, "sandra": sandra_sync_token})


def main() -> int:
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="Serve the MatchAtlas cover-letter website")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.getenv("COVER_LETTER_DB", DEFAULT_DB))
    parser.add_argument("--hash-password", action="store_true")
    args = parser.parse_args()
    if args.hash_password:
        print(hash_password(getpass.getpass("Admin password: ")))
        return 0
    password_hash = os.getenv("COVER_LETTER_ADMIN_PASSWORD_HASH", "")
    secret_key = os.getenv("COVER_LETTER_SECRET_KEY", "")
    sync_token = os.getenv("COVER_LETTER_SYNC_TOKEN", "")
    if not password_hash or not secret_key or not sync_token:
        raise SystemExit("Set admin password hash, session secret, and sync token before serving.")
    app = CoverLetterWebApp(CoverLetterStore(args.db), password_hash, secret_key, secure_cookie=args.host not in {"127.0.0.1", "localhost"}, sync_token=sync_token)
    with make_server(args.host, args.port, app) as server:
        print(f"Serving on http://{args.host}:{args.port}")
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
