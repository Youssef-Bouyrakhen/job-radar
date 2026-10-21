"""Phase 8: "Find new sources" on the Watched links page.

1. Gemini suggests IT employers around Darmstadt / Frankfurt and the careers links it knows.
   Companies that show up often in your email alerts and Arbeitsagentur results are added.
2. Job Radar also tries the usual applicant-system addresses (e.g. name.jobs.personio.de).
3. Every link is loaded and tested like a watched link. Links that don't load are dropped,
   so no made-up URL survives. The best working link per company is kept.
"""
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from . import ai, arbeitsagentur, db, gmail
from .extractors import ats
from .fetcher import browser_available, fetch_html, robots_allows
from .filters import get_filter
from .source_types import is_portal
from .sources import check_url

log = logging.getLogger("jobradar")

COMPANIES_TO_ASK = 30
WORKERS = 4  # companies tested at the same time (each website still gets its polite delay)

# A job counts as "good" if it passes your keyword filter AND is in the area (or remote).
# An empty location counts too: many feeds don't say.
NEARBY = re.compile(
    r"darmstadt|frankfurt|offenbach|eschborn|r[üu]e?sselsheim|mainz|wiesbaden|hanau|langen|dreieich|"
    r"neu-isenburg|bad homburg|oberursel|weiterstadt|griesheim|bad vilbel|kelsterbach|raunheim|"
    r"rhein-main|hessen|hesse|remote|home ?office|mobil|"
    # nationwide jobs ("Bundesweit") can be done from here too
    r"bundesweit|deutschlandweit|nationwide|germany[- ]wide|[üu]berregional", re.I)

LEGAL_FORMS = re.compile(
    r"\b(gmbh|mbh|ag|se|kgaa|kg|co|ug|ohg|eg|ev|e\.v|inc|ltd|llc|plc|group|gruppe|holding|"
    r"deutschland|germany|international|hrdirekt)\b", re.I)
# "Company" values in email alerts that are really job titles (the parser sometimes mixes them up)
NOT_A_COMPANY = re.compile(r"\([mwfdx/ ]+\)|all genders|werkstudent|working student|jobs similar|"
                           r"praktik|intern\b|engineer|developer|entwickler", re.I)
GENERIC_FIRST_WORDS = {"deutsche", "die", "der", "das", "the", "dr", "st"}
# Too common to guess an applicant-system address from ("Software AG" -> not software.jobs.personio.de)
GENERIC_SLUGS = {"software", "data", "digital", "consulting", "cloud", "solutions", "systems", "tech",
                 "technology", "bank", "energy", "media", "it", "services", "network", "security"}

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING"},
            "city": {"type": "STRING"},
            "size": {"type": "STRING", "enum": ["big", "mid", "startup"]},
            "focus": {"type": "STRING"},
            "website": {"type": "STRING"},
            "careers_urls": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["name", "city", "size", "focus", "website", "careers_urls"],
    },
}

PROMPT = """You help a student find employers whose job pages are worth watching.

THE PERSON
{profile}

TASK
List {n} real companies with an office in or near Darmstadt or Frankfurt am Main (also Offenbach,
Eschborn, Rüsselsheim, Mainz, Wiesbaden, Hanau) that regularly hire Werkstudenten (working students)
in IT: software development, data, cloud, DevOps, IT security or IT infrastructure.
Mix big companies, mid-size firms and startups (about a third each).
No staffing or recruiting agencies, no job portals, no universities.
{from_data}
Do NOT list these companies (the person already watches them, rejected them or saw them):
{excluded}

For each company give:
- name: the usual company name
- city: its office near Darmstadt / Frankfurt
- size: "big", "mid" or "startup"
- focus: max 8 words: what IT working students do there
- website: the company's main website, e.g. https://www.example.de
- careers_urls: 1-3 links to the company's own job list, most reliable first. Prefer its
  applicant-system page (Personio, Workday, SmartRecruiters, Greenhouse, Lever, Recruitee,
  SuccessFactors) over a normal careers page. Only links you are confident exist: every link
  is tested and wrong ones are thrown away.
"""

FROM_DATA = """
Also include these employers from the person's own job alerts (number = how often they appeared),
if they are real companies that hire IT working students. Merge spelling variants of the same
employer, skip names that are not real employers:
{companies}
"""


