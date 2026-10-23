"""Company Discovery Engine, Phase B: read each company's jobs, rank the companies, act on them.

1. Jobs: the applicant system's feed if there is one, else JobPosting structured data on the
   page, else the AI reads the job list from the page text (only the job-related lines, to save
   AI requests). Only jobs in the Darmstadt / Frankfurt area (or remote, or without a place) are
   saved; they go through the normal flow: keyword filter, duplicates, AI score, Telegram.
   Also the first time a company is read: you never saw its jobs, so a good one is new to you
   (only jobs that pass the filter and the AI score are sent, so this is no flood).
   Watched and Initiativ companies are read at 13:00 (busy ones, Monday to Friday) and 19:00 (busy ones every
   day, quiet ones every third day), see schedule.py; the AI only reads a page again when its job part changed.
2. Fit score (0-100): small / mid-size companies, distance, share of IT jobs, Werkstudent jobs
   now or before (strong bonus), fresh jobs. Big companies get fewer points but stay visible.
3. The best companies with good jobs become watched links (max 10 per run), marked "auto-added".
4. Companies with IT jobs but no Werkstudent job go to the Initiativ list, with a one-line AI
   reason why an Initiativbewerbung could fit you.
"""
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from . import ai, db, telegram, websearch
from .clock import GERMANY, today
from .config import CONFIG
from .dedup import dedup_key
from .discover import NEARBY, _belongs_to, _is_known, _same, _watched_keys, _with_search, board_url, company_key
from .extractors import Job, ats, clean, generic
from .fetcher import fetch_polite
from .filters import get_filter, hours_of
from .sources import check_url

log = logging.getLogger("jobradar")

FRESH_DAYS = 14         # "has fresh jobs" for the fit score
MAX_PART = 4000         # characters of a page sent to the AI
# A real Werkstudent job. Stricter than the keyword filter, where "Teilzeit" also counts:
# "Voll-/Teilzeit" senior jobs must not earn the Werkstudent bonus.
WERKSTUDENT = re.compile(r"werk[\s-]*student|working student|studentische[nr]? (?:hilfskraft|mitarbeit|aushilfe)|"
                         r"student assistant|studentenjob|student job|minijob f[üu]r studierende", re.I)
MAX_JOBS = 150          # per company (Workday / SmartRecruiters return the newest 100 anyway)


# ---- 1. reading the jobs -------------------------------------------------------------

EXTRACT_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"title": {"type": "STRING"}, "link": {"type": "INTEGER"},
                             "location": {"type": "STRING"}, "employment": {"type": "STRING"}},
              "required": ["title", "link", "location", "employment"]},
}
EXTRACT_PROMPT = """Below are the numbered links and the job-related text lines of the careers page of {company}.
List the open job postings on this page.

Rules:
- Only real job postings listed here. Not menu items, "all jobs" buttons, benefits, events or blog posts,
  and not career areas or target groups ("Students", "Production", "IT & Digital").
- title: the job title exactly as written.
- link: the number of the link that opens this job ([3] -> 3), or 0 if there is none. Never make up links.
- location and employment (e.g. Vollzeit, Teilzeit, Werkstudent): only if written next to the job, else "".
- If the page lists no job postings, return [].

{part}"""


def _is_data(html: str) -> bool:
    """A JSON or XML job list (e.g. d.vinci, rexx), not a web page."""
    start = html.lstrip()[:5]
    return start[:1] in "[{" or start.lower() == "<?xml"


def relevant_part(html: str, page_url: str) -> tuple[str, list[str]]:
    """The job-related links and lines of a page, numbered for the AI. ("", []) if nothing
    on the page looks like a job, then the AI isn't asked at all."""
    if _is_data(html):  # send the data as it is
        return (html[:MAX_PART] if generic.JOB_WORD.search(html) else ""), []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        tag.decompose()
    links: list[str] = []
    labels: list[str] = []
    for a in soup.find_all("a", href=True):
        label, url = clean(a.get_text(" "), 160), generic._resolve(a, page_url)
        if (url and url not in links and len(label) >= 6 and label.lower() not in generic.NAV_TEXT
                and (generic.GENDER.search(label) or generic.JOB_WORD.search(label))):
            links.append(url)
            labels.append(label)
    lines = [clean(line, 200) for line in soup.get_text("\n").splitlines()]
    lines = [line for line in lines if 3 <= len(line) <= 200]
    keep = set()
    for i, line in enumerate(lines):
        if generic.GENDER.search(line) or generic.JOB_WORD.search(line):
            keep |= {i - 1, i, i + 1, i + 2}  # the line around a title often has place / hours
    text = [lines[i] for i in sorted(keep) if 0 <= i < len(lines)]
    if not links and not text:
        return "", []
    part = "LINKS\n" + "\n".join(f"[{n}] {label}" for n, label in enumerate(labels, 1))
    part += "\n\nTEXT\n" + "\n".join(dict.fromkeys(text))
    return part[:MAX_PART], links


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


_last_ai = 0.0
_ai_lock = threading.Lock()


