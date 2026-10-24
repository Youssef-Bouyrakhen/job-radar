"""Landing pages: the careers page only says "see our open jobs", the job list is one click deeper.

For a company the diagnosis calls a landing page (diagnosis.py, reason "landing"):
1. The best links to the job list, best first (candidates): a known job system of the company, link words
   ("Offene Stellen", "Stellenangebote", "Alle Jobs", "Zu den freien Stellen", ...), "Careers in Germany" on an
   international page, an address like /jobs or /stellenangebote, then a plain "Karriere" link.
   The link the diagnosis already found goes first.
2. Each one is opened and checked for job titles (jobs_on). None: the next link, and the links on that page.
   At most 2 steps from the landing page and MAX_PAGES pages; only the company's own website, its job system,
   or another site its own page sends you to with clear words ("Finde deinen Job" -> the group's job portal).
3. A "link" that is a JavaScript button: clicked once in the hidden browser, the address it leads to is used.
   A job list that only shows its jobs in a browser is checked there too (list_browser).
4. Found: that page becomes the careers page (the applicant system's list when there is one), the landing page
   is kept in landing_url. The company counts as not read yet, so the next read goes straight to the list.
5. Once a month (CHECK_DAYS) every saved job list is checked. One that broke (gone, or back on the homepage):
   the search starts again from the landing page; nothing found: the landing page is the careers page again.

Runs as a Companies run of kind "landing", started by auto-resume while there is work (due()), so it never
runs during the fixed runs of the day plan. Everything goes through fetch_polite (robots.txt, 1 request/s per site).
"""
import json
import logging
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import careers, db, diagnosis
from .config import CONFIG
from .discover import CAREERS_LINK
from .extractors import ats, clean, generic
from .fetcher import LOGIN_WORDS, Page, browser_available, fetch_polite, fetch_with_browser, robots_allows

log = logging.getLogger("jobradar")

MAX_STEPS = 2       # clicks from the landing page
MAX_PAGES = 6       # pages opened per company
LINKS_PER_PAGE = 4  # best links followed on each page
MAX_BROWSER = 2     # hidden-browser pages per company (clicks and JavaScript job lists)
NO_ADS = ("The careers page loaded and lists no job ads", "The careers page lists no job ads")  # diagnosis.classify
CHECK_DAYS = 30     # a saved job list is checked again after this many days
RETRY_DAYS = 30     # a landing page where nothing was found is searched again after this many days

# "Careers in Germany" / "Karriere in Deutschland" on an international careers page
COUNTRY = re.compile(r"\b(?:germany|deutschland)\b", re.I)
# ... and the other countries on it, never your job list
OTHER_COUNTRY = re.compile(
    r"\b(?:belgi(?:um|en)|france|frankreich|ital(?:y|ien)|pol(?:and|en)|spa(?:in|nien)|swed(?:en)|schweden|"
    r"the uk|united kingdom|gro(?:ß|ss)britannien|england|ireland|irland|the us|usa|united states|netherlands|"
    r"niederlande|nederland|austria|(?:ö|oe)sterreich|switzerland|schweiz|suisse|denmark|d(?:ä|ae)nemark|norway|"
    r"norwegen|finland|finnland|czech|tschechien|hungary|ungarn|romania|rum(?:ä|ae)nien|portugal|china|india|"
    r"indien|japan|singapore|singapur|canada|kanada|australia|australien|brazil|mexico|turkey|t(?:ü|ue)rkei)\b", re.I)
# The address a button opens: onclick="location.href='/jobs'", "window.open('https://...')"
SCRIPT_URL = re.compile(r"""(?:location(?:\.href)?\s*=|window\.open\(|location\.assign\()\s*["']([^"']+)["']""")
# Pages about jobs that are no job list: "Warnung vor gefälschten Stellenangeboten", job alerts
NOT_A_LIST = re.compile(r"warn|betrug|fraud|\bfake|gef[äa]lscht|scam|phishing|job[- ]?alert|jobabo|newsletter|"
                        r"datenschutz|privacy|impressum|imprint|cookie|\bagb\b", re.I)