# ---- company names ---------------------------------------------------------------

def _fold(text: str) -> str:
    return (text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))


def _words(name: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", LEGAL_FORMS.sub(" ", _fold(name))).split()


def company_key(name: str) -> str:
    """'DZ BANK AG' -> 'dzbank'. Used to recognise the same company again."""
    return "".join(_words(name))


def _group_key(name: str) -> str:
    """Rougher: 'Heraeus Site Operations GmbH' and 'Heraeus Consulting & IT' -> 'heraeus'."""
    words = _words(name)
    if not words:
        return ""
    short_or_generic = len(words[0]) < 4 or words[0] in GENERIC_FIRST_WORDS
    return "".join(words[:2]) if short_or_generic and len(words) > 1 else words[0]


def _host_key(url: str) -> str:
    skip = {"www", "jobs", "job", "careers", "career", "karriere", "apply", "de", "com", "net", "org", "eu", "io"}
    labels = [l for l in (urlparse(url).hostname or "").split(".") if l not in skip]
    return company_key(labels[0]) if labels else ""


def _watched_keys() -> set[str]:
    keys = set()
    for link in db.list_links():
        if link["kind"] != "url" or is_portal(link["url"]):
            continue
        board = ats.detect(link["url"])
        keys |= {_host_key(link["url"]), company_key(board.key) if board else "", company_key(link["name"] or "")}
    return {k for k in keys if len(k) >= 3}


def _same(a: str, b: str) -> bool:
    """'merck' and 'merckgroup' are the same company; 'sap' and 'sapient' are not."""
    return a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)))


def _is_known(key: str, known: set[str]) -> bool:
    return any(_same(key, k) for k in known)


def companies_from_your_data(limit: int = 15) -> list[tuple[str, int]]:
    """Employers that appear at least twice in your (keyword-filtered) alert / Arbeitsagentur jobs."""
    counts, names = Counter(), {}
    for company in db.companies_in_jobs((gmail.NAME, arbeitsagentur.NAME)):
        group = _group_key(company)
        if len(group) < 2 or NOT_A_COMPANY.search(company):
            continue
        counts[group] += 1
        names.setdefault(group, company)
    return [(names[g], n) for g, n in counts.most_common() if n >= 2][:limit]


# ---- testing links ---------------------------------------------------------------

def _with_search(url: str) -> str:
    """Huge Workday / SmartRecruiters boards: search for Werkstudent jobs, not just the newest 100."""
    board = ats.detect(url)
    if board and board.system in ("Workday", "SmartRecruiters") and not ats.search_terms(url):
        return url + ("&" if "?" in url else "?") + ("q" if board.system == "Workday" else "search") + "=Werkstudent"
    return url


def board_url(b: ats.Board) -> str | None:
    """The direct link to a company's applicant-system job list."""
    return {
        "Personio": lambda: f"https://{b.key}.jobs.personio.{b.extra.get('tld', 'de')}",
        "Greenhouse": lambda: f"https://boards.greenhouse.io/{b.key}",
        "Lever": lambda: f"https://jobs.{'eu.' if b.extra.get('eu') else ''}lever.co/{b.key}",
        "SmartRecruiters": lambda: f"https://jobs.smartrecruiters.com/{b.key}",
        "Recruitee": lambda: f"https://{b.key}.recruitee.com",
        "Workday": lambda: f"https://{b.key}.{b.extra['wd']}.myworkdayjobs.com/"
                           + (f"{b.extra['lang']}/" if b.extra.get("lang") else "") + b.extra["site"],
    }.get(b.system, lambda: None)()


def _belongs_to(board_key: str, company: str) -> bool:
    """Does an applicant-system board found on a careers page belong to this company?
    (Deutsche Börse's page also links to the board of its subsidiary ISS.)"""
    key = company_key(board_key)
    words = [w for w in _words(company) if len(w) >= 3 and w not in GENERIC_FIRST_WORDS | GENERIC_SLUGS]
    initials = "".join(w[0] for w in _words(company))
    return (any(w in key or key in w for w in words if len(key) >= 3)
            or (len(initials) >= 2 and key.startswith(initials)))