def _pace() -> None:
    """The free AI tiers also limit requests and words per MINUTE: keep a gap between ours."""
    global _last_ai
    with _ai_lock:
        wait = _last_ai + CONFIG["companies"]["ai_seconds_between_requests"] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_ai = time.monotonic()


def ai_extract(company: str, part: str, links: list[str], page_url: str) -> list[Job]:
    _pace()
    answer = ai.ask_json(EXTRACT_PROMPT.format(company=company, part=part), EXTRACT_SCHEMA, temperature=0.1, priority="jobs")
    jobs, seen = [], set()
    for a in answer if isinstance(answer, list) else []:
        title = clean(a.get("title"))
        if len(title) < 4 or title.lower() in seen:
            continue
        seen.add(title.lower())
        n = a.get("link")
        # All links say "Bewerben": the one whose address ends with the title is the job's own page (Avelios)
        named = next((u for u in links if u.split("#")[0].rstrip("/").lower().endswith("/" + _slug(title))), None)
        # A job without its own link: the careers page itself, marked with the title (no made-up address)
        url = links[n - 1] if isinstance(n, int) and 1 <= n <= len(links) else named or f"{page_url.split('#')[0]}#job-{_slug(title)}"
        jobs.append(Job(title=title, url=url, company=company, location=clean(a.get("location")),
                        employment=clean(a.get("employment"))))
    return jobs[:MAX_JOBS]


def read_without_ai(c: dict) -> dict:
    """Feed or structured data. If neither works, returns the page part the AI should read."""
    note = ""
    if c["feed_url"] and (board := ats.detect(c["feed_url"])) and board.system in ats.FETCHERS:
        try:
            return {"jobs": ats.fetch_jobs(board, "")[:MAX_JOBS], "method": f"{board.system} feed"}
        except Exception as e:
            note = f"The {board.system} feed could not be read ({type(e).__name__})"
    for url in dict.fromkeys(filter(None, [c["feed_url"], c["career_url"]])):
        page = fetch_polite(url)
        if not page.ok:
            note = note or page.reason
            continue
        jobs = [] if _is_data(page.html) else generic._from_json_ld(BeautifulSoup(page.html, "html.parser"), page.url)
        if jobs:
            return {"jobs": jobs[:MAX_JOBS], "method": "Structured data (JobPosting)"}
        part, links = relevant_part(page.html, page.url)
        if part:
            return {"ai_part": part, "ai_links": links, "page_url": page.url}
        note = note or "The careers page shows no job titles (maybe they load with JavaScript)"
        if found := _read_with_jina(c["name"], page.url):
            return found
    return {"jobs": [], "method": None, "note": note or "No careers page to read"}


MD_LINK = re.compile(r"\[([^\]]{3,200})\]\((https?://[^)\s]+)[^)]*\)")


def _read_with_jina(company: str, url: str) -> dict | None:
    """A job list built with JavaScript, read the way a browser shows it (Jina Reader, free).
    An applicant system in it is read directly (and remembered); otherwise the AI gets its job lines."""
    text = websearch.read_page(url)
    if not text:
        return None
    if (board := ats.detect(text)) and board.system in ats.FETCHERS and _belongs_to(
            board.extra.get("site", board.key) if board.system == "Oracle" else board.key, company):
        try:
            return {"jobs": ats.fetch_jobs(board, "")[:MAX_JOBS], "method": f"{board.system} feed",
                    "feed_url": board_url(board)}
        except Exception as e:
            log.info("Companies: the %s list found by Jina Reader for %s could not be read (%s)", board.system, company, e)
    as_link = lambda m: '<a href="%s">%s</a>' % (m[2], m[1])
    html_text = "\n".join("<p>%s</p>" % MD_LINK.sub(as_link, line) for line in text.splitlines())
    part, links = relevant_part(html_text, url)
    return {"ai_part": part, "ai_links": links, "page_url": url} if part else None


# ---- 2. saving them through the normal job flow ----------------------------------------

def _posted_days(job: Job) -> int | None:
    if not job.posted:
        return None
    try:
        day = datetime.fromisoformat(job.posted).date() if "T" in job.posted else date.fromisoformat(job.posted[:10])
    except ValueError:
        return None
    return (today() - day).days


