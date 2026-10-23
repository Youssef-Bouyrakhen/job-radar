"""Diagnosis: WHY a Watched / Initiativ company shows 0 jobs for you (no Werkstudent / part-time job in the area).

One reason per company (REASONS), plus two warnings:
- suspicious 0 ("lie detector"): the Arbeitsagentur, LinkedIn or your job alerts show jobs of this company
  in your area, but its careers page gave none.
- page probably changed: its careers page had jobs before and now has 0.
And what the tool saw on the page (`seen`): the address it opened, how many job links it found, the first lines of text.

Kept up to date by every company read (company_jobs.read_jobs); run() diagnoses all 0-job companies at once.
Nothing here changes how jobs are read: it only explains the 0.
"""
import json
import logging
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import careers, db
from .config import CONFIG
from .discover import NEARBY, _same, company_key
from .extractors import ats, clean, generic
from .fetcher import Page, fetch_polite

log = logging.getLogger("jobradar")

REASONS = {
    "no_jobs": "Really no jobs",
    "not_near": "Jobs, none near you",
    "not_student": "Jobs, none for students",
    "landing": "Landing page (jobs one click deeper)",
    "hidden": "Jobs in JavaScript or iframe",
    "broken": "Page moved or broken",
    "unread": "Not read yet",
}
SEEN_LINES = 25         # first lines of page text kept for "See what the tool saw"
FEW_WORDS = 150         # less visible text than this on a JavaScript app page: its text is built with JavaScript
NO_WORDS = 40           # less than this: built with JavaScript, whatever the page looks like
RECENT_DAYS = 30        # jobs from other sources this new count for the lie detector

# A link to the real job list: by its text ...
DEEPER_TEXT = re.compile(
    r"offene[n]?\s+stellen|freie[n]?\s+stellen|aktuelle[n]?\s+stellen|stellenangebot|stellenanzeige|stellenausschreibung|"
    r"stellenb[öo]rse|jobb[öo]rse|job[- ]?portal|karriere[- ]?portal|bewerber[- ]?portal|alle\s+jobs|alle\s+stellen|"
    r"jobs?\s+(?:ansehen|anzeigen|entdecken|finden)|zu\s+den\s+(?:aktuellen\s+)?(?:jobs|stellen)|finde\s+deinen\s+job|"
    r"jobsuche|job\s*search|open\s+positions|current\s+openings|job\s+openings|vacancies|"
    r"(?:view|see|search|explore|browse)\s+(?:all\s+|open\s+)?(?:jobs|roles|positions)", re.I)
# ... or by its address (".../karriere/stellenanzeigen", ".../dabei-sein/jobs")
DEEPER_PATH = re.compile(
    r"/(?:offene-stellen|freie-stellen|stellenangebote?|stellenanzeigen|stellenausschreibungen|stellenboerse|jobboerse|"
    r"jobportal|job-portal|jobs|vacancies|openings|open-positions|jobsuche|job-search)(?:/|\.html?|$)", re.I)
# A box from another site that holds the job list
JOB_FRAME = re.compile(r"job|karriere|career|stellen|recruit|bewerb|vacanc|softgarden|personio|factorial|"
                       r"onlyfy|prescreen|d\.vinci|dvinci|rexx|umantis|workday|successfactors|smartrecruiters|"
                       r"greenhouse|lever\.co|join\.com|recruitee|bamboohr|heyrecruit|coveto|concludis", re.I)
NO_JOBS_TEXT = re.compile(
    r"[^.\n]{0,60}(?:keine\s+(?:offenen|freien|aktuellen)\s+stellen|keine\s+stellen(?:angebote|anzeigen)?|"
    r"(?:derzeit|momentan|aktuell|zurzeit|zur\s+zeit)\s+(?:sind\s+)?(?:leider\s+)?keine|keine\s+vakanzen|"
    r"no\s+(?:open|current)\s+(?:positions|openings|vacancies)|no\s+available\s+jobs|no\s+vacancies)[^.\n]{0,60}", re.I)