def _test(url: str, company: str, need_jobs: bool = False, browser: bool = False) -> dict | None:
    """Load the link like a watched link. None if it doesn't load (or, for guessed
    applicant-system links, has no jobs: then we can't be sure it's the right company)."""
    try:
        result = check_url(url, browser=browser)
    except Exception as e:
        log.info("  %s: failed (%s)", url, e)
        return None
    if result.board and not ats.detect(url):  # an applicant system found inside the page
        direct = board_url(result.board)
        if not _belongs_to(result.board.key, company):
            log.info("  %s: its job list belongs to someone else (%s)", url, result.board.key)
            return None
        if direct:  # watch the applicant system directly: more reliable than the page around it
            return _test(_with_search(direct), company, need_jobs, browser)
    loaded = result.status == "green" or result.reason.startswith("Page loaded") or "feed is empty" in result.reason
    if not need_jobs or result.status == "green":
        log.info("  %s: %s", url, result.reason)
    if not loaded or (need_jobs and result.status != "green"):
        return None
    job_filter = get_filter()
    good = sum(1 for j in result.jobs
               if job_filter.check(j.title, j.employment, j.description).passed
               and (not j.location or NEARBY.search(j.location)))
    return {"url": url, "method": result.method or "Web page", "reason": result.reason,
            "total_jobs": len(result.jobs), "good_jobs": good,
            "state": "red" if result.status != "green" else "green" if good else "amber"}


def _guesses(name: str) -> list[str]:
    """The usual applicant-system addresses for a company name."""
    words = _words(name)
    if len(words) == 1 and words[0] in GENERIC_SLUGS:  # keep the legal form: "software-ag"
        words = re.sub(r"[^a-z0-9]+", " ", _fold(name)).split()
    slugs = [s for s in dict.fromkeys(["-".join(words), "".join(words)]) if 3 <= len(s) <= 63]  # longer: not a valid subdomain
    urls = []
    for s in slugs:
        urls += [f"https://{s}.jobs.personio.de", f"https://{s}.recruitee.com", f"https://boards.greenhouse.io/{s}",
                 f"https://jobs.lever.co/{s}", f"https://jobs.smartrecruiters.com/{s}?search=Werkstudent"]
    return urls


def _rank(t: dict) -> tuple:
    """Best link: good jobs > any jobs > no job list; then applicant systems; then more good jobs."""
    return ({"green": 2, "amber": 1}.get(t["state"], 0), " feed" in t["method"], t["good_jobs"], t["total_jobs"],
            bool(CAREERS_LINK.search(t["url"])))


# Whole words only: "schwachstellen" (vulnerabilities) is not about jobs
# "stellen" only as a whole word (or Stellenangebote...): "weingueter-stellenbosch" is a travel article
CAREERS_LINK = re.compile(r"karriere|career|\bjobs?\b|\bstellen(?:angebote?|b[öo]rse|markt|anzeigen?|portal|suche)?\b|"
                          r"join[- ]us|arbeiten[- ]bei|work[- ]with[- ]us", re.I)
ANCHOR = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def _links_on_website(website: str) -> list[str]:
    """Careers links found on the company's homepage: real links, not guesses.
    Links to an applicant system come first."""
    if not website:
        return []
    home = website if website.startswith("http") else "https://" + website
    if not robots_allows(home):
        return []
    page = fetch_html(home)
    if not page.ok:
        return []
    site = _host_key(page.url)
    found = []
    for href, text in ANCHOR.findall(page.html):
        url = urljoin(page.url, href.strip())
        if not url.startswith("http") or not CAREERS_LINK.search(href + " " + re.sub(r"<[^>]+>", " ", text)):
            continue
        if ats.detect(url) or _host_key(url) == site:
            found.append(url)
    found = list(dict.fromkeys(found))
    return sorted(found, key=lambda u: not ats.detect(u))[:2]


