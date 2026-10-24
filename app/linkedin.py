"""LinkedIn's public job search (the guest search anyone can open without logging in, like JobSpy):
which companies near Darmstadt hire in IT. Not a job source: only the company name, its LinkedIn page and
the job's place are kept, and the company goes into Companies, so its own careers page gets watched.

Very gentle: once a day (linkedin.hour), 2 keywords a day in turn (each one once a week), at most 5 pages
of 10 jobs per keyword, 5-10 s random pause between pages. A job counts if its place is at most 40 km from Darmstadt
(area.max_km, measured like the 40 km rule for jobs). No login, no cookies, never your account.
If LinkedIn says "too many requests" or blocks (429, 999, a login wall): stop at once, try again tomorrow.
"""
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

from . import db, must_apply, region
from .clock import GERMANY
from .companies import STAFFING
from .config import CONFIG
from .discover import NEAR_PLACE
from .extractors import clean
from .fetcher import HTML_HEADERS

log = logging.getLogger("jobradar")

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PER_PAGE = 10           # LinkedIn's guest search gives 10 jobs per page
LAST_30_DAYS = "r2592000"
NOTE = "found via LinkedIn, hiring"  # under the name of companies LinkedIn found first
BLOCKED = (403, 429, 999)  # 999 is LinkedIn's own "go away"
_lock = threading.Lock()


class Blocked(Exception):
    pass


def source_key(keyword: str) -> str:
    return "linkedin:" + keyword


def _page(client: httpx.Client, keyword: str, page: int) -> list[dict]:
    """The jobs on one page: [{"name", "linkedin_url", "location"}]. Blocked if LinkedIn says no."""
    cfg = CONFIG["linkedin"]
    r = client.get(SEARCH, params={"keywords": keyword, "location": cfg["location"], "distance": cfg["distance_miles"],
                                   "f_TPR": LAST_30_DAYS, "start": page * PER_PAGE})
    # A redirect means the login wall ("authwall"): we never log in, so that is a block too
    if r.status_code in BLOCKED or r.is_redirect or "authwall" in r.text[:3000] or "captcha" in r.text[:3000].lower():
        raise Blocked(f"HTTP {r.status_code}")
    if r.status_code == 400:  # asked past the last result
        return []
    r.raise_for_status()
    jobs = []
    for card in BeautifulSoup(r.text, "html.parser").select("li"):
        sub = card.select_one(".base-search-card__subtitle")
        if not sub:
            continue
        link = sub.find("a")
        place = card.select_one(".job-search-card__location")
        jobs.append({"name": clean(sub.get_text(" "), 120),
                     "linkedin_url": link["href"].split("?")[0] if link and link.get("href", "").startswith("https://") else None,
                     "location": clean(place.get_text(" "), 100) if place else ""})
    return jobs


def km_away(location: str) -> float | None:
    """Real distance of a job's place from your area's center (area.center, the same OpenStreetMap lookup
    as the 40 km rule for jobs); "Frankfurt/Rhein-Main": the nearest part. None: no place found.
    The whole place first: "Sulzbach (Taunus), Hessen" is not the Sulzbach that "Sulzbach" alone finds."""
    whole = re.sub(r"\s*\((.*?)\)", r" \1", location)
    if "/" not in whole and (km := must_apply.km_from_center(whole)) is not None:
        return km
    found = [km for part in must_apply._places(location) if (km := must_apply.km_of_place(part)) is not None]
    return min(found) if found else None


def near(location: str) -> bool:
    """In your area: at most area.max_km (40 km) from Darmstadt. A place the map doesn't know
    ("Rhein-Main-Gebiet") or a failed lookup: the list of places in the area decides."""
    if not location.strip():
        return False
    km = km_away(location)
    return km <= CONFIG["area"]["max_km"] if km is not None else bool(NEAR_PLACE.search(location))


def _search(keyword: str, client: httpx.Client, sleep) -> tuple[list[dict], int]:
    """(the jobs, pages read)."""
    jobs = []
    for page in range(CONFIG["linkedin"]["max_pages"]):
        if page:
            sleep(random.uniform(*CONFIG["linkedin"]["pause_seconds"]))
        found = _page(client, keyword, page)
        jobs += found
        if len(found) < PER_PAGE:  # the last page
            break
    return jobs, page + 1


def keywords_today() -> tuple[int, list[str]]:
    """(index of the first, the keywords): the next ones in turn, so the whole list is searched once a week."""
    cfg = CONFIG["linkedin"]
    words = cfg["keywords"]
    start = int(db.get_setting("linkedin_next", "0") or 0) % len(words)
    return start, [words[(start + n) % len(words)] for n in range(min(cfg["keywords_per_day"], len(words)))]


def _german_day(iso: str | None):
    return datetime.fromisoformat(iso).astimezone(GERMANY).date() if iso else None


def last() -> dict:
    try:
        return json.loads(db.get_setting("linkedin_last", "") or "{}")
    except ValueError:
        return {}


def scheduled() -> None:
    """Once a day (scheduler). A second start on the same day (e.g. after a restart) does nothing."""
    if _german_day(last().get("at")) == datetime.now(GERMANY).date():
        log.info("LinkedIn: already searched today")
        return
    search()