def save_jobs(c: dict, jobs: list[Job]) -> tuple[dict, list[dict]]:
    """Run the jobs through filter + duplicates and save the ones in the area.
    Returns (the company's job numbers, the new jobs)."""
    jf = get_filter()
    stats = Counter(total=len(jobs))
    it_titles, rows = [], []
    elsewhere, filtered = Counter(), Counter()  # for good_note: where the other jobs are, why they failed
    for j in jobs:
        if not j.url or not j.title:
            continue
        if not j.company or (j.company == j.company.lower() and " " not in j.company):  # "360t", a feed's id
            j.company = c["name"]
        days = _posted_days(j)
        stats["fresh"] += days is not None and days <= FRESH_DAYS
        if j.location and not NEARBY.search(j.location):
            elsewhere[clean(j.location, 30)] += 1
            continue
        head = f"{j.title} | {j.employment}"
        is_it = bool(jf.it.find(head)) or len(jf.it.find_all(j.description)) >= jf.min_it_words_in_description
        employer = f"{c['name']} | {c['website'] or ''} | {j.url}"
        verdict = jf.check(j.title, j.employment, j.description, employer)
        hours = hours_of(j.title, j.employment, j.description, employer)
        student = bool(WERKSTUDENT.search(head)) or bool(hours and hours.high <= jf.max_hours)
        stats["near"] += 1
        stats["it"] += is_it
        stats["student"] += student
        stats["good"] += verdict.passed and student  # a Werkstudent / part-time job that passes your filter
        if student and not verdict.passed:
            filtered[verdict.reason] += 1
        if is_it and len(it_titles) < 5:
            it_titles.append(j.title)
        rows.append({**asdict(j), "dedup_key": dedup_key(j.title, j.company), "hours": verdict.hours,
                     "filter_passed": verdict.passed, "filter_reason": verdict.reason})
    source = {"id": None, "name": f"{c['name']} (Companies)", "url": c["career_url"] or ""}
    new = db.insert_new_jobs(source, rows, baseline=False)
    fields = {"jobs_total": stats["total"], "jobs_near": stats["near"], "it_jobs": stats["it"],
              "student_jobs": stats["student"], "good_jobs": stats["good"], "fresh_jobs": stats["fresh"],
              "it_titles": json.dumps(it_titles, ensure_ascii=False),
              "good_note": good_note(stats, elsewhere, filtered)}
    return fields, new


def good_note(stats: Counter, elsewhere: Counter, filtered: Counter) -> str:
    """Why a company's jobs are (not) good, e.g. "1 good · 1 Werkstudent job filtered out
    (Not IT-related) · 3 not Werkstudent / part-time · 4 outside the area (Dortmund, Berlin)"."""
    top = lambda counter: ", ".join(k for k, _ in counter.most_common(2))
    not_student = stats["near"] - stats["student"]
    parts = [f"{stats['good']} good"]
    if filtered:
        parts.append(f"{sum(filtered.values())} Werkstudent job{'s' * (sum(filtered.values()) > 1)} "
                     f"filtered out ({top(filtered)})")
    if not_student:
        parts.append(f"{not_student} not Werkstudent / part-time")
    if elsewhere:
        parts.append(f"{sum(elsewhere.values())} outside the area ({top(elsewhere)})")
    return " · ".join(parts)


# ---- 3. fit score ----------------------------------------------------------------------

def student_history(rows: list[dict]) -> dict[int, int]:
    """{company id: Werkstudent jobs from it in your job history} (any source, also old ones)."""
    from .companies import clean_name  # companies imports this module
    keys = Counter(company_key(clean_name(co)) for co, title in db.employer_titles() if WERKSTUDENT.search(title))
    by_prefix: dict[str, list[str]] = {}
    for k in keys:
        by_prefix.setdefault(k[:5], []).append(k)
    found = {}
    for c in rows:
        n = keys.get(c["name_key"], 0) + sum(keys[k] for k in by_prefix.get(c["name_key"][:5], [])
                                             if k != c["name_key"] and _same(k, c["name_key"]))
        if n:
            found[c["id"]] = n
    return found


SIZE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"id": {"type": "INTEGER"},
                             "size": {"type": "STRING", "enum": ["big", "mid", "small", "unknown"]}},
              "required": ["id", "size"]},
}
SIZE_PROMPT = """How big is each of these employers (in or near Darmstadt / Frankfurt am Main, Germany)?
- big: more than about 1,000 employees, or a well-known national / international brand
- mid: about 50 to 1,000 employees
- small: fewer than about 50 employees
- unknown: you don't know this company. Don't guess.
Answer for each company by its number.

{companies}
"""


def data_size(c: dict) -> str | None:
    """Size from our own data, no AI: staffing agency, or big / mid from the number of job ads
    (Arbeitsagentur IT ads, jobs on its careers page). None = unknown. Never "small": a famous
    name can have few ads."""
    if c["size_hint"]:
        return c["size_hint"]
    jobs = c["jobs_total"] or 0
    return "big" if jobs >= 40 else "mid" if jobs >= 10 else None


def ai_sizes(rows: list[dict], limit: int) -> int:
    """Ask the AI how big companies are (40 per request), only those our data can't size.
    Companies with a careers page first."""
    todo = sorted((c for c in rows if not c["size_ai"] and not data_size(c)),
                  key=lambda c: (not c["career_url"], -sum(json.loads(c["sources"] or "{}").values())))[:limit]
    found = 0
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        lines = "\n".join(f"{n}. {c['name']}" + (f" ({c['website']})" if c["website"] else "")
                          for n, c in enumerate(batch, 1))
        _pace()
        try:
            answer = ai.ask_json(SIZE_PROMPT.format(companies=lines), SIZE_SCHEMA, temperature=0.1, priority="low")
        except (ai.RateLimited, ai.Overloaded):
            break
        for a in answer if isinstance(answer, list) else []:
            n = a.get("id")
            if isinstance(n, int) and 1 <= n <= len(batch) and a.get("size") in ("big", "mid", "small", "unknown"):
                db.update_company(batch[n - 1]["id"], {"size_ai": a["size"]})
                found += 1
    return found


