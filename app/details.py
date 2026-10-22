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
from functools import cache
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from . import arbeitsagentur, db
from .config import CONFIG
from .extractors import clean, html_to_text
from .extractors.generic import _walk
from .fetcher import fetch_html, robots_allows
from .filters import hours_of

log = logging.getLogger("jobradar")

MAX_CHARS = 8000
# schema.org employmentType -> the words the filter and the AI know
EMPLOYMENT = {"FULL_TIME": "Vollzeit", "PART_TIME": "Teilzeit", "INTERN": "Praktikum", "TEMPORARY": "befristet",
              "CONTRACTOR": "freie Mitarbeit", "PER_DIEM": "Aushilfe"}
WORKDAY_PLACE = re.compile(r"myworkdayjobs\.com/.*?/job/([^/]+)/")
# SuccessFactors career sites (Fraunhofer, ...): /job/Stuttgart-Student-Assistant-...-70569/1449749733/
CITY_TITLE_PLZ = re.compile(r"/job/([^/]+)-(\d{5})/\d+/?$")


@dataclass
class Details:
    description: str = ""
    location: str = ""      # e.g. "Koblenz", "Remote"; "" = the page doesn't say
    employment: str = ""    # e.g. "Vollzeit", "Teilzeit"


def fetch_details(url: str, title: str = "") -> Details:
    if url.startswith(arbeitsagentur.DETAIL_URL):
        return Details(arbeitsagentur.fetch_description(url))  # its jobs always come with a place
    found = Details(location=_place_in_url(url, title))
    if not robots_allows(url):
        return found
    page = fetch_html(url)
    if not page.ok:
        return found
    # "careers#job-working-student-it-..." is the job list: the job's own page has its place
    if (slug := JOB_ANCHOR.search(url)) and (own := _own_page(page.html, page.url, slug[1])) and robots_allows(own):
        if (own_page := fetch_html(own)).ok:
            page = own_page
    return page_details(page.html, found)


JOB_ANCHOR = re.compile(r"#job-([a-z0-9-]{6,})$")


def _own_page(html: str, page_url: str, slug: str) -> str | None:
    """The link on a job list whose address ends with the job's name: /careers/working-student-it-..."""
    for href in re.findall(r"""href=["']([^"'#]+)["']""", html):
        if href.rstrip("/").lower().endswith(slug):
            return urljoin(page_url, href)
    return None


def needs_details(job: dict) -> bool:
    """Only a title or a short teaser, no place or no hours: the full ad tells more."""
    return not job.get("details_fetched") and (len(job["description"] or "") < 400 or not job["location"]
                                               or not job["hours"])


def complete(job: dict) -> None:
    """Reads the job's own page and fills in what the job list didn't say: the full description,
    the city, the job type (Vollzeit / Teilzeit ...) and the hours. Changes `job` and saves it."""
    try:
        found = fetch_details(job["url"], job["title"])
    except Exception as e:
        log.info("Could not read the full ad of '%s' (%s)", job["title"][:60], type(e).__name__)
        found = Details()
    if len(found.description) > len(job["description"] or ""):
        job["description"] = found.description
    job["location"] = job["location"] or found.location
    job["employment"] = job["employment"] or found.employment
    employer = f"{job.get('company') or ''} | {job.get('source') or ''} | {job['url']}"
    if not job["hours"] and (hours := hours_of(job["title"], job["employment"] or "", job["description"] or "", employer)):
        job["hours"] = hours.text
    db.save_details(job)


def _place_in_url(url: str, title: str = "") -> str:
    if (m := WORKDAY_PLACE.search(url)) and not re.search(r"\d", m[1]):
        return m[1].replace("-", " ")
    if m := CITY_TITLE_PLZ.search(unquote(url)):
        # The city is the part before the title: "Sankt-Augustin-Student-Assistant-..." -> "Sankt Augustin"
        first = (re.findall(r"\w+", title) or [""])[0]
        cut = m[1].find(f"-{first}-") if first else -1
        return f"{(m[1][:cut] if cut > 0 else m[1].split('-')[0]).replace('-', ' ')} ({m[2]})"
    return ""


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
    metas = [m.get("content") or "" for m in soup.find_all("meta") if
             (m.get("name") or m.get("property") or "").lower() in META_PLACES]
    # Best case: structured job data with the description, the place and the job type.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "", strict=False)
        except ValueError:
            continue
        for posting in _walk(data):
            if posting.get("description"):
                description = html_to_text(posting["description"], MAX_CHARS)
                return Details(description, _place(posting) or found.location
                               or _hinted_place(description, metas, html), _employment(posting))
    # Otherwise: the main text of the page, without menus and footers.
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    found.description = clean(main.get_text(" "), MAX_CHARS)
    found.location = found.location or _hinted_place(found.description, metas, html)
    return found


