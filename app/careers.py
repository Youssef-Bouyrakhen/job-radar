"""Company Discovery Engine, part 2: a company's website and its careers page.

1. Website: the OpenStreetMap tag if there is one, otherwise the AI's guess. It only counts
   if it loads and really belongs to the company (its name is in the address or on the page).
2. Careers page: links on the homepage (Karriere, Jobs, Stellen, Careers, Join us), then the
   sitemap, then the usual addresses (/karriere, /jobs, /career).
3. Applicant system: embedded Personio, Join, Softgarden, Recruitee, d.vinci, rexx, onlyfy,
   SmartRecruiters, Greenhouse, Lever, Workday or SuccessFactors boards are recognised and
   their direct job list is saved.

Everything goes through fetch_polite: honest User-Agent, robots.txt, max 1 request/s per
site. No logins, no bot-protection tricks, and only links that actually loaded are saved.
"""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from .discover import ANCHOR, CAREERS_LINK, _belongs_to, _fold, _words, board_url, company_key
from .extractors import ats
from .fetcher import Page, fetch_polite, robots_sitemaps
from .source_types import is_portal

# Social networks, maps, directories: never a company's own website
NOT_A_WEBSITE = ("facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "google.com",
                 "goo.gl", "wikipedia.org", "kununu.com", "northdata.de", "gelbeseiten.de", "dasoertliche.de",
                 "11880.com", "cylex.de", "wlw.de", "firmenwissen.de", "unternehmensregister.de")
PARKED = re.compile(r"domain (?:is|may be) for sale|diese domain (?:kaufen|steht zum verkauf)|"
                    r"domain kaufen|buy this domain|sedoparking|parkingcrew|this domain is parked|"
                    r"domain wurde registriert|hier entsteht eine neue", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
SITE_NAME = re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:site_name|og:title|author)[\"'][^>]+"
                       r"content=[\"']([^\"']+)", re.I)

# Careers sub-pages that are not the main careers page
SUBTOPIC = re.compile(r"faq|video|benefit|blog|news|event|story|stories|podcast|team|testimonial|"
                      r"erfahrung|insight|life|culture|kultur|press|presse|datenschutz|privacy|impressum", re.I)
COMMON_PATHS = ("/karriere", "/jobs", "/career", "/careers", "/de/karriere", "/en/careers", "/stellenangebote",
                "/unternehmen/karriere", "/jobs-karriere", "/karriere/stellenangebote")
# Links on a careers page that lead to the actual list of open jobs
JOB_LIST_LINK = re.compile(r"offene[- ]stellen|stellenangebote|stellenb[öo]rse|jobb[öo]rse|alle[- ]jobs|"
                           r"open[- ]positions|job[- ]openings|vacancies|current[- ]openings|jobs?[- ]finden|"
                           r"zu den (?:jobs|stellen)|jobsuche|job[- ]search", re.I)
MAX_SITEMAP_FILES = 3
# Never a careers page: magazine, blog, news and press articles (they may still mention "Karriere")
EDITORIAL = re.compile(r"(?:^|[/_.-])(?:blog|blogs|magazin|magazine|reisemagazin|news|newsroom|presse?|"
                       r"pressemitteilung(?:en)?|artikel|article|articles|ratgeber|stories|story|podcast)(?:$|[/_.-])", re.I)
# A careers page talks about jobs: at least one of these in its text (or it embeds an applicant system)
JOB_WORDS = re.compile(r"stellenangebot|offene[n]? stellen|\bjobs?\b|karriere|bewerb|open positions|"
                       r"vacanc|job openings|career|ausschreibung", re.I)


@dataclass
class Career:
    career_url: str | None = None
    ats_type: str | None = None
    feed_url: str | None = None
    state: str = "red"           # green: job feed / amber: careers page / red: nothing found
    note: str = ""


# ---- small helpers ---------------------------------------------------------------

def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _site(url: str) -> str:
    """'karriere.acme.de' -> 'acme.de' (good enough for German and .com domains)."""
    return ".".join(host(url).split(".")[-2:])


def normalize_url(url: str) -> str | None:
    url = re.split(r"[;\s]+", (url or "").strip())[0]  # OSM tags can hold several: "a.de;b.de"
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url if "." in host(url) else None


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _fold(text))


