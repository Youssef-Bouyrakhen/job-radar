"""Two free web helpers for finding careers pages, both used politely and counted.

1. TinyFish Search (key TINYFISH_API_KEY in .env, free up to 12,000 searches a day, 30 a minute):
   "<company> Karriere" when a company's own website shows no careers page. Off without the key.
2. Jina Reader (r.jina.ai, free without a key, 20 pages a minute): reads a page the way a
   browser shows it, for job lists built with JavaScript. Only pages robots.txt lets us read,
   never a page that refused us (bot protection stays respected).
"""
import logging
import os
import threading
import time
from collections import Counter
from datetime import date, datetime
from .clock import GERMANY  # the free searches start again at midnight

import httpx

from .fetcher import USER_AGENT, robots_allows

log = logging.getLogger("jobradar")

SEARCH_URL = "https://api.search.tinyfish.ai"
SEARCH_GAP = 3.0   # seconds between searches: 20 a minute, TinyFish allows 30
JINA_URL = "https://r.jina.ai/"
JINA_GAP = 3.2     # seconds between pages: under Jina's 20 a minute without a key

usage = Counter()  # searches / jina / search_errors / jina_errors since the app started
_lock = threading.Lock()
_last = {"search": 0.0, "jina": 0.0}


def _pace(kind: str, gap: float) -> None:
    with _lock:
        wait = _last[kind] + gap - time.monotonic()
        _last[kind] = time.monotonic() + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


def search_enabled() -> bool:
    return bool(os.environ.get("TINYFISH_API_KEY"))


class LimitReached(Exception):
    """Today's free searches are used up: the company waits for the next night (it is not saved as checked)."""


_limit_day = None  # the German date the free searches ran out


def _today() -> date:
    return datetime.now(GERMANY).date()


def limit_reached() -> bool:
    return _limit_day == _today()


def search(query: str) -> list[dict]:
    """[{url, title, snippet}], [] if search is off or fails (it is only a last step).
    Raises LimitReached when today's free searches are used up."""
    global _limit_day
    if not search_enabled():
        return []
    if limit_reached():
        raise LimitReached("the free web searches of today are used up")
    _pace("search", SEARCH_GAP)
    usage["searches"] += 1
    try:
        r = httpx.get(SEARCH_URL, params={"query": query, "location": "DE", "language": "de"},
                      headers={"X-API-Key": os.environ["TINYFISH_API_KEY"]}, timeout=30)
        r.raise_for_status()
        return [x for x in r.json().get("results", []) if x.get("url")]
    except httpx.HTTPStatusError as e:
        # 429 = too many (we stay under the per-minute limit, so: the day's), 402 = the free amount is used up
        if e.response.status_code in (402, 429):
            _limit_day = _today()
            log.warning("Web search: the free searches of today are used up (HTTP %d): the rest waits for "
                        "the next night", e.response.status_code)
            raise LimitReached(f"the free web searches of today are used up (HTTP {e.response.status_code})") from e
        usage["search_errors"] += 1
        log.info("Web search for %r failed (%s)", query, type(e).__name__)
        return []
    except (httpx.HTTPError, ValueError) as e:
        usage["search_errors"] += 1
        log.info("Web search for %r failed (%s)", query, type(e).__name__)
        return []


def read_page(url: str) -> str:
    """The page as Markdown text, read by Jina Reader. "" if not allowed or it failed."""
    if not robots_allows(url):
        return ""
    _pace("jina", JINA_GAP)
    usage["jina"] += 1
    try:
        r = httpx.get(JINA_URL + url, headers={"User-Agent": USER_AGENT, "X-Return-Format": "markdown"}, timeout=60)
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as e:
        usage["jina_errors"] += 1
        log.info("Jina Reader could not read %s (%s)", url, type(e).__name__)
        return ""