# Boxes that never hold jobs (tracking, videos, maps), even when their address says "job-ad-app"
NOT_A_JOB_FRAME = re.compile(r"googletagmanager|doubleclick|google\.com/maps|youtube|vimeo|facebook|recaptcha|hotjar|"
                             r"cookiebot|usercentrics|consent", re.I)
JS_APP = re.compile(r"""id=["'](?:root|app|__next|__nuxt)["']|__NEXT_DATA__|ng-version=|data-reactroot|window\.__NUXT__""", re.I)
BROKEN_NOTE = re.compile(r"took too long|could not connect|error|refused|robots\.txt|parked|did not load|not found", re.I)

_progress = {"running": False, "done": 0, "total": 0, "finished_at": None, "error": None}
_lock = threading.Lock()


# ---- what the tool saw on a page --------------------------------------------------------

def _norm(url: str) -> str:
    return url.split("#")[0].rstrip("/").lower()


def _deeper(soup: BeautifulSoup, page_url: str) -> dict | None:
    """The best link from this page to a job list on another page: {"text", "url", "system"}."""
    here, best, best_score = _norm(page_url), None, 0
    page_path = urlparse(page_url).path
    if (base := soup.find("base", href=True)) and base["href"].startswith("http"):
        page_url = base["href"]  # <base href="https://acme.de/">: relative links start there
    for a in soup.find_all("a", href=True):
        url = generic._resolve(a, page_url)
        parsed = urlparse(url)
        # not this page, not its own homepage (a job portal's start page on a subdomain is fine)
        if not url or _norm(url) == here or (parsed.path.strip("/") == "" and parsed.hostname == urlparse(page_url).hostname):
            continue
        text = clean(a.get_text(" ") or a.get("title") or a.get("aria-label") or "", 80)
        if generic.NOT_A_JOB.search(text) or generic.NOT_A_JOB.search(url) or careers.is_editorial(url):
            continue
        system = careers.find_systems(url)
        own = careers._site(url) == careers._site(page_url)
        if not system and not own and careers.not_own_jobs(url):
            continue  # a job portal or social network, not the company's list
        # the address counts only for its new part: /dabei-sein -> /dabei-sein/jobs, not /jobs/ -> /jobs/support/inquiry
        new_part = "/" + parsed.path[len(os.path.commonprefix([page_path, parsed.path])):].lstrip("/")
        score = 3 if system else 2 if DEEPER_TEXT.search(text) else 1 if own and DEEPER_PATH.search(new_part) else 0
        if score > best_score:
            best_score, best = score, {"text": text or url, "url": url, "system": system[0][0] if system else None}
    return best


def look(url: str, page: Page) -> dict:
    """What the tool saw: the address it opened, where it ended up, job links, boxes, text."""
    seen = {"url": url, "final_url": page.url, "ok": page.ok, "status": page.status, "problem": page.reason or None}
    if not page.ok:
        return seen
    html = page.html
    if len(html) < 2000 and (m := careers.SCRIPT_REDIRECT.search(html)):  # incloud.de/jobs -> join.incloud.de
        seen["script_redirect"] = urljoin(page.url, m[1])
    if html.lstrip()[:1] in "[{" or html.lstrip()[:5].lower() == "<?xml":  # a JSON / XML job list
        seen.update(data=True, words=len(html.split()), lines=html[:1500].splitlines()[:SEEN_LINES], job_links=0)
        return seen
    jobs, _ = generic.extract(html, page.url)
    soup = BeautifulSoup(html, "html.parser")
    frames = [src for f in soup.find_all("iframe") if (src := (f.get("src") or f.get("data-src") or "").strip())]
    scripts = [s["src"] for s in soup.find_all("script", src=True)]
    seen.update(
        job_links=len(jobs), job_examples=[j.title for j in jobs[:5]],
        frames=[u for f in frames if (u := urljoin(page.url, f)) and JOB_FRAME.search(urlparse(u).netloc + urlparse(u).path)
                and not NOT_A_JOB_FRAME.search(u)][:3],
        js_app=bool(JS_APP.search(html[:300000])),
        widgets=list(dict.fromkeys(s[0] for src in scripts for s in careers.find_systems(src)))[:3],
        deeper=_deeper(soup, page.url),
        title=clean(" ".join(careers.TITLE.findall(html[:20000])), 150),
    )
    problem = careers.career_page_problem(page, None)
    seen["problem"] = problem if problem and "leads back" not in problem else None
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    lines = [line for line in (clean(x, 200) for x in soup.get_text("\n").splitlines()) if len(line) >= 3]
    seen["words"] = sum(len(line.split()) for line in lines)
    seen["lines"] = lines[:SEEN_LINES]
    if m := NO_JOBS_TEXT.search("\n".join(lines)):
        seen["no_jobs_text"] = clean(m[0], 160)
    return seen