def _text_head(html: str, limit: int = 60000) -> str:
    return re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html[:limit]))


# ---- 1. the website ----------------------------------------------------------------

def belongs(page: Page, name: str) -> bool:
    """Does this homepage belong to the company? Its name must be in the address, the page
    title, or the visible text (e.g. the footer "© 2026 Acme GmbH")."""
    key = company_key(name)
    all_words = _words(name)
    words = [w for w in all_words if len(w) >= 4]
    domain = _compact(host(page.url).split(".")[-2] if "." in host(page.url) else host(page.url))
    if key and (key in domain or (len(domain) >= 4 and domain in key)) or any(w in domain for w in words):
        return True
    initials = "".join(w[0] for w in all_words)
    if all_words and len(domain) >= 2 and domain in (all_words[0], initials):  # ey.com, bwi.de
        return True
    title = " ".join(TITLE.findall(page.html[:20000]) + SITE_NAME.findall(page.html[:20000]))
    if key and len(key) >= 4 and (key in _compact(title) or key in _compact(_text_head(page.html))):
        return True
    return len(words) >= 2 and sum(w in _compact(title) for w in words) >= 2


def check_website(url: str | None, name: str) -> tuple[Page | None, str]:
    """(homepage, note). The homepage is None if it doesn't load or isn't this company's."""
    url = normalize_url(url or "")
    if not url:
        return None, "No website address"
    url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"  # the homepage, not a subpage about one office
    if is_portal(url) or any(host(url) == d or host(url).endswith("." + d) for d in NOT_A_WEBSITE):
        return None, f"{host(url)} is a portal or social network, not the company's own website"
    page = fetch_polite(url)
    if not page.ok and url.startswith("https://"):  # some small company sites still have no https
        page = fetch_polite("http://" + url.removeprefix("https://"))
    if not page.ok:
        return None, f"{host(url)}: {page.reason}"
    if PARKED.search(page.html[:20000]):
        return None, f"{host(page.url)} is a parked domain"
    if not belongs(page, name):
        return None, f"{host(page.url)} loaded, but its name doesn't match {name}"
    return page, ""


# ---- 2. applicant systems ------------------------------------------------------------

# System, pattern for its link, then the addresses to try (most direct job list first).
# The pattern's first group is the company's id inside that system.
EXTRA_SYSTEMS = (
    ("Join", r"join\.com/companies/([\w-]+)", ("https://join.com/companies/{k}",)),
    ("Softgarden", r"\b([\w-]+)\.softgarden\.io", ("https://{k}.softgarden.io/de/vacancies", "https://{k}.softgarden.io")),
    ("Softgarden", r"\b([\w-]+)\.career\.softgarden\.de", ("https://{k}.career.softgarden.de",)),
    ("d.vinci", r"\b([\w-]+)\.dvinci-hr\.com", ("https://{k}.dvinci-hr.com/jobPublication/list.json",
                                                "https://{k}.dvinci-hr.com/de/jobs")),
    ("rexx", r"\b([\w-]+)\.rexx-(?:recruitment|systems)\.com", ("https://{k}.rexx-recruitment.com/stellenangebote.html",
                                                                 "https://{k}.rexx-recruitment.com")),
    ("onlyfy", r"\b([\w-]+)\.onlyfy\.jobs", ("https://{k}.onlyfy.jobs",)),
    ("onlyfy", r"\b([\w-]+)\.prescreenapp\.io", ("https://{k}.prescreenapp.io",)),
    ("SuccessFactors", r"(career\d*\.successfactors\.(?:eu|com)/career\?company=[\w-]+)", ("https://{k}",)),
)
NOT_A_BOARD_ID = {"www", "app", "api", "static", "cdn", "assets", "help", "support", "widget", "embed"}