# Text built in the browser: Vue / Angular placeholders like "{{ jobCount }}"
PLACEHOLDER = re.compile(r"\{\{[^}]{1,80}\}\}")

_lock = threading.Lock()


@dataclass
class Found:
    url: str                       # the page with the job list
    how: str                       # e.g. "12 job titles", "Recruitee job list"
    count: int | None = None       # job titles seen (None: an applicant system that wasn't counted)
    steps: list[str] = field(default_factory=list)  # the link texts clicked, from the landing page
    ats_type: str | None = None
    feed_url: str | None = None
    browser: bool = False          # its jobs only show in a browser
    examples: list[str] = field(default_factory=list)


def _norm(url: str) -> str:
    return url.split("#")[0].rstrip("/").lower()


def _text(el) -> str:
    return clean(el.get_text(" ") or el.get("title") or el.get("aria-label") or el.get("value") or "", 120)


def _target(el, base: str) -> str:
    """The address a link or button leads to, "" when only a click in the browser can tell."""
    if el.name == "a" and (url := generic._resolve(el, base)):
        return url
    for attr in ("data-href", "data-url", "data-link", "data-target-url"):
        if (v := (el.get(attr) or "").strip()) and not v.startswith(("#", "javascript:")):
            return urljoin(base, v).split("#")[0]
    for attr in ("onclick", "href"):
        if m := SCRIPT_URL.search(el.get(attr) or ""):
            return urljoin(base, m[1]).split("#")[0]
    return ""


def _frames(soup: BeautifulSoup, base: str) -> list[str]:
    """Embedded boxes (iframes) that look like a job list."""
    found = []
    for f in soup.find_all("iframe"):
        src = (f.get("src") or f.get("data-src") or "").strip()
        url = urljoin(base, src).split("#")[0] if src and not src.startswith(("about:", "javascript:")) else ""
        if url.startswith("http") and diagnosis.JOB_FRAME.search(urlparse(url).netloc + urlparse(url).path) \
                and not diagnosis.NOT_A_JOB_FRAME.search(url):
            found.append(url)
    return list(dict.fromkeys(found))[:2]


def candidates(page: Page, name: str, first: str | None = None) -> list[dict]:
    """The links on this page that may lead to the job list, best first:
    [{"url" (or "" = a button to click), "text", "score"}]."""
    soup = BeautifulSoup(page.html, "html.parser")
    base = page.url
    if (b := soup.find("base", href=True)) and b["href"].startswith("http"):
        base = b["href"]
    here, site, host = _norm(page.url), careers._site(page.url), urlparse(page.url).hostname
    page_path = urlparse(page.url).path
    best: dict[str, dict] = {}
    elements = soup.find_all(["a", "button"]) + soup.select("[role=button], [onclick], [data-href], [data-url], [data-link]")
    for order, el in enumerate(dict.fromkeys(elements)):
        text = _text(el)
        url = _target(el, base)
        if len(text) > 70 or generic.NOT_A_JOB.search(text) or NOT_A_LIST.search(text + " " + url) \
                or OTHER_COUNTRY.search(text + " " + urlparse(url).path) and not COUNTRY.search(text):
            continue
        words = bool(diagnosis.DEEPER_TEXT.search(text))
        if not url:  # a JavaScript button: only with clear words, clicked in the browser
            href = (el.get("href") or "").strip()
            if words and (el.name != "a" or href in ("", "#") or href.startswith("javascript:")):
                best.setdefault("click:" + text.lower(), {"url": "", "text": text, "score": 4, "order": order})
            continue
        parsed = urlparse(url)
        if _norm(url) == here or generic.NOT_A_JOB.search(url) or careers.is_editorial(url) \
                or any(w in url.lower() for w in LOGIN_WORDS) or re.search(r"\.(?:pdf|jpe?g|png|docx?)$", parsed.path, re.I):
            continue
        if parsed.path.strip("/") == "" and parsed.hostname == host:
            continue  # its own homepage
        if generic.looks_like_job(text, url) and not words:
            continue  # one job ad, not the list
        system = careers.find_systems(url)
        system_ok = bool(system) and careers._board_belongs(system[0][0], system[0][1], name)
        own = careers._site(url) == site
        new_part = "/" + parsed.path[len(os.path.commonprefix([page_path, parsed.path])):].lstrip("/")
        country = bool(COUNTRY.search(text)) and len(text) <= 40
        score = (6 if system_ok else 5 if words else 4 if country else
                 3 if own and diagnosis.DEEPER_PATH.search(new_part) else
                 2 if own and len(text) <= 30 and CAREERS_LINK.search(text) else 0)
        if not score:
            continue
        if not own and not system_ok and (score < 4 or careers.not_own_jobs(url)):
            continue  # another website: only when the company's page sends you there with clear words
        key = _norm(url)
        if key not in best or best[key]["score"] < score:
            best[key] = {"url": url, "text": text or url, "score": score, "order": order}
    for url in _frames(soup, base):  # a box from the job system that holds the list (Offenbach, BBBank)
        best[_norm(url)] = {"url": url, "text": "the embedded job list", "score": 6, "order": -1}
    ranked = sorted(best.values(), key=lambda c: (-c["score"], c["order"]))
    if first and not any(_norm(c["url"]) == _norm(first) for c in ranked[:1]):
        ranked = [{"url": first, "text": "the link the diagnosis found", "score": 7}] + \
                 [c for c in ranked if _norm(c["url"]) != _norm(first)]
    return ranked


