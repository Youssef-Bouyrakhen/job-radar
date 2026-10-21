"""Company Discovery Engine, part 2: a company's website and its careers page.

1. Website: the OpenStreetMap tag if there is one, otherwise the AI's guess. It only counts
   if it loads and really belongs to the company (its name is in the address or on the page).
   A short address (eb-sim.de for "EB - Sustainable Investment Management") counts when its
   Impressum names the company.
2. Careers page, in this order; the first that works wins and its step is saved (STEPS):
   1 homepage links (Karriere, Jobs, Stellen, ...), also to another website (c24.de -> powerappsportals.com)
   2 the usual addresses: jobs. / karriere. / career(s). subdomains, the sitemap, /karriere, /jobs ...
   3 the homepage in a hidden browser, when its menu is built with JavaScript
   4 known job systems guessed from the name (Personio, Softgarden, Join, Recruitee, ...)
   5 a web search "<company> Karriere" (TinyFish, free; job portals like Indeed are skipped)
   6 Jina Reader for a careers page built with JavaScript that steps 1-3 found but couldn't read
3. Applicant system: embedded Personio, Join, Softgarden, Recruitee, d.vinci, rexx, onlyfy,
   SmartRecruiters, Greenhouse, Lever, Workday, Power Pages, Oracle Recruiting or SuccessFactors
   boards are recognised and their direct job list is saved.

Everything goes through fetch_polite: honest User-Agent, robots.txt, max 1 request/s per
site. No logins, no bot-protection tricks (a site that refuses us is left alone), and only
links that actually loaded are saved.
"""
import html
import re
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from . import websearch
from .discover import ANCHOR, CAREERS_LINK, _belongs_to, _fold, _words, board_url, company_key
from .extractors import ats, generic
from .fetcher import BOT_NAME, Page, browser_available, fetch_polite, fetch_with_browser, robots_allows, robots_sitemaps
from .source_types import is_portal

# Social networks, maps, directories: never a company's own website
NOT_A_WEBSITE = ("facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "google.com",
                 "goo.gl", "wikipedia.org", "kununu.com", "northdata.de", "gelbeseiten.de", "dasoertliche.de",
                 "11880.com", "cylex.de", "wlw.de", "firmenwissen.de", "unternehmensregister.de")
# Web search results that are never the company's own careers page
NOT_OWN_JOBS = ("indeed.", "stepstone.", "linkedin.", "kununu.", "glassdoor.", "xing.", "jobware.", "monster.",
                "jobteaser.", "studysmarter.", "berufsstart.", "studyflix.", "workingstudentjobs.", "jobstairs.",
                "adzuna.", "kimeta.", "connecticum.", "campusjaeger.", "absolventa.", "meinestadt.", "jooble.",
                "arbeitsagentur.", "northdata.", "wikipedia.", "facebook.", "instagram.", "youtube.", "tiktok.")