def _feeds_of_known_board(b: ats.Board) -> list[str]:
    """Job-feed addresses of the systems Job Radar can already read, plus the board page."""
    feed = {
        "Personio": lambda: f"https://{b.key}.jobs.personio.{b.extra.get('tld', 'de')}/xml",
        "Greenhouse": lambda: f"https://boards-api.greenhouse.io/v1/boards/{b.key}/jobs",
        "Lever": lambda: f"https://api{'.eu' if b.extra.get('eu') else ''}.lever.co/v0/postings/{b.key}?mode=json",
        "SmartRecruiters": lambda: f"https://api.smartrecruiters.com/v1/companies/{b.key}/postings",
        "Recruitee": lambda: f"https://{b.key}.recruitee.com/api/offers/",
    }.get(b.system, lambda: None)()
    return [u for u in (feed, board_url(b)) if u]


def find_systems(html: str) -> list[tuple[str, str, list[str]]]:
    """Applicant systems linked or embedded in a page: (system, company id, addresses to try)."""
    found = []
    if board := ats.detect(html):
        if board.system != "StudySmarter":  # a job portal, not a company's applicant system
            found.append((board.system, board.key, _feeds_of_known_board(board)))
    text = html.replace("\\/", "/")
    for system, pattern, templates in EXTRA_SYSTEMS:
        for m in re.finditer(pattern, text, re.I):
            key = m[1].rstrip("/")
            if key.lower() in NOT_A_BOARD_ID:
                continue
            urls = [t.format(k=key) for t in templates]
            found.append((system, key, urls))
            break
    return found


def _board_belongs(system: str, key: str, name: str) -> bool:
    if system == "SuccessFactors":
        key = key.rsplit("=", 1)[-1]  # career5.successfactors.eu/career?company=acmeP -> acmeP
    return _belongs_to(key, name)


def _first_loading(urls: list[str]) -> str | None:
    for url in urls:
        page = fetch_polite(url, max_bytes=500_000)
        if page.ok and page.html.strip():
            return page.url if not urlparse(url).query else url  # keep ?mode=json style addresses
    return None


# ---- 3. the careers page ---------------------------------------------------------------

def _career_links(page: Page, pattern: re.Pattern = CAREERS_LINK) -> list[str]:
    """Careers links on a page that stay on the company's own site (or go to an applicant system)."""
    links = []
    for href, text in ANCHOR.findall(page.html):
        url = urljoin(page.url, href.strip())
        if not url.startswith("http"):
            continue
        label = href + " " + re.sub(r"<[^>]+>", " ", text)
        if not pattern.search(label):
            continue
        if (_site(url) == _site(page.url) or find_systems(url)) and not is_editorial(url):
            links.append(url.split("#")[0])
    links = list(dict.fromkeys(links))
    # Applicant systems first, then the main careers page: its address ends with a careers word
    # (/karriere, /jobs-karriere/index.jsp), not a sub-topic (/karriere/faq), shorter first
    def rank(u: str) -> tuple:
        parts = [re.sub(r"\.(html?|php|jsp|aspx?)$", "", p) for p in urlparse(u).path.lower().split("/") if p]
        parts = [p for p in parts if p != "index"]
        last = parts[-1] if parts else ""
        return (not find_systems(u), bool(SUBTOPIC.search(u.lower())), not CAREERS_LINK.search(last), len(u))
    return sorted(links, key=rank)


def _from_sitemap(home: Page) -> list[str]:
    """Careers pages listed in the site's sitemap (only the first few sitemap files)."""
    files = robots_sitemaps(home.url) or [urljoin(home.url, "/sitemap.xml")]
    found, read = [], 0
    while files and read < MAX_SITEMAP_FILES:
        url = files.pop(0)
        read += 1
        page = fetch_polite(url)
        if not page.ok:
            continue
        try:
            root = ET.fromstring(page.html.encode("utf-8", errors="replace"))
        except ET.ParseError:
            continue
        locs = [(e.text or "").strip() for e in root.iter() if e.tag.endswith("loc")]
        if root.tag.endswith("sitemapindex"):  # a list of sitemaps: open the ones about pages first
            files += sorted(locs, key=lambda u: not re.search(r"page|seite|karriere|career|job", u, re.I))
            continue
        found += [u for u in locs if _site(u) == _site(home.url) and CAREERS_LINK.search(urlparse(u).path)
                  and not is_editorial(u)]
    return sorted(dict.fromkeys(found), key=lambda u: len(urlparse(u).path))[:3]