BY_HAND_BEFORE = timedelta(minutes=15)  # no search by hand this long before the 03:00 / 13:00 / 19:00 runs


def why_not_now(now: datetime | None = None) -> str:
    """"" if a search by hand may start now, else why not (blocked today, a fixed run going or about to start)."""
    from . import schedule  # it imports this module
    now = (now or datetime.now(GERMANY)).astimezone(GERMANY)
    if not CONFIG["linkedin"].get("enabled"):
        return "the LinkedIn search is off (config.yaml)"
    if running():
        return "a LinkedIn search is already going"
    if status()["blocked"]:
        return "LinkedIn blocked today's search: try again tomorrow"
    going = schedule.running() & {"night", "midday", "evening", "recheck", "read"}
    if going:
        return f"a fixed run is going ({', '.join(sorted(going))})"
    for run in schedule.runs():
        at = schedule.next_at(run, now)
        if run["key"] != "linkedin" and at - now <= BY_HAND_BEFORE:
            return f"the {run['hour']:02d}:00 {run['key']} run starts at {at:%H:%M}"
    return ""


def start_by_hand() -> str:
    """Starts today's keywords in the background (POST /api/linkedin/run). "" if started, else why not."""
    why = why_not_now()
    if why:
        log.info("LinkedIn search by hand not started: %s", why)
        return why
    log.info("LinkedIn search started by hand")
    threading.Thread(target=search, daemon=True).start()
    return ""


def search(sleep=time.sleep) -> dict:
    """Today's keywords. Returns what happened (also saved for the dashboard)."""
    from .company_import import add_companies  # it imports companies, which this module needs first
    if not _lock.acquire(blocking=False):
        return {"error": "a LinkedIn search is already going"}
    try:
        start, words = keywords_today()
        now_searching.update(done=0, total=len(words), word="")
        result = {"at": db.now(), "keywords": [], "pages": 0, "jobs": 0, "outside": 0, "recruiters": 0,
                  "new": 0, "known": 0, "blocked": False, "error": ""}
        with httpx.Client(headers=HTML_HEADERS, timeout=30, follow_redirects=False) as client:
            for n, word in enumerate(words):
                if n:
                    sleep(random.uniform(*CONFIG["linkedin"]["pause_seconds"]))
                now_searching.update(done=n, word=word)
                try:
                    jobs, pages = _search(word, client, sleep)
                except Blocked as e:
                    result.update(blocked=True, error=str(e))
                    log.warning("LinkedIn blocked the search (%s): stopped, tried again tomorrow", e)
                    break
                except Exception as e:  # LinkedIn down, network: tomorrow again, the same keyword
                    result["error"] = f"{type(e).__name__}: {e}"
                    log.warning("LinkedIn search \"%s\" failed (%s): tried again tomorrow", word, result["error"])
                    break
                db.set_setting("linkedin_next", str((start + n + 1) % len(CONFIG["linkedin"]["keywords"])))
                records = []
                for j in jobs:
                    if not j["name"]:
                        continue
                    if not near(j["location"]):  # 25 miles reach further than 40 km from Darmstadt, or no place given
                        result["outside"] += 1
                    elif STAFFING.search(j["name"]):
                        result["recruiters"] += 1
                    else:
                        records.append({"name": j["name"], "city": j["location"].split(",")[0].strip() or None,
                                        "note": NOTE, "linkedin_url": j["linkedin_url"]})
                added = add_companies(records, source_key(word)) if records else {"new": 0, "known": 0}
                result["keywords"].append(word)
                result["pages"] += pages
                result["jobs"] += len(jobs)
                result["new"] += added["new"]
                result["known"] += added["known"]
                log.info("LinkedIn \"%s\": %d jobs on %d pages, %d near here: %d new companies, %d known",
                         word, len(jobs), pages, len(records), added["new"], added["known"])
        if result["new"]:
            region.assign_towns()  # the new ones in a town count for its coverage
        result["finished_at"] = db.now()
        db.set_setting("linkedin_last", json.dumps(result))
        log.info("LinkedIn: %s", status()["text"])
        return result
    finally:
        _lock.release()


now_searching = {"done": 0, "total": 0, "word": ""}  # the search going now (Timeline page)


def running() -> bool:
    return _lock.locked()


def new_this_week() -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    return sum(1 for c in db.company_rows() if c["origin_note"] == NOTE and (c["first_seen"] or "") >= since)


def status() -> dict:
    """For the Coverage and Companies pages: "LinkedIn: 12 new companies this week, last search ...",
    or that LinkedIn blocked today's search."""
    if not CONFIG["linkedin"].get("enabled"):
        return {"text": "", "blocked": False}
    r, week = last(), new_this_week()
    blocked = bool(r.get("blocked")) and _german_day(r.get("at")) == datetime.now(GERMANY).date()
    return {"text": f"LinkedIn: {week} new {'company' if week == 1 else 'companies'} this week", "week_new": week,
            "hour": CONFIG["linkedin"].get("hour"),
            "at": r.get("at"), "keywords": r.get("keywords") or [], "blocked": blocked,
            "error": "" if blocked else r.get("error") or ""}