def size_of(c: dict) -> str | None:
    """staffing agency / big / mid / small, or None if unknown. Our data first, then the AI."""
    return data_size(c) or (c["size_ai"] if c["size_ai"] in ("big", "mid", "small") else None)


def fit(c: dict) -> tuple[int, str]:
    score, why = 0, []
    size = size_of(c)
    if size == "staffing agency":
        score -= 20
        why.append("staffing agency")
    elif size == "big":
        why.append("big company")
    elif size == "mid":
        score += 15
        why.append("mid-size")
    elif size == "small":
        score += 20
        why.append("small company")
    else:
        score += 10
        why.append("size unknown")
    d = c["distance_km"]
    if d is None:
        score += 5
    else:
        score += 20 if d <= 5 else 15 if d <= 15 else 8 if d <= 30 else 0
        why.append(f"{round(d)} km away")
    if c["jobs_near"]:
        share = (c["it_jobs"] or 0) / c["jobs_near"]
        score += round(20 * share)
        why.append(f"{share:.0%} IT jobs")
    if c["student_jobs"]:
        score += 30
        why.append("Werkstudent jobs now")
    elif c["student_history"]:
        score += 30
        why.append("posted Werkstudent jobs before")
    if c["fresh_jobs"]:
        score += 10
        why.append("fresh jobs")
    if c.get("category") in CONFIG["companies"]["fit_bonus_categories"]:
        score += CONFIG["companies"]["fit_category_bonus"]
        why.append({"insurance": "insurance", "finance": "bank / finance", "cloud": "cloud / data center",
                    "security": "cybersecurity"}.get(c["category"], c["category"]))
    return max(0, min(100, score)), " · ".join(why)


# ---- 4. auto-adding watched links --------------------------------------------------------

def link_urls(c: dict) -> list[str]:
    """Addresses to watch, best first: the applicant system's job list (searching for Werkstudent
    on big Workday / SmartRecruiters boards, then without), else the page."""
    board = ats.detect(c["feed_url"] or "")
    if board and board.system in ats.FETCHERS and (url := board_url(board)):
        return list(dict.fromkeys([_with_search(url), url]))
    return [u for u in (c["feed_url"] or c["career_url"],) if u]


def auto_add(rows: list[dict], limit: int, progress: dict) -> int:
    watched = _watched_keys()
    # Also town companies the engine marked Watched (careers page read every day) that aren't a watched link yet
    candidates = sorted((c for c in rows if c["good_jobs"] and (c["status"] == "new" or c["status"] == "watched"
                                                                and c["town"] and not c["link_id"])),
                        key=lambda c: (-(c["fit_score"] or 0), -c["good_jobs"]))
    added = 0
    for c in candidates:
        if added >= limit:
            break
        urls = link_urls(c)
        if not urls:
            continue
        if _is_known(c["name_key"], watched) or any(db.find_link_by_url(u) for u in urls):
            db.update_company(c["id"], {"status": "watched"})
            continue
        progress["step"] = f"Testing {c['name']} as a watched link…"
        for url in urls:  # only add a link the normal hourly check can read
            result = check_url(url, browser=False)
            if result.status == "green":
                break
        if result.status != "green":
            db.update_company(c["id"], {"jobs_note": f"Not auto-added: the hourly link check can't read "
                                                     f"this page ({result.reason})"})
            continue
        link_id = db.add_link(url, name=c["name"])
        db.mark_auto_added(link_id)
        db.update_company(c["id"], {"status": "watched", "link_id": link_id})
        log.info("Companies: auto-added %s as a watched link (%s)", c["name"], url)
        added += 1
    return added


# ---- 5. the Initiativ list ---------------------------------------------------------------

REASON_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {"id": {"type": "INTEGER"}, "reason": {"type": "STRING"}},
              "required": ["id", "reason"]},
}
REASON_PROMPT = """THE PERSON
{profile}

These companies near Darmstadt / Frankfurt have IT jobs open, but no Werkstudent job right now,
so the person could send an unsolicited application (Initiativbewerbung).
For each company (by its number) write ONE short English sentence (max 20 words) on why it could
fit the person, based on its IT jobs. Concrete, no flattery, no made-up facts.

{companies}
"""


INITIATIV_TOP = 10  # only the best-fitting Initiativ companies get a reason (saves AI requests)