def look_at(c: dict) -> dict | None:
    """Open the company's careers page the way the daily read does (read_without_ai) and look at it."""
    url = c["career_url"] or c["feed_url"]
    return look(url, fetch_polite(url)) if url else None


# ---- the reason -----------------------------------------------------------------------

def is_zero(c: dict) -> bool:
    """Shows 0 jobs for you: a Watched / Initiativ company without a Werkstudent / part-time job in the area."""
    return c["status"] in ("watched", "initiativ") and not c.get("student_jobs")


def classify(c: dict, seen: dict | None) -> tuple[str, str]:
    """(reason, one line for the Companies page)."""
    total, near, student = c.get("jobs_total") or 0, c.get("jobs_near") or 0, c.get("student_jobs") or 0
    if not c["career_url"] and not c["feed_url"]:
        return "broken", "No working careers page known"
    if not c["jobs_read_at"]:
        return "unread", "Has a careers page, its jobs were not read yet"
    if total and not near:
        where = re.search(r"outside the area \(([^)]*)\)", c.get("good_note") or "")
        return "not_near", f"{total} job{'s' * (total > 1)}, 0 near you" + (f" (they are in {where[1]})" if where else "")
    if near and not student:
        it = f", {c['it_jobs']} of them IT" if c.get("it_jobs") else ""
        return "not_student", f"{near} job{'s' * (near > 1)} near you{it}, none for students (Werkstudent / part-time)"
    method, note = c.get("jobs_method") or "", c.get("jobs_note") or ""
    if method.endswith(" feed") and not note:  # an applicant system's own list: it knows best
        return "no_jobs", f"The {method[:-5]} job list is empty right now"
    if not seen:  # read, but the page wasn't looked at: go by the note of the last read
        if BROKEN_NOTE.search(note):
            return "broken", note
        if "JavaScript" in note:
            return "hidden", note
        return "no_jobs", "The careers page lists no job ads"
    if not seen["ok"]:
        return "broken", seen.get("problem") or "The careers page did not load"
    if seen.get("script_redirect"):
        return "broken", f"The careers address only forwards to {seen['script_redirect']} (the read stays on the empty first page)"
    path = urlparse(seen["final_url"]).path.strip("/")
    if not path and urlparse(seen["url"]).path.strip("/"):
        return "broken", "The careers address now leads to the homepage"
    if seen.get("frames"):
        return "hidden", f"The jobs are in an embedded box from {urlparse(seen['frames'][0]).hostname}"
    if seen.get("widgets"):
        return "hidden", f"The page loads its jobs from {seen['widgets'][0]} with JavaScript"
    if (d := seen.get("deeper")) and not seen.get("job_links"):
        return "landing", f"jobs probably behind '{d['text'][:60]}'" + (f" ({d['system']})" if d.get("system") else "")
    words = seen.get("words", 0)
    if not seen.get("data") and (words < NO_WORDS or (words < FEW_WORDS and seen.get("js_app"))):
        return "hidden", f"The page builds its text with JavaScript (only {words} words in it)"
    if seen.get("no_jobs_text"):
        return "no_jobs", f"The page says: \"{seen['no_jobs_text']}\""
    if seen.get("problem") and "no job words" in seen["problem"]:
        return "broken", "The careers page no longer talks about jobs"
    return "no_jobs", "The careers page loaded and lists no job ads"


