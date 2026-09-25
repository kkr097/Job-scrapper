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
from datetime import datetime, timedelta, timezone
from http import cookies
from typing import Any, Callable
from wsgiref.simple_server import make_server

from cover_letters import CoverLetterStore, DEFAULT_DB


SESSION_COOKIE = "kk_cover_admin"
MAX_JSON_BYTES = 5_000_000


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
    def __init__(self, store: Any, password_hash: str, secret_key: str, secure_cookie: bool = True, sync_token: str = ""):
        self.store = store
        self.password_hash = password_hash
        self.secret_key = secret_key.encode("utf-8")
        self.secure_cookie = secure_cookie
        self.sync_token = sync_token

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

    def _sync_authorized(self, environ: dict) -> bool:
        supplied = environ.get("HTTP_AUTHORIZATION", "")
        expected = f"Bearer {self.sync_token}" if self.sync_token else ""
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
    def _layout(title: str, content: str, admin: bool = False) -> str:
        nav = '<a href="/">Jobs</a>' + ('<a href="/admin">Private dashboard</a>' if admin else '<a href="/admin">Admin</a>')
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--ink:#0a0a0a;--paper:#fff;--line:#d9d9d9;--muted:#696969}}*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 Arial,sans-serif}}header{{display:flex;justify-content:space-between;align-items:center;padding:22px 5vw;border-bottom:1px solid var(--line)}}
header strong{{font-size:1.4rem;letter-spacing:.08em}}nav{{display:flex;gap:22px}}a{{color:inherit}}main{{max-width:1180px;margin:auto;padding:44px 5vw}}h1{{font-size:clamp(2.3rem,6vw,5.6rem);line-height:.98;max-width:900px;margin:0 0 38px}}h2{{margin-top:34px}}.meta{{color:var(--muted);font-size:.88rem}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:0 0 30px}}.stat{{border:1px solid var(--line);padding:22px}}.stat b{{display:block;font-size:2.5rem}}.toolbar{{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 28px}}select,input,textarea,button{{font:inherit;padding:10px 12px;border:1px solid var(--ink);background:#fff}}button,.pill{{border-radius:999px;background:#0a0a0a;color:#fff;padding:10px 18px;text-decoration:none;display:inline-block}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:14px 10px;border-bottom:1px solid var(--line);vertical-align:top}}.expired{{color:#777}}pre{{white-space:pre-wrap;font:inherit;border:1px solid var(--line);padding:24px}}.card{{border:1px solid var(--line);padding:24px;margin:18px 0}}.error{{color:#9b1c1c}}textarea{{display:block;width:100%;min-height:70px;margin:10px 0}}@media(max-width:760px){{table,thead,tbody,tr,th,td{{display:block}}thead{{display:none}}td{{padding:5px 0;border:0}}tr{{padding:18px 0;border-bottom:1px solid var(--line)}}}}
</style></head><body><header><strong>KK JOBS</strong><nav>{nav}</nav></header><main>{content}</main></body></html>"""

    def _public_home(self, environ: dict, start_response: Callable):
        query = dict(urllib.parse.parse_qsl(environ.get("QUERY_STRING", "")))
        view = query.get("view", "all") if query.get("view") in {"all", "active", "expired"} else "all"
        try:
            min_score = float(query["score"]) if query.get("score") else None
        except ValueError:
            min_score = None
        all_jobs = self.store.public_jobs("all", min_score)
        jobs = all_jobs if view == "all" else [job for job in all_jobs if job["age_status"] == view]
        active_count = sum(job["age_status"] == "active" for job in all_jobs)
        expired_count = sum(job["age_status"] == "expired" for job in all_jobs)
        rows = []
        for job in jobs:
            status_class = "expired" if job["age_status"] == "expired" else ""
            letter = f'<a href="/jobs/{job["job_id"]}/cover-letter">Cover letter sample</a>' if job["status"] == "ready" and job["public_text"] else '<span class="meta">Cover letter unavailable</span>'
            date = str(job.get("posted_at") or job.get("first_seen") or "")[:10]
            rows.append(f'<tr class="{status_class}"><td><a href="{html.escape(job["url"], quote=True)}" rel="noopener noreferrer">{html.escape(job["title"])}</a><div class="meta">{html.escape(job["company"])}</div></td><td>{html.escape(date)}</td><td>{html.escape(job["source"])}</td><td>{html.escape(str(job["score"] or ""))}</td><td>{html.escape(job["age_status"].title())}</td><td>{letter}</td></tr>')
        content = f"""<p class="meta">CURATED JOBS · LAST 28 DAYS</p><h1>Relevant work, without the noise.</h1>
<section class="stats"><div class="stat"><span>Jobs in last 14 days</span><b>{active_count}</b></div><div class="stat"><span>Expired jobs, days 15–28</span><b>{expired_count}</b></div></section>
<form class="toolbar" method="get"><label>Status <select name="view"><option value="all">All</option><option value="active" {'selected' if view=='active' else ''}>Last 14 days</option><option value="expired" {'selected' if view=='expired' else ''}>Days 15–28</option></select></label><label>Minimum score <input name="score" type="number" min="0" max="10" value="{html.escape(query.get('score',''))}"></label><button>Filter</button></form>
<table><thead><tr><th>Job</th><th>Date</th><th>Source</th><th>Score</th><th>Status</th><th>Letter</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="6">No jobs match these filters.</td></tr>'}</tbody></table>"""
        return self._response(start_response, self._layout("KK Jobs", content))

    def _public_letter(self, job_id: str, start_response: Callable):
        job = self.store.get_public_job(job_id)
        if not job or job["status"] != "ready" or not job["public_text"]:
            return self._response(start_response, self._layout("Not available", "<h1>Cover letter unavailable.</h1>"), "404 Not Found")
        content = f'<p class="meta">REDACTED SAMPLE</p><h1>{html.escape(job["title"])}</h1><pre>{html.escape(job["public_text"])}</pre><p class="meta">Personal contact details and signature are intentionally omitted.</p>'
        return self._response(start_response, self._layout("Cover letter sample", content))

    def _login(self, environ: dict, start_response: Callable):
        if environ["REQUEST_METHOD"] == "POST":
            form = self._read_form(environ)
            if self.password_hash and verify_password(form.get("password", ""), self.password_hash):
                token = self._new_session()
                secure = "; Secure" if self.secure_cookie else ""
                cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}"
                return self._redirect(start_response, "/admin", [("Set-Cookie", cookie)])
            error = '<p class="error">Invalid password.</p>'
        else:
            error = ""
        content = f'<p class="meta">PRIVATE AREA</p><h1>Admin sign in</h1>{error}<form method="post"><label>Password <input type="password" name="password" required autocomplete="current-password"></label> <button>Sign in</button></form>'
        return self._response(start_response, self._layout("Admin sign in", content))

    def _admin(self, environ: dict, start_response: Callable):
        if not self._session_valid(environ):
            return self._redirect(start_response, "/admin/login")
        csrf = self._csrf(environ)
        jobs = self.store.admin_jobs()
        applied_count = sum(bool(job["applied"]) for job in jobs)
        cards = []
        for job in jobs:
            full = html.escape(job["full_text"] or "Cover letter unavailable")
            checked = "checked" if job["applied"] else ""
            cards.append(f"""<section class="card"><h2><a href="{html.escape(job['url'], quote=True)}">{html.escape(job['title'])}</a></h2>
<p class="meta">{html.escape(job['company'])} · score {html.escape(str(job['score'] or ''))} · letter {html.escape(job['status'])}</p>
<details><summary>Cover letter</summary><pre id="letter-{job['job_id']}">{full}</pre><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('letter-{job['job_id']}').innerText)">Copy</button></details>
<form method="post" action="/admin/jobs/{job['job_id']}/application"><input type="hidden" name="csrf" value="{csrf}"><label><input type="checkbox" name="applied" value="1" {checked}> Applied</label><textarea name="notes" placeholder="Private notes">{html.escape(job['notes'] or '')}</textarea><button>Save</button></form>
<form method="post" action="/admin/jobs/{job['job_id']}/regenerate"><input type="hidden" name="csrf" value="{csrf}"><button>Regenerate cover letter</button></form></section>""")
        stats = f'<section class="stats"><div class="stat"><span>Tracked jobs</span><b>{len(jobs)}</b></div><div class="stat"><span>Applications sent</span><b>{applied_count}</b></div></section>'
        return self._response(start_response, self._layout("Private dashboard", '<p class="meta">ADMIN ONLY</p><h1>Your application dashboard.</h1>' + stats + "".join(cards), admin=True))

    def _admin_action(self, environ: dict, start_response: Callable, job_id: str, action: str):
        if not self._session_valid(environ):
            return self._response(start_response, "Unauthorized", "401 Unauthorized")
        form = self._read_form(environ)
        if not hmac.compare_digest(form.get("csrf", ""), self._csrf(environ)):
            return self._response(start_response, "Invalid CSRF token", "403 Forbidden")
        if action == "regenerate":
            self.store.regenerate(job_id)
        elif action == "application":
            self.store.set_application(job_id, form.get("applied") == "1", form.get("notes", ""))
        return self._redirect(start_response, "/admin")

    def _sync_api(self, environ: dict, start_response: Callable):
        if not self._sync_authorized(environ):
            return self._json_response(start_response, {"error": "unauthorized"}, "401 Unauthorized")
        try:
            payload = self._read_json(environ)
            jobs = payload.get("jobs")
            if not isinstance(jobs, list):
                raise ValueError("jobs must be a list")
            if len(jobs) > 2500:
                raise ValueError("too many jobs in one request")
            return self._json_response(start_response, {"synced": self.store.sync_jobs(jobs)})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json_response(start_response, {"error": str(exc)}, "400 Bad Request")

    def __call__(self, environ: dict, start_response: Callable):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if method == "GET" and path == "/healthz":
            return self._json_response(start_response, {"status": "ok"})
        if method == "GET" and path == "/robots.txt":
            return self._response(start_response, "User-agent: *\nDisallow: /\n")
        if path == "/api/v1/state" and method == "GET":
            if not self._sync_authorized(environ):
                return self._json_response(start_response, {"error": "unauthorized"}, "401 Unauthorized")
            return self._json_response(start_response, self.store.cloud_state())
        if path == "/api/v1/sync" and method == "POST":
            return self._sync_api(environ, start_response)
        if method == "GET" and path == "/":
            return self._public_home(environ, start_response)
        match = re.match(r"^/jobs/([a-f0-9]{24})/cover-letter$", path)
        if method == "GET" and match:
            return self._public_letter(match.group(1), start_response)
        if path == "/admin/login" and method in {"GET", "POST"}:
            return self._login(environ, start_response)
        if path == "/admin" and method == "GET":
            return self._admin(environ, start_response)
        match = re.match(r"^/admin/jobs/([a-f0-9]{24})/(regenerate|application)$", path)
        if method == "POST" and match:
            return self._admin_action(environ, start_response, match.group(1), match.group(2))
        return self._response(start_response, self._layout("Not found", "<h1>Not found.</h1>"), "404 Not Found")


def create_application() -> CoverLetterWebApp:
    password_hash = os.getenv("COVER_LETTER_ADMIN_PASSWORD_HASH", "")
    secret_key = os.getenv("COVER_LETTER_SECRET_KEY", "")
    sync_token = os.getenv("COVER_LETTER_SYNC_TOKEN", "")
    if not password_hash or not secret_key or not sync_token:
        raise RuntimeError("Admin password hash, session secret, and sync token are required")
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        from cloud_store import PostgresCoverLetterStore
        store = PostgresCoverLetterStore(database_url)
    else:
        store = CoverLetterStore(os.getenv("COVER_LETTER_DB", DEFAULT_DB))
    return CoverLetterWebApp(store, password_hash, secret_key, secure_cookie=True, sync_token=sync_token)


def main() -> int:
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="Serve the KK Jobs cover-letter website")
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