def initiativ_reasons(rows: list[dict], limit: int) -> int:
    # Also companies you put on the list yourself, as long as their IT jobs are known
    top = sorted((c for c in rows if c["status"] == "initiativ" and json.loads(c["it_titles"] or "[]")),
                 key=lambda c: -(c["fit_score"] or 0))[:INITIATIV_TOP]
    todo = [c for c in top if not c["initiativ_reason"]][:limit]
    written = 0
    for i in range(0, len(todo), 20):
        batch = todo[i:i + 20]
        lines = "\n".join(
            f"{n}. {c['name']} ({c['city'] or 'near Frankfurt'}"
            + (f", {round(c['distance_km'])} km" if c["distance_km"] is not None else "")
            + f"): IT jobs: {'; '.join(json.loads(c['it_titles'] or '[]')[:4])}" for n, c in enumerate(batch, 1))
        _pace()
        try:
            answer = ai.ask_json(REASON_PROMPT.format(profile=ai.settings()["profile"].strip(), companies=lines),
                                 REASON_SCHEMA, temperature=0.3, priority="low")
        except (ai.RateLimited, ai.Overloaded):
            break
        for a in answer if isinstance(answer, list) else []:
            n = a.get("id")
            if isinstance(n, int) and 1 <= n <= len(batch) and a.get("reason"):
                db.update_company(batch[n - 1]["id"], {"initiativ_reason": clean(a["reason"], 200)})
                written += 1
    return written


# ---- the Phase B part of a run -----------------------------------------------------------

NEARBY_RULE = "2"  # bump when NEARBY counts more places: companies with jobs "outside the area" are read again once


def reread_after_area_change() -> int:
    """One-time exception to the once-a-week rule: jobs outside the old area (e.g. "Bundesweit")
    were never saved, so those companies are read again in this run."""
    if db.get_setting("nearby_rule") == NEARBY_RULE:
        return 0
    todo = [c for c in db.company_rows() if c["jobs_read_at"] and (c["jobs_total"] or 0) > (c["jobs_near"] or 0)]
    for c in todo:  # read again like the first time: old jobs found now don't go to Telegram
        db.update_company(c["id"], {"jobs_read_at": None})
    db.set_setting("nearby_rule", NEARBY_RULE)
    log.info("Companies: %d companies had jobs outside the area: their jobs are read again once", len(todo))
    return len(todo)


def companies_to_read(limit: int, ids: set[int] | None = None) -> list[dict]:
    """Companies with a careers page whose jobs weren't read in the last week; never read first, then the
    ones waiting for the AI (no AI was left for them, see ai_waiting), then the oldest.
    ids: only these (an imported list, look-alikes)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(7, CONFIG["companies"]["recheck_after_days"]))  # never more often
    waiting = ai_waiting()
    rows = [c for c in db.company_rows() if (c["career_url"] or c["id"] in waiting) and c["status"] != "skipped"
            and (ids is None or c["id"] in ids)
            and (not c["jobs_read_at"] or c["id"] in waiting or datetime.fromisoformat(c["jobs_read_at"]) < cutoff)]
    return sorted(rows, key=lambda c: (bool(c["jobs_read_at"]), c["id"] not in waiting, c["jobs_read_at"] or "",
                                       -(c["fit_score"] or 0)))[:limit]


def ai_waiting() -> set[int]:
    """Companies whose job list changed but no AI was left to read it: the next run reads them first
    (auto-resume starts one for them, see auto_resume.work)."""
    return set(json.loads(db.get_setting("ai_waiting") or "[]"))


def ai_pages_left() -> int:
    """AI pages for the 19:00 read and auto-resume: what the free AIs really have left today, minus the
    reserve for scoring jobs (ai_budget: keep_percent), at most max_ai_pages_watched."""
    if not ai.enabled():
        return 0
    return min(CONFIG["companies"]["max_ai_pages_watched"], ai.spare_requests("jobs"))


def skipped_note(n: int) -> str:
    return f"{n} companies skipped, no AI left, will read in the next run"


def _part_hash(part: str) -> str:
    return hashlib.sha1(part.encode()).hexdigest()


def read_jobs(todo: list[dict], progress: dict, stop=lambda: False, ai_pages: int | None = None) -> int:
    """Read and save the jobs of these companies: feeds and structured data without the AI; the AI
    only for a page whose job part changed since it last read it (ai_pages at most, None = max_ai_pages_per_run).
    A changed page the AI didn't get to is never skipped silently: counted (progress "ai_skipped"), shown
    as a warning and kept in ai_waiting for the next run.
    stop(): True = don't start more (the morning). Returns how many new jobs passed your filter."""
    cfg = CONFIG["companies"]
    progress.update(step="Reading job lists…", done=0, total=len(todo))
    results: dict[int, dict] = {}

    def read(c: dict) -> None:
        if stop():  # morning: read next night
            progress["done"] += 1
            return
        try:
            results[c["id"]] = read_without_ai(c)
        except Exception as e:
            log.warning("Companies: reading jobs of %s failed (%s)", c["name"], e)
            results[c["id"]] = {"jobs": [], "method": None, "note": f"Reading failed: {e}"[:300]}
        progress["done"] += 1

    with ThreadPoolExecutor(cfg["workers"]) as pool:
        list(pool.map(read, todo))

    ai_budget = (cfg["max_ai_pages_per_run"] if ai_pages is None else ai_pages) if ai.enabled() else 0
    new_good, waiting, done_ids = 0, set(), set()  # waiting for the AI now / read now
    progress.update(step="Saving jobs…", done=0, ai_used=0, ai_skipped=0)
    for c in todo:
        r = results.get(c["id"])
        if r is None:
            continue  # not read before the morning deadline
        part_hash = None
        if "ai_part" in r:
            part_hash = _part_hash(r["ai_part"])
            if part_hash == c["jobs_part_hash"]:  # the same job list as last time: nothing new, no AI request
                db.save_company_check(c["id"], {"jobs_note": None}, stamp="jobs_read_at")
                done_ids.add(c["id"])
                progress["jobs_read"] += 1
                progress["done"] += 1
                continue
            if ai_budget <= 0:  # read in the next run, when there are AI requests left
                waiting.add(c["id"])
                progress["ai_skipped"] += 1
                continue
            ai_budget -= 1
            progress["ai_used"] += 1
            progress["step"] = f"The AI reads the job list of {c['name']}…"
            try:
                r = {"jobs": ai_extract(c["name"], r["ai_part"], r["ai_links"], r["page_url"]), "method": "AI from page text"}
            except (ai.RateLimited, ai.Overloaded) as e:
                ai_budget = 0
                progress["warnings"].append(ai.stop_note(e, "some job lists will be read next run"))
                waiting.add(c["id"])
                progress["ai_skipped"] += 1
                continue
            except Exception as e:  # every AI that was tried failed on this page: tried again next run
                log.warning("Companies: the AI could not read the job list of %s (%s)", c["name"], str(e)[:160])
                waiting.add(c["id"])
                continue
        fields, new = save_jobs(c, r["jobs"])
        if r.get("feed_url"):  # an applicant system Jina Reader found: read directly next time
            fields["feed_url"] = r["feed_url"]
        db.save_company_check(c["id"], {**fields, "jobs_method": r.get("method"), "jobs_note": r.get("note"),
                                        "jobs_part_hash": part_hash}, stamp="jobs_read_at")
        new_good += sum(1 for j in new if j["filter_passed"])
        done_ids.add(c["id"])
        progress["jobs_read"] += 1
        progress["new_jobs"] += len(new)
        progress["done"] += 1
    # Merged with what is saved now (another run may have changed it meanwhile)
    db.set_setting("ai_waiting", json.dumps(sorted((ai_waiting() - done_ids) | waiting)))
    if progress["ai_skipped"]:
        progress["warnings"].append(skipped_note(progress["ai_skipped"]))
    return new_good