# ---- the place when the structured data doesn't give it ----------------------------------

# German cities a job ad names in a "Standort" line or its meta tags (also their English names)
CITIES = ("Berlin", "Hamburg", "München", "Munich", "Köln", "Cologne", "Frankfurt am Main", "Frankfurt", "Stuttgart",
          "Düsseldorf", "Dortmund", "Essen", "Leipzig", "Bremen", "Dresden", "Hannover", "Hanover", "Nürnberg",
          "Nuremberg", "Duisburg", "Bochum", "Wuppertal", "Bielefeld", "Bonn", "Münster", "Mannheim", "Karlsruhe",
          "Augsburg", "Wiesbaden", "Mönchengladbach", "Gelsenkirchen", "Aachen", "Braunschweig", "Kiel", "Chemnitz",
          "Magdeburg", "Freiburg", "Krefeld", "Mainz", "Lübeck", "Erfurt", "Rostock", "Kassel", "Potsdam",
          "Saarbrücken", "Ludwigshafen", "Oldenburg", "Osnabrück", "Leverkusen", "Heidelberg", "Darmstadt",
          "Regensburg", "Paderborn", "Ingolstadt", "Offenbach", "Würzburg", "Ulm", "Heilbronn", "Wolfsburg",
          "Göttingen", "Koblenz", "Erlangen", "Jena", "Trier", "Kaiserslautern", "Hanau", "Gießen", "Marburg", "Worms",
          "Eschborn", "Bad Homburg", "Oberursel", "Rüsselsheim", "Bad Vilbel", "Kelsterbach", "Bensheim", "Walldorf",
          "Sankt Augustin", "Garching", "Unterföhring", "Ismaning", "Böblingen", "Sindelfingen", "Gütersloh")
# A line that names the place: "Standort Munich", "Location: Hamburg", "Arbeitsort: 64283 Darmstadt"
PLACE_LABEL = re.compile(r"\b(?:standorte?|arbeitsort|einsatzort|dienstort|job location|location|office)\b\s*[:\-–]?\s*"
                         r"(.{2,60})", re.I)
META_PLACES = ("description", "og:description", "og:title", "twitter:description", "twitter:title")
# Job-system data in the page: Greenhouse "location": {"name": ...}, Ashby "location": "Munich", "addressLocality"
SYSTEM_PLACE = re.compile(r"""\\?["'](?:location|addressLocality|city|locationName)\\?["']\s*:\s*(?:\{\s*\\?["']name\\?["']\s*:\s*)?"""
                          r"""\\?["']([^"'\\{}]{2,60})\\?["']""", re.I)
FULLY_REMOTE_DATA = re.compile(r"""\\?["']workplaceType\\?["']\s*:\s*\\?["']remote\\?["']""", re.I)


@cache
def _city_pattern() -> re.Pattern:
    names = set(CITIES) | set(CONFIG["companies"]["places"]) | set(CONFIG.get("region", {}).get("towns", {}))
    names = {n for n in names if " Airport" not in n}
    return re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")(?![\w-])", re.I)


POSTCODE_PLACE = re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][\wäöüß.-]+(?: (?:am|an der|im|bei) [A-ZÄÖÜ][\wäöüß.-]+)?)")


def _city_in(text: str) -> str:
    """A known city in this text, else ''."""
    m = _city_pattern().search(text)
    return m[1] if m else ""


def _hinted_place(text: str, metas: list[str], html: str) -> str:
    """The place from the ad's "Standort / Location" line, its meta tags, or the job system's data in the page."""
    for m in PLACE_LABEL.finditer(text or ""):
        # After the label also any town with its postcode: "Arbeitsort: 64331 Weiterstadt"
        if place := _city_in(m[1]) or (f"{p[1]} {p[2]}" if (p := POSTCODE_PLACE.match(m[1].strip())) else ""):
            return place
    for content in metas:
        if place := _city_in(content):
            return place
    # The job system's data only on the page of ONE job (a job list has many, in many places)
    if len(re.findall(r"workplaceType", html)) > 1:
        return ""
    if FULLY_REMOTE_DATA.search(html):
        return "Remote"
    places = list(dict.fromkeys(filter(None, (_city_in(m[1]) for m in SYSTEM_PLACE.finditer(html)))))
    return places[0] if len(places) == 1 else ""
