"""Downloads web pages politely.

1. A normal HTTP request (fast, cheap).
2. Only if that finds nothing: a hidden Chrome browser via Playwright, for pages
   that build their job list with JavaScript.

We never try to get around logins, CAPTCHAs or bot protection. If a site blocks
us, we report it and the link is marked as not trackable.
"""
import json
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib.util import find_spec
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import CONFIG, ROOT

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
HTML_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
API_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, */*"}
# The Company Discovery Engine visits many unknown websites, so it says honestly who it is
BOT_NAME = "JobRadar-CompanyDiscovery"
BOT_HEADERS = {
    "User-Agent": f"{BOT_NAME}/1.0 (private job search tool; respects robots.txt; max 1 request/s per site)",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Text that shows up on "are you a robot?" pages from Cloudflare, DataDome, PerimeterX, etc.
BLOCK_MARKERS = (
    "just a moment...", "cf-chl", "px-captcha", "captcha-delivery", "datadome",
    "verify you are human", "are you a robot", "unusual traffic", "access denied",
    "request unsuccessful", "incapsula", "sicherheitsüberprüfung",
)
LOGIN_WORDS = ("login", "signin", "sign-in", "anmelden", "/auth")


@dataclass
class Page:
    ok: bool
    url: str
    html: str = ""
    status: int = 0
    reason: str = ""


# ---- politeness: wait between requests to the same website -------------------

_last_request: dict[str, float] = {}
_lock = threading.Lock()


def _polite_wait(url: str, delay: float) -> None:
    host = urlparse(url).netloc
    with _lock:
        now = time.monotonic()
        wait = max(0.0, _last_request.get(host, 0.0) + delay - now)
        _last_request[host] = now + wait
    if wait:
        time.sleep(wait + random.uniform(0, 1.0))


_robots: dict[str, RobotFileParser | None] = {}


def robots_allows(url: str, agent: str = "*") -> bool:
    """Respect the website's robots.txt (its rules for automated visitors)."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        parser = None
        try:
            headers = BOT_HEADERS if agent == BOT_NAME else HTML_HEADERS
            r = httpx.get(base + "/robots.txt", headers=headers, timeout=10, follow_redirects=True)
            if r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                parser = RobotFileParser()
                parser.parse(r.text.splitlines())
        except (httpx.HTTPError, ValueError):  # ValueError: a malformed address (e.g. a part over 63 characters)
            pass
        _robots[base] = parser
    parser = _robots[base]
    return parser is None or parser.can_fetch(agent, url)


def robots_sitemaps(url: str) -> list[str]:
    """The sitemap addresses a site lists in its robots.txt (after robots_allows was asked)."""
    p = urlparse(url)
    parser = _robots.get(f"{p.scheme}://{p.netloc}")
    return (parser.site_maps() or []) if parser else []


# ---- checks on the downloaded page --------------------------------------------

def detect_block(status: int, html: str) -> str | None:
    if status == 429:
        return "The site says we made too many requests (rate limited)"
    if status in (401, 403):
        return f"The site refused access (HTTP {status}), probably bot protection or a login"
    if len(html) < 30000 and any(m in html[:30000].lower() for m in BLOCK_MARKERS):
        return "Blocked by bot protection / CAPTCHA page"
    return None


def _is_login_redirect(original: str, final: str) -> bool:
    before, after = original.lower(), final.lower()
    return any(w in after and w not in before for w in LOGIN_WORDS)


def _check(url: str, final_url: str, status: int, html: str) -> Page:
    block = detect_block(status, html)
    if block:
        return Page(False, final_url, html, status, block)
    if _is_login_redirect(url, final_url):
        return Page(False, final_url, html, status, "The page redirects to a login")
    if status >= 400:
        return Page(False, final_url, html, status, f"The site answered with an error (HTTP {status})")
    return Page(True, final_url, html, status)


# ---- downloading --------------------------------------------------------------

def fetch_html(url: str) -> Page:
    _polite_wait(url, CONFIG["fetch"]["delay_per_site_seconds"])
    try:
        with httpx.Client(headers=HTML_HEADERS, timeout=CONFIG["fetch"]["timeout_seconds"],
                          follow_redirects=True) as client:
            r = client.get(url)
    except httpx.TimeoutException:
        return Page(False, url, reason="The site took too long to answer")
    except httpx.ConnectError:
        return Page(False, url, reason="Could not connect to the site (is the address correct?)")
    except (httpx.HTTPError, ValueError) as e:  # ValueError: a malformed address
        return Page(False, url, reason=f"Could not connect to the site ({type(e).__name__})")
    return _check(url, str(r.url), r.status_code, r.text)


def fetch_polite(url: str, max_bytes: int = 3_000_000) -> Page:
    """For the Company Discovery Engine: honest User-Agent, robots.txt, at most one request
    per second per site, and big files (huge sitemaps) are cut off instead of downloaded."""
    if not robots_allows(url, BOT_NAME):
        return Page(False, url, reason="robots.txt asks automated visitors not to load this page")
    _polite_wait(url, CONFIG["companies"]["delay_per_site_seconds"])
    try:
        with httpx.Client(headers=BOT_HEADERS, timeout=15, follow_redirects=True) as client:
            with client.stream("GET", url) as r:
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        break
                text = body.decode(r.encoding or "utf-8", errors="replace")
                return _check(url, str(r.url), r.status_code, text)
    except httpx.TimeoutException:
        return Page(False, url, reason="The site took too long to answer")
    except (httpx.HTTPError, ValueError) as e:  # ValueError: a malformed address
        return Page(False, url, reason=f"Could not connect to the site ({type(e).__name__})")


def fetch_api(url: str, method: str = "GET", body: dict | None = None, as_json: bool = True,
              params: dict | None = None, headers: dict | None = None):
    """Read an official job feed. Raises an exception if it fails."""
    _polite_wait(url, CONFIG["fetch"]["delay_per_api_seconds"])
    with httpx.Client(headers={**API_HEADERS, **(headers or {})}, timeout=CONFIG["fetch"]["timeout_seconds"],
                      follow_redirects=True) as client:
        r = client.request(method, url, json=body, params=params)
    r.raise_for_status()
    return r.json() if as_json else r.content


def browser_available() -> bool:
    return find_spec("playwright") is not None


def fetch_with_browser(url: str) -> Page:
    """Open the page in a hidden Chrome. Runs in a separate process so a browser
    crash can never take down the app."""
    _polite_wait(url, CONFIG["fetch"]["delay_per_site_seconds"])
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.render", url], cwd=ROOT, capture_output=True,
            encoding="utf-8", errors="replace", timeout=CONFIG["fetch"]["browser_timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        return Page(False, url, reason="The browser took too long to load the page")
    if proc.returncode != 0:
        err = proc.stderr or ""
        if "Executable doesn't exist" in err or "playwright install" in err:
            return Page(False, url, reason="The browser is not installed yet (run setup.bat)")
        last_line = err.strip().splitlines()[-1] if err.strip() else "unknown error"
        return Page(False, url, reason=f"The browser could not load the page: {last_line[:150]}")
    data = json.loads(proc.stdout)
    return _check(url, data["final_url"], data["status"], data["html"])