def is_editorial(url: str) -> bool:
    """A magazine / news / press article. Not when the address ends in a careers word:
    /Ueber-uns-und-Presse/Karriere is a careers page, /reisemagazin/weingueter-stellenbosch isn't."""
    path = urlparse(url).path.rstrip("/")
    return bool(EDITORIAL.search(path)) and not CAREERS_LINK.search(path.rsplit("/", 1)[-1])


SCRIPT_REDIRECT = re.compile(r"""(?:window\.|document\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""")


def follow_script_redirect(page: Page) -> Page:
    """Some careers addresses are a tiny page whose script sends the browser on
    (incloud.de/jobs -> join.incloud.de). Follow it once. "/lander" is a parked domain."""
    if not page.ok or len(page.html) > 2000 or not (m := SCRIPT_REDIRECT.search(page.html)):
        return page
    target = urljoin(page.url, m[1])
    if urlparse(target).path.rstrip("/") == "/lander":
        return Page(False, page.url, reason="is a parked domain (the website found for this company is wrong)")
    return fetch_polite(target)


def career_page_problem(page: Page, home_url: str | None = None) -> str | None:
    """Why this isn't a careers page, or None if it is. Not one: a page that redirects back to
    the homepage, a blog / magazine / news / press article, or a page without job words or an
    applicant system."""
    if not page.ok:
        return page.reason or "did not load"
    if home_url and page.url.rstrip("/") == home_url.rstrip("/"):
        return "leads back to the homepage"
    if is_editorial(page.url):
        return "is a blog / magazine / news page"
    if find_systems(page.html):
        return None
    title = " ".join(TITLE.findall(page.html[:20000]))
    if not (CAREERS_LINK.search(urlparse(page.url).path) or CAREERS_LINK.search(title)):
        return "its address and title aren't about careers"
    if not JOB_WORDS.search(_text_head(page.html, len(page.html))):  # the words can come after big scripts
        return "has no job words (Stellenangebote, Jobs, Karriere, Bewerbung, open positions)"
    return None


def _is_career_page(page: Page, home: Page) -> bool:
    return career_page_problem(page, home.url) is None


def _system_on(page: Page, name: str) -> tuple[str, str] | None:
    """(system, job-list address that loaded) for an applicant system on this page."""
    for system, key, urls in find_systems(page.url + " " + page.html):
        if not _board_belongs(system, key, name):
            continue
        if url := _first_loading(urls):
            return system, url
    return None


def find_career(home: Page, name: str) -> Career:
    """Look for the careers page and its applicant system, starting from the homepage."""
    if found := _system_on(home, name):  # the homepage itself embeds the job list
        return Career(home.url, found[0], found[1], "green", f"{found[0]} job list on the homepage")

    candidates = _career_links(home)[:3]
    how = "homepage link"
    if not candidates:
        candidates, how = _from_sitemap(home), "sitemap"
    if not candidates:
        candidates, how = [urljoin(home.url, p) for p in COMMON_PATHS], "usual address"

    tried = 0
    for url in candidates:
        if tried >= 5:
            break
        tried += 1
        if direct := find_systems(url):  # the link goes straight to an applicant system
            system, key, urls = direct[0]
            if _board_belongs(system, key, name) and (feed := _first_loading(urls + [url])):
                return Career(url, system, feed, "green", f"{system} job list ({how})")
            continue
        page = follow_script_redirect(fetch_polite(url))
        if not _is_career_page(page, home):
            continue
        if found := _system_on(page, name):
            return Career(page.url, found[0], found[1], "green", f"{found[0]} job list ({how})")
        # One step further: "Offene Stellen" / "Alle Jobs" often leads to the applicant system
        for deeper in _career_links(page, JOB_LIST_LINK)[:2]:
            if direct := find_systems(deeper):
                system, key, urls = direct[0]
                if _board_belongs(system, key, name) and (feed := _first_loading(urls + [deeper])):
                    return Career(page.url, system, feed, "green", f"{system} job list ({how})")
                continue
            sub = fetch_polite(deeper)
            if sub.ok and (found := _system_on(sub, name)):
                return Career(page.url, found[0], found[1], "green", f"{found[0]} job list ({how})")
        return Career(page.url, None, None, "amber", f"Careers page ({how}), no applicant system found")
    return Career(note="No careers page found (homepage links, sitemap and usual addresses checked)")