def _score_and_send() -> None:
    """Scored first, then sent to Telegram (like jobs from watched links)."""
    if ai.enabled():
        ai.score_in_background()
    else:
        telegram.notify_in_background()


def run(progress: dict, ids: set[int] | None = None, ai_pages: int | None = None) -> None:
    """ai_pages: AI pages for this run (auto-resume: ai_pages_left()), None = max_ai_pages_per_run."""
    from .companies import too_late  # companies imports this module
    cfg = CONFIG["companies"]
    todo = companies_to_read(cfg["max_job_reads_per_run"], ids)
    new_good = read_jobs(todo, progress, stop=lambda: too_late(progress), ai_pages=ai_pages)
    log.info("Companies: read %d job lists, %d new jobs (%d match your filter)%s",
             progress["jobs_read"], progress["new_jobs"], new_good,
             f"; {skipped_note(progress['ai_skipped'])}" if progress["ai_skipped"] else "")

    mine = lambda rows: [c for c in rows if ids is None or c["id"] in ids]  # an import: AI only for its companies
    if ai.enabled():
        progress["step"] = "Asking the AI how big the companies are…"
        ai_sizes(mine(db.company_rows()), cfg["max_size_lookups_per_run"])
    rank(progress)
    progress["auto_added"] = auto_add(db.company_rows(), cfg["max_auto_added_per_run"], progress)
    if ai.enabled():
        progress["step"] = "Writing Initiativ reasons…"
        initiativ_reasons(mine(db.company_rows()), cfg["max_initiativ_reasons_per_run"])
    if new_good:
        _score_and_send()


def read_unread(progress: dict) -> None:
    """Once, started by hand: every company with a careers page whose jobs were never read, now.
    Feeds and structured data need no AI; the AI reads max_ai_pages_per_run pages, the others
    stay unread and are read first in the next night runs (the AI's daily reserve stays safe)."""
    todo = [c for c in companies_to_read(1_000_000) if not c["jobs_read_at"]]
    new_good = read_jobs(todo, progress)
    ids = {c["id"] for c in todo}
    waiting = sum(1 for c in db.company_rows() if c["id"] in ids and not c["jobs_read_at"])
    log.info("Companies: read %d of %d never-read job lists, %d new jobs (%d match your filter), %d wait for the AI",
             progress["jobs_read"], len(todo), progress["new_jobs"], new_good, waiting)
    progress["notes"].append(f"{progress['jobs_read']} of {len(todo)} never-read job lists read; "
                             f"{waiting} need the AI and are read in the next night runs")
    rank(progress)
    if new_good:
        _score_and_send()


