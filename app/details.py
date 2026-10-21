"""Gets the full description of one job, so Gemini can judge it properly
(e.g. whether a "Vollzeit" label is real or just for the semester break), and the
city and job type when the job list didn't give them.

Arbeitsagentur jobs: official details API. Everything else: the job's own page
(same polite rules as always: robots.txt, delays, no getting around blocks).
"""
import json
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from . import arbeitsagentur, db
from .extractors import clean, html_to_text
from .extractors.generic import _walk
from .fetcher import fetch_html, robots_allows
from .filters import detect_hours

log = logging.getLogger("jobradar")

MAX_CHARS = 8000
# schema.org employmentType -> the words the filter and the AI know
EMPLOYMENT = {"FULL_TIME": "Vollzeit", "PART_TIME": "Teilzeit", "INTERN": "Praktikum", "TEMPORARY": "befristet",
              "CONTRACTOR": "freie Mitarbeit", "PER_DIEM": "Aushilfe"}
WORKDAY_PLACE = re.compile(r"myworkdayjobs\.com/.*?/job/([^/]+)/")


@dataclass
class Details:
    description: str = ""
    location: str = ""      # e.g. "Koblenz", "Remote"; "" = the page doesn't say
    employment: str = ""    # e.g. "Vollzeit", "Teilzeit"


def fetch_details(url: str) -> Details:
    if url.startswith(arbeitsagentur.DETAIL_URL):
        return Details(arbeitsagentur.fetch_description(url))  # its jobs always come with a place
    found = Details(location=_place_in_url(url))
    if not robots_allows(url):
        return found
    page = fetch_html(url)
    if not page.ok:
        return found
    return page_details(page.html, found)


def needs_details(job: dict) -> bool:
    """Only a title or a short teaser, no place or no hours: the full ad tells more."""
    return not job.get("details_fetched") and (len(job["description"] or "") < 400 or not job["location"]
                                               or not job["hours"])


def complete(job: dict) -> None:
    """Reads the job's own page and fills in what the job list didn't say: the full description,
    the city, the job type (Vollzeit / Teilzeit ...) and the hours. Changes `job` and saves it."""
    try:
        found = fetch_details(job["url"])
    except Exception as e:
        log.info("Could not read the full ad of '%s' (%s)", job["title"][:60], type(e).__name__)
        found = Details()
    if len(found.description) > len(job["description"] or ""):
        job["description"] = found.description
    job["location"] = job["location"] or found.location
    job["employment"] = job["employment"] or found.employment
    if not job["hours"] and (hours := detect_hours(f"{job['employment'] or ''} | {job['description'] or ''}")):
        job["hours"] = hours.text
    db.save_details(job)


def _place_in_url(url: str) -> str:
    m = WORKDAY_PLACE.search(url)
    return m[1].replace("-", " ") if m and not re.search(r"\d", m[1]) else ""


def _place(posting: dict) -> str:
    if "TELECOMMUTE" in str(posting.get("jobLocationType") or "").upper():
        return "Remote"
    places = posting.get("jobLocation") or []
    cities = []
    for p in places if isinstance(places, list) else [places]:
        address = p.get("address") if isinstance(p, dict) else None
        city = address.get("addressLocality") if isinstance(address, dict) else address if isinstance(address, str) else None
        if isinstance(city, str) and clean(city) and clean(city) not in cities:
            cities.append(clean(city))
    return " / ".join(cities[:3])  # several places: the distance check looks at the first one


def _employment(posting: dict) -> str:
    kinds = posting.get("employmentType") or []
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return ", ".join(dict.fromkeys(EMPLOYMENT[k.upper()] for k in kinds if isinstance(k, str) and k.upper() in EMPLOYMENT))


def page_details(html: str, found: Details | None = None) -> Details:
    found = found or Details()
    soup = BeautifulSoup(html, "html.parser")
    # Best case: structured job data with the description, the place and the job type.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "", strict=False)
        except ValueError:
            continue
        for posting in _walk(data):
            if posting.get("description"):
                return Details(html_to_text(posting["description"], MAX_CHARS),
                               _place(posting) or found.location, _employment(posting))
    # Otherwise: the main text of the page, without menus and footers.
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    found.description = clean(main.get_text(" "), MAX_CHARS)
    return found