def _visible(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return soup.get_text("\n")


def jobs_on(page: Page, name: str, systems: bool = True) -> Found | None:
    """The job list on this page: an applicant system of the company, JobPosting data, job ad links or job titles
    ("... (m/w/d)"); also a list that says it is empty right now. None: no job list here."""
    if systems and (found := careers._system_on(page, name)):
        system, feed = found
        count, works = None, True
        if (board := ats.detect(feed)) and board.system in ats.FETCHERS:
            try:
                count = len(ats.fetch_jobs(board, ""))
            except Exception:
                works = False  # its feed can't be read (Sixpack's Personio: 404): look at the page instead
        if works:
            return Found(page.url, f"{system} job list", count, ats_type=system, feed_url=feed)
    start = page.html.lstrip()[:5]
    if start[:1] in "[{" or start.lower() == "<?xml":  # a JSON / XML file, not a page
        return None
    jobs, _ = generic.extract(page.html, page.url)  # JobPosting data and job ad links
    titles = list(dict.fromkeys(j.title for j in jobs))
    soup = BeautifulSoup(page.html, "html.parser")
    structured = bool(generic._from_json_ld(soup, page.url))
    text = _visible(soup)
    gender = len(generic.GENDER.findall(text))
    if structured or len(titles) >= 2 or gender >= 2 or titles and generic.GENDER.search(titles[0]):
        count = max(len(titles), gender)
        return Found(page.url, f"{count} job title{'s' * (count > 1)}", count, examples=titles[:3])
    if (m := diagnosis.NO_JOBS_TEXT.search(text)) and not NOT_A_LIST.search(m[0] + " " + page.url):
        return Found(page.url, f"says \"{clean(m[0], 80)}\"", 0)
    return None


def _built_with_js(page: Page) -> bool:
    words = len(careers._text_head(page.html, len(page.html)).split())
    return words < diagnosis.FEW_WORDS or bool(diagnosis.JS_APP.search(page.html[:300000])) \
        or len(PLACEHOLDER.findall(page.html[:300000])) >= 2


def _browser(url: str, budget: Counter, click: str | None = None) -> Page | None:
    """The page in the hidden browser (one at a time: it needs a lot of memory). None: not allowed or no budget left."""
    if budget["browser"] >= MAX_BROWSER or not browser_available() or not robots_allows(url, careers.BOT_NAME):
        return None
    budget["browser"] += 1
    with careers._browser:
        careers.usage["browser"] += 1
        page = fetch_with_browser(url, click=click)
    return page if page.ok else None


def _is_home(url: str) -> bool:
    """A website's start page ("/", "/de/", "/en-us/"): it may show a few jobs, but it isn't the job list."""
    return bool(re.fullmatch(r"/?(?:[a-z]{2}(?:[-_][a-z]{2})?/?)?(?:index\.\w+)?", urlparse(url).path, re.I))


def _clicked(page: Page, cand: dict, name: str, seen: set, budget: Counter) -> Found | None:
    """Click a button without an address once in the browser; the address it leads to is the job list
    if it has job titles (in a normal download, or else only in the browser)."""
    shown = _browser(page.url, budget, click=cand["text"])
    if not shown or _norm(shown.url) in seen:
        return None
    seen.add(_norm(shown.url))
    plain = fetch_polite(shown.url)  # what the daily read will get at that address
    if plain.ok and (found := jobs_on(plain, name)):
        return found
    if (found := jobs_on(shown, name, systems=False)) and found.count:
        found.browser = True
        return found
    return None


def _in_frame(shown: Page, name: str, seen: set) -> Found | None:
    """A job list in an embedded box that only the browser shows (added by the cookie banner or a script)."""
    for url in _frames(BeautifulSoup(shown.html, "html.parser"), shown.url):
        if _norm(url) in seen:
            continue
        seen.add(_norm(url))
        frame = fetch_polite(url)
        if frame.ok and (found := jobs_on(frame, name)):
            return found
    return None


def find_list(name: str, start: Page, first: str | None = None, stop=lambda: False) -> Found | None:
    """Look for the job list behind this landing page: at most MAX_STEPS clicks and MAX_PAGES pages.
    Normal downloads first; only if they find nothing, the hidden browser: pages built with JavaScript that a
    strong link led to, and buttons without an address (at most MAX_BROWSER browser pages)."""
    if (found := jobs_on(start, name)) and found.feed_url:  # the careers page is the job system's own page
        return found
    seen, budget = {_norm(start.url)}, Counter()
    queue, later, fallback = [(start, [])], [], None  # later: (page, link, steps) for the browser
    if _built_with_js(start):  # the careers page may be the list itself, built with JavaScript (d-fine)
        later.append((start, None, []))
    while queue:
        page, steps = queue.pop(0)
        for cand in candidates(page, name, first if not steps else None)[:LINKS_PER_PAGE]:
            if budget["pages"] >= MAX_PAGES or stop():
                break
            if not cand["url"]:
                later.append((page, cand, steps))
                continue
            if _norm(cand["url"]) in seen:
                continue
            seen.add(_norm(cand["url"]))
            budget["pages"] += 1
            sub = careers.follow_script_redirect(fetch_polite(cand["url"]))
            if not sub.ok or _is_home(sub.url) and careers.host(sub.url) == careers.host(start.url):
                continue  # broken, or back on the company's homepage
            here = steps + [cand["text"]]
            if found := jobs_on(sub, name):
                found.steps = here
                if not _is_home(sub.url) and (not fallback or (found.count or 0) >= (fallback.count or 0)):
                    return found
                fallback = fallback or found  # e.g. the German homepage with the 5 newest jobs: look for the list
            elif cand["score"] >= 4:  # a strong link to a page without job titles: maybe they load with JavaScript
                later.append((sub, None, here))
            if len(here) < MAX_STEPS:
                queue.append((sub, here))
    if fallback or stop():
        return fallback
    # Pages that look built with JavaScript first, then buttons, then the other pages strong links led to
    later.sort(key=lambda x: 0 if not x[1] and _built_with_js(x[0]) else 1 if x[1] else 2)
    for page, cand, steps in later:
        if budget["browser"] >= MAX_BROWSER or stop():
            break
        if cand:  # a button
            found = _clicked(page, cand, name, seen, budget)
            steps = steps + [cand["text"]]
        elif shown := _browser(page.url, budget):  # a job list built with JavaScript
            found = jobs_on(shown, name, systems=False)
            if found and found.count:
                found.browser, found.url = True, page.url  # the address as linked (the browser may drop "?search=...")
            else:
                found = _in_frame(shown, name, seen)
                if found:
                    steps = steps + ["the embedded job list"]
        else:
            found = None
        if found:
            found.steps = steps
            return found
    return None


# ---- saving, the monthly check ---------------------------------------------------------

def save(c: dict, found: Found, landing_url: str) -> dict:
    """The job list becomes the careers page; the company counts as not read yet (read first next time)."""
    fields = {"career_url": found.url, "landing_url": landing_url, "list_checked_at": db.now(),
              "list_browser": int(found.browser), "landing_tried_at": db.now(),
              "jobs_read_at": None, "jobs_part_hash": None, "jobs_note": None,
              "career_how": (f"Job list behind the careers page ({' > '.join(found.steps)}): {found.how}" if found.steps
                             else f"The careers page is the job list: {found.how}"
                             + (" (shows its jobs only in a browser)" if found.browser else ""))[:300]}
    if found.feed_url:
        fields.update(ats_type=found.ats_type, feed_url=found.feed_url, career_state="green")
    db.update_company(c["id"], fields)
    return fields


def _list_works(c: dict) -> bool:
    """The saved job list still works: its applicant system answers, or the page loads (not on the homepage or
    the landing page) and still has a job list (a list that needs the browser: it loads)."""
    if c["feed_url"] and (board := ats.detect(c["feed_url"])) and board.system in ats.FETCHERS:
        try:
            ats.fetch_jobs(board, "")
            return True
        except Exception:
            pass
    page = careers.follow_script_redirect(fetch_polite(c["career_url"]))
    moved_home = _is_home(page.url) and _norm(page.url) != _norm(c["career_url"])  # sent to a homepage
    to_landing = _norm(page.url) == _norm(c["landing_url"]) != _norm(c["career_url"])  # sent back to the landing page
    if not page.ok or moved_home or to_landing:
        return False
    return bool(c["list_browser"]) or jobs_on(page, c["name"]) is not None


def _search(c: dict, start_url: str, first: str | None, stop) -> Found | None:
    page = careers.follow_script_redirect(fetch_polite(start_url))
    return find_list(c["name"], page, first, stop) if page.ok else None


def _first_link(c: dict) -> str | None:
    """The link to the job list the diagnosis already found on the careers page."""
    d = diagnosis.get(c["id"])
    return ((d.get("seen") or {}).get("deeper") or {}).get("url") if d and d["reason"] == "landing" else None


def check_saved(c: dict, stop=lambda: False) -> str:
    """The monthly check of one saved job list: "ok", "moved" (found again from the landing page) or "back"
    (nothing found: the landing page is the careers page again)."""
    if _list_works(c):
        db.update_company(c["id"], {"list_checked_at": db.now()})
        return "ok"
    if found := _search(c, c["landing_url"], None, stop):
        save(c, found, c["landing_url"])
        log.info("Landing pages: %s: the saved job list %s broke, found again at %s", c["name"], c["career_url"], found.url)
        return "moved"
    if stop():
        return "stopped"  # checked again in the next run
    db.update_company(c["id"], {"career_url": c["landing_url"], "landing_url": None, "list_checked_at": None,
                                "list_browser": None, "landing_tried_at": db.now(), "jobs_part_hash": None,
                                **({"feed_url": None, "ats_type": None, "career_state": "amber"} if c["feed_url"] else {}),
                                "career_how": "The job list behind the careers page broke and none was found again"})
    log.info("Landing pages: %s: the saved job list %s broke, back to the landing page", c["name"], c["career_url"])
    return "back"


def search(c: dict, stop=lambda: False) -> Found | None:
    """Look for the job list behind one landing-page company and save it."""
    found = _search(c, c["career_url"], _first_link(c), stop)
    if found:
        save(c, found, c["landing_url"] or c["career_url"])
        log.info("Landing pages: %s: job list found at %s (%s, %s)", c["name"], found.url, " > ".join(found.steps), found.how)
    elif not stop():
        db.update_company(c["id"], {"landing_tried_at": db.now()})
    return found


# ---- what is due, the run --------------------------------------------------------------

def _older(stamp: str | None, days: int) -> bool:
    return not stamp or datetime.fromisoformat(stamp) < datetime.now(timezone.utc) - timedelta(days=days)


def _searchable(d: dict) -> bool:
    """Diagnosed as a landing page, or "lists no job ads" without a reason (Osborne Clarke: "Careers in Germany")."""
    return d["reason"] == "landing" or d["reason"] == "no_jobs" and (d["detail"] or "").startswith(NO_ADS)


def due() -> tuple[list[dict], list[dict]]:
    """(saved job lists to check this month, landing pages to search: never tried or not for RETRY_DAYS)."""
    landing = {r["company_id"] for r in diagnosis.all_rows() if _searchable(r)}
    rows = db.company_rows()
    checks = [c for c in rows if c["landing_url"] and c["career_url"] and c["status"] != "skipped"
              and _older(c["list_checked_at"], CHECK_DAYS)]
    checked = {c["id"] for c in checks}
    searches = [c for c in rows if c["id"] in landing and c["career_url"] and c["status"] in ("watched", "initiativ")
                and _older(c["landing_tried_at"], RETRY_DAYS) and c["id"] not in checked]
    return checks, searches


def work() -> int:
    checks, searches = due()
    return len(checks) + len(searches)


def landing_count() -> int:
    return sum(1 for r in diagnosis.all_rows() if r["reason"] == "landing")


def run(progress: dict, stop=lambda: False) -> Counter:
    """Check the saved job lists that are due and search behind the landing pages that are due."""
    checks, searches = due()
    if not db.get_setting("landing_before"):  # for "Landing page: 514 -> ?"
        db.set_setting("landing_before", str(landing_count()))
    progress.update(step="Landing pages: looking for the job lists one click deeper…", done=0,
                    total=len(checks) + len(searches))
    stats, touched = Counter(), set()

    def one(job: tuple[str, dict]) -> None:
        kind, c = job
        try:
            if stop():
                return
            if kind == "check":
                result = check_saved(c, stop)
            else:
                result = "found" if search(c, stop) else "not found" if not stop() else "stopped"
        except Exception as e:  # one bad company never stops the others (and isn't tried again in every run)
            log.warning("Landing pages: %s failed (%s)", c["name"], e)
            db.update_company(c["id"], {"list_checked_at" if kind == "check" else "landing_tried_at": db.now()})
            result = "failed"
        with _lock:
            stats[result] += 1
            if result in ("found", "moved", "back"):
                touched.add(c["id"])
            progress["done"] += 1

    with ThreadPoolExecutor(CONFIG["companies"]["workers"]) as pool:
        list(pool.map(one, [("check", c) for c in checks] + [("search", c) for c in searches]))
    # The ones with a new careers page: "not read yet" now (their jobs are read next), not "landing" any more
    diagnosis.diagnose([c for c in db.company_rows() if c["id"] in touched], {}, keep_seen=False)
    result = {"at": db.now(), **stats, "landing_now": landing_count()}
    db.set_setting("landing_last", json.dumps(result))
    log.info("Landing pages: %d searched (%d job lists found), %d saved job lists checked (%d ok, %d moved, %d back "
             "to the landing page), %d failed; %d landing pages left",
             len(searches), stats["found"], len(checks), stats["ok"], stats["moved"], stats["back"], stats["failed"],
             result["landing_now"])
    progress["notes"].append(f"Landing pages: {stats['found']} of {len(searches)} job lists found one click deeper")
    return stats


def status() -> dict:
    """For "Landing page: 514 -> ?": the count before the first run, now, and the last run."""
    before, now = db.get_setting("landing_before"), landing_count()
    found = sum(1 for c in db.company_rows() if c["landing_url"])
    unread = sum(1 for c in db.company_rows() if c["landing_url"] and not c["jobs_read_at"])
    checks, searches = due()
    return {"text": f"Landing page: {before or '?'} → {now}", "landing_before": int(before) if before else None,
            "landing_now": now, "job_lists_saved": found, "job_lists_not_read_yet": unread,
            "due_checks": len(checks), "due_searches": len(searches),
            "last_run": json.loads(db.get_setting("landing_last") or "null")}