def rank(progress: dict) -> None:
    """Fit score for every company, and the Initiativ list."""
    progress["step"] = "Ranking companies…"
    rows = db.company_rows()
    history = student_history(rows)
    for c in rows:
        c["student_history"] = history.get(c["id"], 0)
        c["fit_score"], c["fit_reasons"] = fit(c)
        fields = {"student_history": c["student_history"], "fit_score": c["fit_score"], "fit_reasons": c["fit_reasons"]}
        # Initiativ: IT jobs, but no Werkstudent job now or before. Back out of it once it posts one
        # (only with a careers page: one without, from the Impressum rule, has nothing to watch).
        if c["status_by"] == "engine":
            if c["status"] == "new" and c["it_jobs"] and not c["student_jobs"] and not c["student_history"] \
                    and size_of(c) != "staffing agency":
                fields["status"] = c["status"] = "initiativ"
                fields["initiativ_at"] = db.now()
                progress["initiativ"] += 1
            elif c["status"] == "initiativ" and c["career_url"] and (c["student_jobs"] or c["student_history"]):
                fields["status"] = c["status"] = "new"
        db.update_company(c["id"], fields)


# ---- every day at 19:00: the jobs of every watched and Initiativ company --------------------



def _as_link(c: dict, active: set[int], yours: set[str]) -> bool:
    """A watched link already (yours or auto-added): the normal link check reads it every hour."""
    return c["link_id"] in active or _is_known(c["name_key"], yours)


def _daily(c: dict, active: set[int], yours: set[str]) -> bool:
    return c["status"] in ("watched", "initiativ") and bool(c["career_url"] or c["feed_url"])         and not _as_link(c, active, yours)


def watched_to_read() -> list[dict]:
    """Watched and Initiativ companies with a careers page (an Initiativ company that posts a Werkstudent
    job reaches you the same day). Not the ones that are a watched link already."""
    active, yours = {l["id"] for l in db.list_links() if l["active"]}, _watched_keys()
    return [c for c in db.company_rows() if _daily(c, active, yours)]


def _next_at(after: datetime, hour: int) -> datetime:
    """The first `hour`:00 (German time) at or after `after`."""
    at = after.astimezone(GERMANY).replace(hour=hour, minute=0, second=0, microsecond=0)
    return at if at >= after else at + timedelta(days=1)


def next_reads(rows: list[dict]) -> None:
    """Adds next_read (ISO time, empty = none planned) and next_read_how to each company: when its jobs
    are read next and by what. The night run reads at most max_job_reads_per_run, so that one can be later."""
    cfg = CONFIG["companies"]
    active, yours = {l["id"] for l in db.list_links() if l["active"]}, _watched_keys()
    now, daily_hour, night_hour = datetime.now(GERMANY), cfg.get("watched_read_hour"), cfg.get("daily_run_hour")
    every = timedelta(days=max(7, cfg["recheck_after_days"]))
    for c in rows:
        c["next_read"], c["next_read_how"] = None, ""
        if not (c["career_url"] or c["feed_url"]) or c["status"] == "skipped":
            continue
        if _as_link(c, active, yours):
            c["next_read_how"] = "with your watched link (every hour)"
        elif daily_hour is not None and _daily(c, active, yours):
            c["next_read"], c["next_read_how"] = _next_at(now, daily_hour).isoformat(), f"every day at {daily_hour:02d}:00"
        elif night_hour is not None:
            due = max(now, datetime.fromisoformat(c["jobs_read_at"]) + every) if c["jobs_read_at"] else now
            c["next_read"] = _next_at(due, night_hour).isoformat()
            c["next_read_how"] = f"night run at {night_hour:02d}:00 (once a week{'' if c['jobs_read_at'] else '; first read, in line'})"


reading_watched = threading.Event()  # set while the 19:00 read is going