def _other_sources(rows: list[dict]) -> dict[int, list[str]]:
    """{company id: ["Arbeitsagentur: 3 IT job ads in your area", ...]} from the other sources. Only fresh ones:
    the Arbeitsagentur IT ads (collected again every Sunday), LinkedIn (last 30 days), your alerts and links (last
    30 days). Not the ads of any kind a town's collection found once ("region:Darmstadt:aa"): those can be old."""
    from .companies import clean_name  # companies imports company_jobs, which imports this module
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        recent = conn.execute("""SELECT company, source, location FROM jobs WHERE first_seen >= ? AND company != ''
                                 AND source NOT LIKE '% (Companies)' AND duplicate_of IS NULL""", (since,)).fetchall()
    alerts: Counter = Counter()
    for r in recent:
        if r["location"] and NEARBY.search(r["location"]):
            alerts[company_key(clean_name(r["company"]))] += 1
    by_prefix: dict[str, list[str]] = {}
    for k in alerts:
        by_prefix.setdefault(k[:5], []).append(k)
    found = {}
    for c in rows:
        src = c["sources"] if isinstance(c["sources"], dict) else json.loads(c["sources"] or "{}")
        aa = sum(n for k, n in src.items() if k == "arbeitsagentur" or k.startswith("ba_"))
        li = sum(n for k, n in src.items() if k.startswith("linkedin:"))
        mail = sum(alerts[k] for k in by_prefix.get(c["name_key"][:5], []) if _same(k, c["name_key"]))
        parts = ([f"Arbeitsagentur: {aa} IT job ad{'s' * (aa > 1)} in your area (Sunday's collection)"] if aa else []) \
            + ([f"LinkedIn: {li} job{'s' * (li > 1)} within 40 km"] if li else []) \
            + ([f"your job alerts / links: {mail} job{'s' * (mail > 1)} in your area (last {RECENT_DAYS} days)"] if mail else [])
        if parts:
            found[c["id"]] = parts
    return found


def _had_jobs() -> dict[str, str]:
    """{company name: newest job its careers page ever gave} from the job history."""
    with db.connect() as conn:
        rows = conn.execute("SELECT source, MAX(first_seen) FROM jobs WHERE source LIKE '% (Companies)' GROUP BY source")
        return {r[0][:-len(" (Companies)")]: r[1] for r in rows}


def diagnose(rows: list[dict], seen_by_id: dict[int, dict | None], keep_seen: bool = True) -> Counter:
    """Save the diagnosis of these companies (fresh rows). Companies that now have jobs for you lose theirs.
    keep_seen: a company without a new look keeps what the tool saw last time."""
    others, history = _other_sources(rows), _had_jobs()
    reasons = Counter()
    with db.connect() as conn:
        old = {r["company_id"]: dict(r) for r in conn.execute("SELECT * FROM diagnosis")}
        for c in rows:
            if not is_zero(c):
                conn.execute("DELETE FROM diagnosis WHERE company_id = ?", (c["id"],))
                continue
            seen = seen_by_id.get(c["id"])
            if seen is None and keep_seen and (o := old.get(c["id"])) and o["seen"]:
                seen = json.loads(o["seen"])
            reason, detail = classify(c, seen)
            reasons[reason] += 1
            suspicious = None
            if not c.get("jobs_near") and (proof := others.get(c["id"])):
                suspicious = "Suspicious 0: " + "; ".join(proof)
            if seen and seen.get("job_links") and not c.get("jobs_total"):
                suspicious = (suspicious + ". " if suspicious else "Suspicious 0: ") + \
                    f"the page has {seen['job_links']} link{'s' * (seen['job_links'] > 1)} that look like job ads, but 0 were saved"
            changed = None
            before = c.get("jobs_seen_at") or history.get(c["name"])
            if not c.get("jobs_total") and before and c["jobs_read_at"]:
                changed = f"Page probably changed: it had jobs before (last on {before[:10]}), now 0"
            conn.execute("""INSERT INTO diagnosis (company_id, reason, detail, suspicious, changed, seen, at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(company_id) DO UPDATE SET reason = excluded.reason, detail = excluded.detail,
                              suspicious = excluded.suspicious, changed = excluded.changed, seen = excluded.seen,
                              at = excluded.at""",
                         (c["id"], reason, detail, suspicious, changed,
                          json.dumps(seen, ensure_ascii=False) if seen else None, db.now()))
    return reasons