PARKED = re.compile(r"domain (?:is|may be) for sale|diese domain (?:kaufen|steht zum verkauf)|"
                    r"domain not available|domain nicht verf[üu]gbar|"
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


STEPS = {1: "homepage link", 2: "usual address", 3: "browser (JavaScript menu)", 4: "known job system",
         5: "web search", 6: "Jina Reader"}
usage = Counter()  # hidden-browser pages since the app started (web search and Jina: websearch.usage)
_browser = threading.Lock()  # one hidden browser at a time: it needs a lot of memory


@dataclass
class Career:
    career_url: str | None = None
    ats_type: str | None = None
    feed_url: str | None = None
    state: str = "red"           # green: job feed / amber: careers page / red: nothing found
    note: str = ""
    step: int | None = None      # how it was found (STEPS), None = not found
    website: str | None = None   # the company's own website, when the web search found it


# ---- small helpers ---------------------------------------------------------------

def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _site(url: str) -> str:
    """'karriere.acme.de' -> 'acme.de' (good enough for German and .com domains)."""
    return ".".join(host(url).split(".")[-2:])


def valid_host(name: str) -> bool:
    """Can this domain be opened at all? Every part 1-63 characters, at most 253 in all, and it
    encodes for DNS. Otherwise httpx raises UnicodeError ("label empty or too long")."""
    if not name or len(name) > 253 or any(not 0 < len(part) <= 63 for part in name.rstrip(".").split(".")):
        return False
    try:
        name.encode("idna")
    except UnicodeError:
        return False
    return True


def normalize_url(url: str) -> str | None:
    url = re.split(r"[;\s]+", (url or "").strip())[0]  # OSM tags can hold several: "a.de;b.de"
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    try:
        name = urlparse(url).hostname or ""
    except ValueError:  # e.g. "https://[broken"
        return None
    return url if "." in name and valid_host(name) else None


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


IMPRESSUM = re.compile(r"impressum|imprint|legal[- ]?notice|anbieterkennzeichnung", re.I)


def _short_form(domain: str, name: str) -> bool:
    """eb-sim.de for "EB - Sustainable Investment Management": the address is a short form of the
    name (its letters in the same order, starting with the same letter)."""
    full = company_key(name)
    if len(domain) < 2 or not full or domain[0] != full[0] or len(domain) >= len(full):
        return False
    letters = iter(full)
    return all(ch in letters for ch in domain)


def impressum_names(home: Page, name: str) -> bool:
    """Does the website's Impressum name the company? (By law every German company website has one.)"""
    links = [urljoin(home.url, href.strip()) for href, text in ANCHOR.findall(home.html)
             if IMPRESSUM.search(href + " " + re.sub(r"<[^>]+>", " ", text))]
    for url in list(dict.fromkeys(links))[:2] or [urljoin(home.url, "/impressum")]:
        if _site(url) != _site(home.url):
            continue
        page = fetch_polite(url)
        if page.ok and company_key(name) in _compact(html.unescape(_text_head(page.html, len(page.html)))):  # "EB &ndash; ..."
            return True
    return False


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
        domain = _compact(host(page.url).split(".")[-2] if "." in host(page.url) else host(page.url))
        if not (_short_form(domain, name) and impressum_names(page, name)):
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


def find_systems(html: str, url: str = "") -> list[tuple[str, str, list[str]]]:
    """Applicant systems linked or embedded in a page: (system, company id, addresses to try)."""
    found = []
    if board := ats.detect(html) or (url and ats.detect_job_site(url, html)):
        if board.system != "StudySmarter":  # a job portal, not a company's applicant system
            key = board.extra.get("site", board.key) if board.system == "Oracle" else board.key
            found.append((board.system, key, _feeds_of_known_board(board)))
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
    """key: the company's id in the system (for Oracle its site name: "career-synlab", or just "CX_1")."""
    if system == "SuccessFactors":
        key = key.rsplit("=", 1)[-1]  # career5.successfactors.eu/career?company=acmeP -> acmeP
    if system == "Oracle" and re.fullmatch(r"(?:CX|careers?|jobs?)(?:_\d+)?", key, re.I):
        return True  # a generic site name: only trusted because the company's own page links to it
    return _belongs_to(key, name)


def _first_loading(urls: list[str]) -> str | None:
    for url in urls:
        page = fetch_polite(url, max_bytes=500_000)
        if page.ok and page.html.strip():
            return page.url if not urlparse(url).query else url  # keep ?mode=json style addresses
    return None


# ---- 3. the careers page ---------------------------------------------------------------

def not_own_jobs(url: str) -> bool:
    """A job portal, social network or directory: never the company's own careers page."""
    h = host(url)
    return is_portal(url) or any(h == d or h.endswith("." + d) for d in NOT_A_WEBSITE) \
        or any(d in h for d in NOT_OWN_JOBS)


def _career_links(page: Page, pattern: re.Pattern = CAREERS_LINK, other_sites: bool = False) -> list[str]:
    """Careers links on a page that stay on the company's own site (or go to an applicant system).
    other_sites: also a "Karriere" link to another website (c24.de -> c24-recruiting.powerappsportals.com)."""
    links = []
    for href, text in ANCHOR.findall(page.html):
        url = urljoin(page.url, href.strip())
        if not url.startswith("http"):
            continue
        words = re.sub(r"<[^>]+>", " ", text)
        if not pattern.search(href + " " + words):
            continue
        own = _site(url) == _site(page.url) or find_systems(url)
        other = other_sites and pattern.search(words) and not not_own_jobs(url)
        if (own or other) and not is_editorial(url):
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
    for system, key, urls in find_systems(page.url + " " + page.html, page.url):
        if not _board_belongs(system, key, name):
            continue
        if url := _first_loading(urls):
            return system, url
    return None


def _js_menu(home: Page) -> bool:
    """A homepage whose menu is built with JavaScript: hardly any links in the downloaded HTML."""
    return len(ANCHOR.findall(home.html)) < 15 or bool(re.search(
        r'id=["\'](?:root|app|__next|__nuxt)["\']|__NEXT_DATA__|ng-version=|data-reactroot|window\.__NUXT__', home.html[:300000]))


def _looks_empty(page: Page) -> bool:
    """A careers address whose text is built with JavaScript: little visible text in the HTML."""
    return len(_text_head(page.html, len(page.html)).split()) < 150


class _Search:
    """The steps for one company: each address is tried once, pages built with JavaScript are
    remembered for Jina Reader (step 6)."""

    def __init__(self, name: str, home: Page | None):
        self.name, self.home = name, home
        self.tried: set[str] = set()
        self.js_pages: list[str] = []

    def career(self, url: str, page: Page, step: int, note: str = "") -> Career:
        how = STEPS[step] + (f": {note}" if note else "")
        if found := _system_on(page, self.name):
            return Career(page.url, found[0], found[1], "green", f"{found[0]} job list ({how})", step)
        # One step further: "Offene Stellen" / "Alle Jobs" often leads to the applicant system
        for deeper in _career_links(page, JOB_LIST_LINK, other_sites=True)[:2]:
            if direct := find_systems(deeper):
                system, key, urls = direct[0]
                if _board_belongs(system, key, self.name) and (feed := _first_loading(urls + [deeper])):
                    return Career(page.url, system, feed, "green", f"{system} job list ({how})", step)
                continue
            sub = fetch_polite(deeper)
            if sub.ok and (found := _system_on(sub, self.name)):
                return Career(page.url, found[0], found[1], "green", f"{found[0]} job list ({how})", step)
        return Career(page.url, None, None, "amber", f"Careers page ({how}), no applicant system found", step)

    def attempt(self, urls: list[str], step: int, limit: int, must_name: bool = False) -> Career | None:
        """Try up to `limit` new addresses. must_name: the page must name the company (another website)."""
        tried = 0
        for url in urls:
            if url in self.tried or tried >= limit:
                continue
            self.tried.add(url)
            tried += 1
            if direct := find_systems(url):  # straight to an applicant system
                system, key, feeds = direct[0]
                if _board_belongs(system, key, self.name) and (feed := _first_loading(feeds + [url])):
                    return Career(url, system, feed, "green", f"{system} job list ({STEPS[step]})", step)
                continue
            page = follow_script_redirect(fetch_polite(url))
            if not page.ok:
                continue
            other = not self.home or _site(page.url) != _site(self.home.url)
            if (must_name or other) and not belongs(page, self.name) and not _system_on(page, self.name):
                continue  # another website that doesn't name the company
            problem = career_page_problem(page, self.home.url if self.home else None)
            if problem and "no job words" in problem and _looks_empty(page):
                self.js_pages.append(page.url)
            if not problem or _system_on(page, self.name):
                return self.career(url, page, step)
        return None

    def run(self) -> Career:
        home, name = self.home, self.name
        if home:
            # 1. homepage links, also to another website
            if found := _system_on(home, name):
                return Career(home.url, found[0], found[1], "green", f"{found[0]} job list on the homepage ({STEPS[1]})", 1)
            if found := self.attempt(_career_links(home, other_sites=True), 1, 4):
                return found
            # 2. the usual addresses: subdomains, sitemap, paths
            site = _site(home.url)
            subdomains = [f"https://{s}.{site}/" for s in ("jobs", "karriere", "career", "careers") if valid_host(f"{s}.{site}")]
            if found := self.attempt(subdomains, 2, 4) or self.attempt(_from_sitemap(home), 2, 3) \
                    or self.attempt([urljoin(home.url, p) for p in COMMON_PATHS], 2, 4):
                return found
            # 3. a menu built with JavaScript: the homepage in a hidden browser
            if _js_menu(home) and browser_available() and robots_allows(home.url, BOT_NAME):
                with _browser:
                    usage["browser"] += 1
                    shown = fetch_with_browser(home.url)
                if shown.ok:
                    if found := _system_on(shown, name):
                        return Career(home.url, found[0], found[1], "green", f"{found[0]} job list ({STEPS[3]})", 3)
                    if found := self.attempt(_career_links(shown, other_sites=True), 3, 4):
                        return found
        # 4. known job systems guessed from the name: only boards that have jobs, a name long enough to be sure,
        # and only systems where each company has its own address (a wrong guess costs their servers nothing)
        for slug in _slugs(name):
            for url in (f"https://{slug}.jobs.personio.de", f"https://{slug}.jobs.personio.com", f"https://{slug}.recruitee.com",
                        f"https://{slug}.softgarden.io/de/vacancies"):
                if found := self._guess(url):
                    return found
        # 5. web search "<company> Karriere"
        if found := self.web_search():
            return found
        # 6. careers pages built with JavaScript: read them with Jina Reader
        for url in self.js_pages[:2]:
            if found := self._jina(url):
                return found
        checked = ("homepage links, usual addresses, " + ("browser, " if _js_menu(home) else "") if home else "") + "job systems" \
                  + (", web search" if websearch.search_enabled() else "") + (", Jina Reader" if self.js_pages else "")
        return Career(note=f"No careers page found ({checked} checked)")

    def _guess(self, url: str) -> Career | None:
        from .sources import check_url  # sources -> ... -> this module: load late
        if url in self.tried or not valid_host(host(url)):
            return None
        self.tried.add(url)
        try:
            result = check_url(url, browser=False)
        except Exception:
            return None
        if result.status != "green":
            return None
        if board := ats.detect(url):
            feeds = _feeds_of_known_board(board)
            return Career(board_url(board) or url, board.system, feeds[0] if feeds else url, "green",
                          f"{board.system} job list ({STEPS[4]})", 4)
        system = find_systems(url)[0][0]  # Softgarden: its page lists the jobs
        return Career(url, system, url, "green", f"{system} job list ({STEPS[4]})", 4)

    def web_search(self) -> Career | None:
        results = websearch.search(f"{self.name} Karriere")
        own = _site(self.home.url) if self.home else None
        ranked = []
        for n, r in enumerate(results):
            url = r["url"]
            if not url.startswith("http") or not_own_jobs(url) or is_editorial(url):
                continue
            if find_systems(url):
                ranked.append((0, n, url))  # a known job system
            elif own and _site(url) == own:
                ranked.append((1, n, url))  # the company's own website
            elif not own and _named_like(host(url), self.name):
                ranked.append((2, n, url))  # a website named like the company (c24.de for C24 Bank)
        found = self.attempt([u for _, _, u in sorted(ranked)], 5, 3, must_name=True)
        if found and not self.home and not find_systems(found.career_url):
            found.website = f"{urlparse(found.career_url).scheme}://{urlparse(found.career_url).netloc}/"
        return found

    def _jina(self, url: str) -> Career | None:
        text = websearch.read_page(url)
        if not text:
            return None
        for system, key, feeds in find_systems(text):
            if _board_belongs(system, key, self.name) and (feed := _first_loading(feeds)):
                return Career(url, system, feed, "green", f"{system} job list ({STEPS[6]})", 6)
        if JOB_WORDS.search(text) and (generic.GENDER.search(text) or generic.JOB_WORD.search(text)):
            return Career(url, None, None, "amber", f"Careers page built with JavaScript ({STEPS[6]})", 6)
        return None


GENERIC_TAILS = ("", "group", "gruppe", "holding", "online", "karriere", "jobs", "career", "careers", "de", "deutschland")


def _named_like(hostname: str, name: str) -> bool:
    """A website named after the company, found by the web search: c24.de for "C24 Bank", acme-group.com
    for "Acme". Not duerrdental.com for "Dürr": another word in the address is another company."""
    domain = _compact(hostname.split(".")[-2] if "." in hostname else hostname)
    plain = name.translate(str.maketrans("äöüÄÖÜ", "aouAOU"))  # durr.com for Dürr (not only duerr.com)
    return bool(domain) and any((len(domain) >= 3 and key.startswith(domain))
                                or (domain.startswith(key) and domain[len(key):] in GENERIC_TAILS)
                                for key in {company_key(name), company_key(plain)} if key)


LEGAL_ONLY = re.compile(r"\b(?:gmbh|mbh|ag|se|kg|kgaa|ug|ohg|gbr|co|e\.?\s?v|haftungsbeschraenkt)\b\.?", re.I)


def _slugs(name: str) -> list[str]:
    """Job-system addresses from the name: "public-cloud", and with words _words leaves out ("publiccloudgroup")."""
    short = _words(name)
    full = re.sub(r"[^a-z0-9]+", " ", LEGAL_ONLY.sub(" ", _fold(name))).split()
    return [s for s in dict.fromkeys(["-".join(full), "".join(full), "-".join(short), "".join(short)]) if 5 <= len(s) <= 63]


def find_career(home: Page | None, name: str) -> Career:
    """Look for the careers page and its applicant system: steps 1-6 (STEPS), the first that works wins.
    home None: the company's website isn't known, only the job systems and the web search are tried."""
    return _Search(name, home).run()