def last_new_jobs() -> dict[str, str]:
    """{job source ("<company> (Companies)"): when its newest NEW job came, ISO}. New: posted on that day (when
    the ad says so), or found after the company's first read (the first read's jobs may be months old)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT source, first_seen, posted_at FROM jobs WHERE source LIKE '% (Companies)'").fetchall()
    first: dict[str, str] = {}
    for r in rows:
        first[r["source"]] = min(first.get(r["source"], r["first_seen"]), r["first_seen"])
    last: dict[str, str] = {}
    for r in rows:
        when = r["posted_at"][:10] if r["posted_at"] else r["first_seen"] if r["first_seen"] > first[r["source"]] else None
        if when and when > last.get(r["source"], ""):
            last[r["source"]] = when
    return last


def activity(rows: list[dict], now: datetime | None = None) -> dict[int, str]:
    """{company id: "busy" / "between" / "quiet"} for the daily reads.
    busy: a new job in the last busy_days (30), never read yet, or skipped by the AI last time.
    quiet: no new job in the last quiet_days (60). between: the rest (read every evening, not at 13:00)."""
    cfg, now = CONFIG["companies"], now or datetime.now(timezone.utc)
    busy_since = (now - timedelta(days=cfg["busy_days"])).isoformat(timespec="seconds")
    quiet_since = (now - timedelta(days=cfg["quiet_days"])).isoformat(timespec="seconds")
    last, waiting = last_new_jobs(), ai_waiting()
    found = {}
    for c in rows:
        newest = last.get(f"{c['name']} (Companies)", "")
        if not c["jobs_read_at"] or c["id"] in waiting or newest >= busy_since[:len(newest)] and newest:
            found[c["id"]] = "busy"
        else:
            found[c["id"]] = "between" if newest and newest >= quiet_since[:len(newest)] else "quiet"
    return found


def quiet_turn(company_id: int, day: date) -> bool:
    """A quiet company is read at 19:00 every quiet_every_days: each evening about a third of them, in turn."""
    every = CONFIG["companies"]["quiet_every_days"]
    return company_id % every == day.toordinal() % every


def plan_read(kind: str, now: datetime | None = None) -> tuple[list[dict], Counter]:
    """(the companies to read, {busy / between / quiet / quiet_later: how many}).
    kind "midday" (13:00): the busy ones. kind "evening" (19:00): busy and between ones, and the quiet ones
    whose turn it is. Longest ago read first: if the AI runs out, the ones it skipped last time get it first."""
    now = now or datetime.now(timezone.utc)
    rows = watched_to_read()
    kinds, day, todo, counts = activity(rows, now), now.astimezone(GERMANY).date(), [], Counter()
    for c in rows:
        k = kinds[c["id"]]
        counts[k] += 1
        if kind == "midday" and k != "busy" or k == "quiet" and not quiet_turn(c["id"], day):
            counts["later"] += 1
            continue
        todo.append(c)
    return sorted(todo, key=lambda c: c["jobs_read_at"] or ""), counts


READS = {"evening": ("watched_read_last", "19:00 read"), "midday": ("midday_read_last", "13:00 read")}
reading_kind: dict[str, str | None] = {"now": None}  # "midday" / "evening" while one of the reads is going


def read_watched(kind: str = "evening") -> dict:
    """The daily reads of the watched (and Initiativ) companies, so a good new job reaches you the same day:
    kind "evening" (19:00) and "midday" (13:00, Monday to Friday, busy companies only), see plan_read.
    Waits while a Companies run is going (they read the same pages)."""
    from . import companies  # it imports this module
    setting, label = READS[kind]
    for _ in range(180):  # at most 3 hours
        if not companies.progress()["running"]:
            break
        time.sleep(60)
    else:
        log.warning("%s: a Companies run is still going after 3 hours, skipped", label)
        return {}
    started = time.monotonic()
    todo, counts = plan_read(kind)
    progress = {"step": "", "done": 0, "total": 0, "jobs_read": 0, "new_jobs": 0, "warnings": []}
    ai_pages = ai_pages_left()
    reading_watched.set()  # auto-resume doesn't start a run meanwhile
    reading_kind["now"] = kind
    try:
        new_good = read_jobs(todo, progress, ai_pages=ai_pages)
    finally:
        reading_watched.clear()
        reading_kind["now"] = None
    seconds = time.monotonic() - started
    log.info("Watched careers pages (%s): read %d of %d in %.0f s (%d busy, %d between, %d quiet, %d not today), "
             "%d new jobs (%d match your filter), the AI read %d of max %d pages%s",
             label, progress["jobs_read"], len(todo), seconds, counts["busy"], counts["between"], counts["quiet"],
             counts["later"], progress["new_jobs"], new_good, progress["ai_used"], ai_pages,
             "".join(f"; {w}" for w in progress["warnings"]))
    result = {"at": db.now(), "kind": kind, "read": progress["jobs_read"], "companies": len(todo),
              "seconds": round(seconds), "new_jobs": progress["new_jobs"], "new_good": new_good,
              "ai_used": progress["ai_used"], "ai_pages": ai_pages, "ai_skipped": progress["ai_skipped"],
              "not_today": counts["later"], "warnings": progress["warnings"]}
    db.set_setting(setting, json.dumps(result))  # for the Coverage page
    if new_good:
        _score_and_send()
    return {**progress, **result}


def read_midday() -> dict:
    return read_watched("midday")


def last_watched_read(kind: str = "evening") -> dict | None:
    """The last 19:00 / 13:00 read, for the Coverage page. ai_waiting: companies still waiting for the AI now."""
    last = json.loads(db.get_setting(READS[kind][0]) or "null")
    return last and {**last, "ai_waiting": len(ai_waiting())}


FIRST_READ_RULE = "1"  # bump to release the jobs of first reads again


def release_first_reads(days: int = 30) -> int:
    """Once: good jobs from a company's first read were kept from Telegram before v9.7. The recent ones
    you haven't opened or marked yet are released, so the normal Telegram round sends the good ones."""
    if db.get_setting("first_read_rule") == FIRST_READ_RULE:
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        released = conn.execute(
            """UPDATE jobs SET baseline = 0
               WHERE link_id IS NULL AND source LIKE '% (Companies)' AND baseline = 1 AND notified = 0
                 AND filter_passed = 1 AND duplicate_of IS NULL AND seen = 0
                 AND COALESCE(status, '') = '' AND first_seen >= ?""", (since,)).rowcount
    db.set_setting("first_read_rule", FIRST_READ_RULE)
    if released:
        log.info("Companies: %d good jobs from first reads can go to Telegram now", released)
    return released