def after_read(ids: set[int], seen_by_id: dict[int, dict | None]) -> None:
    """After a company read: diagnose the companies just read (with what the read saw on their page)."""
    if not ids:
        return
    try:
        diagnose([c for c in db.company_rows() if c["id"] in ids], seen_by_id)
    except Exception as e:  # never stops the read
        log.warning("Diagnosis after the company read failed (%s)", e)


def one(company_id: int) -> dict | None:
    """Look at one company's page now and diagnose it. None: no such company."""
    c = next((c for c in db.company_rows() if c["id"] == company_id), None)
    if not c:
        return None
    seen = look_at(c) if c["jobs_read_at"] and not (c.get("student_jobs")) else None
    diagnose([c], {c["id"]: seen}, keep_seen=False)
    return get(company_id) or {"company_id": company_id, "reason": None, "detail": "Has jobs for you: nothing to diagnose"}


def run(look_again: bool = True) -> Counter:
    """Diagnose every 0-job company (and the ones you marked wrong). look_again: open each careers page
    that gave 0 jobs (polite: at most one request per second per site)."""
    rows = [c for c in db.company_rows() if is_zero(c)]
    wrong = {r["company_id"] for r in all_rows() if r["wrong_at"]}
    rows += [c for c in db.company_rows() if c["id"] in wrong and not is_zero(c)]
    todo = [c for c in rows if look_again and c["jobs_read_at"] and not c.get("jobs_total") and (c["career_url"] or c["feed_url"])
            and not (c["jobs_method"] or "").endswith(" feed")]
    _progress.update(running=True, done=0, total=len(todo), finished_at=None, error=None)
    seen: dict[int, dict | None] = {}

    def check(c: dict) -> None:
        try:
            seen[c["id"]] = look_at(c)
        except Exception as e:
            log.info("Diagnosis: looking at %s failed (%s)", c["name"], e)
        _progress["done"] += 1

    with ThreadPoolExecutor(CONFIG["companies"]["workers"]) as pool:
        list(pool.map(check, todo))
    return diagnose(rows, seen)


def start(look_again: bool = True) -> bool:
    """run() in the background. False if it is running already."""
    if not _lock.acquire(blocking=False):
        return False

    def work():
        try:
            reasons = run(look_again)
            log.info("Diagnosis: %s", ", ".join(f"{REASONS[k]} {n}" for k, n in reasons.most_common()))
        except Exception as e:
            log.exception("Diagnosis failed")
            _progress["error"] = f"{type(e).__name__}: {e}"
        finally:
            _progress.update(running=False, finished_at=db.now())
            _lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def progress() -> dict:
    return dict(_progress)


# ---- reading it back, "This is wrong" ----------------------------------------------------

def all_rows() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM diagnosis")]


def get(company_id: int) -> dict | None:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM diagnosis WHERE company_id = ?", (company_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    r["seen"] = json.loads(r["seen"]) if r["seen"] else None
    return r


def by_company() -> dict[int, dict]:
    """For the Companies page: everything but what the tool saw (that is loaded when you click)."""
    return {r["company_id"]: {k: r[k] for k in ("reason", "detail", "suspicious", "changed", "at", "wrong_at")}
            | {"has_seen": bool(r["seen"])} for r in all_rows()}


def mark_wrong(company_id: int, wrong: bool) -> bool:
    """Put the company on (or take it off) the re-check list. False: it has no diagnosis."""
    with db.connect() as conn:
        cur = conn.execute("UPDATE diagnosis SET wrong_at = ? WHERE company_id = ?", (db.now() if wrong else None, company_id))
        return cur.rowcount > 0