def _best_link(company: dict, watched: set[str]) -> dict | None:
    tried, results = set(), []

    def try_urls(urls: list[str], **kw) -> None:
        for url in urls:
            url = _with_search(url.strip())
            if not url.startswith("http") or url in tried or is_portal(url) or _is_known(_host_key(url), watched):
                continue
            tried.add(url)
            if t := _test(url, company["name"], **kw):
                results.append(t)
                if kw.get("need_jobs"):
                    return  # one working applicant-system guess is enough

    def done() -> bool:  # an applicant system with jobs: the most reliable kind of link
        return any(t["state"] != "red" and " feed" in t["method"] for t in results)

    log.info("Find new sources: %s", company["name"])
    try_urls((company.get("careers_urls") or [])[:3])
    if not done():
        try_urls(_links_on_website(company.get("website") or ""))
    if not done():
        try_urls(_guesses(company["name"]), need_jobs=True)
    if not results:
        return None
    best = max(results, key=_rank)
    # The page loaded but showed no jobs: maybe the list is built with JavaScript.
    if best["state"] == "red" and best["reason"].startswith("Page loaded") and browser_available():
        best = _test(best["url"], company["name"], browser=True) or best
    return best


# ---- the search ------------------------------------------------------------------

_progress = {"running": False, "step": "", "done": 0, "total": 0, "found": 0, "dropped": 0,
             "error": "", "finished_at": None}
_lock = threading.Lock()


def progress() -> dict:
    return dict(_progress)


def start() -> bool:
    """Start a search in the background. False if one is already running."""
    with _lock:
        if _progress["running"]:
            return False
        _progress.update(running=True, step="Asking Gemini for companies…", done=0, total=0, found=0,
                         dropped=0, error="", finished_at=None)
    threading.Thread(target=_run, daemon=True, name="discover").start()
    return True


def _run() -> None:
    try:
        _search()
    except ai.RateLimited:
        _progress["error"] = "Every AI provider's free limit is used up for now. Try again in a few minutes."
    except ai.Overloaded:
        _progress["error"] = "The AI providers' servers are busy right now. Try again in a few minutes."
    except Exception as e:
        log.exception("Finding new sources failed")
        _progress["error"] = f"Something went wrong: {e}"
    finally:
        _progress.update(running=False, step="", finished_at=db.now())


def _search() -> None:
    if not ai.enabled():
        _progress["error"] = "AI is off: add an AI key to .env (or turn it on in config.yaml)."
        return
    watched = _watched_keys()
    # suggested before, added or skipped; and companies you skipped on the Companies page
    known = db.suggestion_keys() | {k for k in map(company_key, db.skipped_company_names()) if len(k) >= 3}
    from_data = [(n, c) for n, c in companies_from_your_data()
                 if not _is_known(company_key(n), watched | known)]
    excluded = sorted({l["name"] for l in db.list_links() if l["kind"] == "url" and l["name"]}
                      | set(db.suggestion_names()))
    prompt = PROMPT.format(
        profile=ai.settings()["profile"].strip(), n=COMPANIES_TO_ASK,
        from_data=FROM_DATA.format(companies="\n".join(f"- {n} ({c}x)" for n, c in from_data)) if from_data else "",
        excluded=", ".join(excluded[:200]) or "(none)")
    answer = ai.ask_json(prompt, SCHEMA, temperature=0.5, priority="jobs")

    data_counts = {_group_key(n): c for n, c in from_data}
    companies, seen = [], set()
    for c in answer:
        key = company_key(c.get("name", ""))
        if len(key) < 2 or key in seen or _is_known(key, watched | known):
            continue
        seen.add(key)
        companies.append({**c, "key": key, "data_count": data_counts.get(_group_key(c["name"]), 0)})
    log.info("Find new sources: Gemini suggested %d new companies, testing their careers pages", len(companies))
    _progress.update(step="Testing careers pages…", total=len(companies))

    def test_company(c: dict) -> None:
        best = _best_link(c, watched)
        db.add_suggestion({"company": c["name"].strip(), "company_key": c["key"], "city": c.get("city", ""),
                           "size": c.get("size", ""), "focus": c.get("focus", ""), "data_count": c["data_count"],
                           # no working link: remembered (so it isn't suggested again), but never shown
                           **(best or {"url": None, "method": None, "state": None, "reason": "No careers link loaded",
                                       "total_jobs": 0, "good_jobs": 0, "status": "no_link"})})
        if not best:
            log.info("Find new sources: no working careers link for %s", c["name"])
        with _lock:
            _progress["found" if best else "dropped"] += 1
            _progress["done"] += 1

    started = time.monotonic()
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(test_company, companies))
    log.info("Find new sources: %d with a working link, %d dropped (%.0f s)",
             _progress["found"], _progress["dropped"], time.monotonic() - started)
