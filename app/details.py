"""Gets the full description of one job, so Gemini can judge it properly
(e.g. whether a "Vollzeit" label is real or just for the semester break).

Arbeitsagentur jobs: official details API. Everything else: the job's own page
(same polite rules as always: robots.txt, delays, no getting around blocks).
"""
import json

from bs4 import BeautifulSoup

from . import arbeitsagentur
from .extractors import clean, html_to_text
from .extractors.generic import _walk
from .fetcher import fetch_html, robots_allows

MAX_CHARS = 8000


def fetch_description(url: str) -> str:
    if url.startswith(arbeitsagentur.DETAIL_URL):
        return arbeitsagentur.fetch_description(url)
    if not robots_allows(url):
        return ""
    page = fetch_html(url)
    if not page.ok:
        return ""
    return page_text(page.html)


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Best case: structured job data with the description.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "", strict=False)
        except ValueError:
            continue
        for posting in _walk(data):
            if posting.get("description"):
                return html_to_text(posting["description"], MAX_CHARS)
    # Otherwise: the main text of the page, without menus and footers.
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return clean(main.get_text(" "), MAX_CHARS)
