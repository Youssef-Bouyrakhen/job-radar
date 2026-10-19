"""🔥 Must apply: the few jobs worth applying to today. A job goes in only if ALL are true:

1. Werkstudent / working student / studentische Hilfskraft, max 20 h/week if the ad states hours
2. AI score 8 or higher
3. posted in the last 3 days (no posting date: first seen)
4. within 30 km of Darmstadt, or remote / Homeoffice / Bundesweit
5. no blockers: the AI checks "2+ years experience", staffing agency, Master-only in the scoring request
6. at least one low-competition sign: from the company's own careers page / applicant system, a small or
   mid-size company, the company posted Werkstudent jobs before, or its category is Insurance,
   Banks/Finance, Cloud/Data centers/Networks or Cybersecurity

The numbers are in config.yaml (must_apply:). Once in, a job stays until you mark it Applied or hide it.
Telegram: a "🔥 MUST APPLY" message right away, and one reminder after 24 h if you haven't applied or hidden it.
"""
import json
import logging
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from . import db
from .config import CONFIG
from .fetcher import BOT_HEADERS
from .filters import detect_hours

log = logging.getLogger("jobradar")

STUDENT = re.compile(r"werk\s?-?student|working[- ]student|studentische[rn]?\s+(?:hilfskraft|aushilfe|mitarbeit|"
                     r"besch[äa]ftigung)|student(?:ische)?\s+assistant|studentjob|\bhiwi\b", re.I)
REMOTE = re.compile(r"remote|home\s?-?office|mobiles? arbeiten|bundesweit|deutschlandweit|germany[- ]wide|"
                    r"nationwide|[üu]berregional|ortsunabh[äa]ngig|anywhere", re.I)
STAFFING_FIRM = re.compile(r"personal(?:dienst|service|vermittlung|leasing|beratung)|zeitarbeit|staffing|"
                           r"recruit|\b(?:hays|randstad|adecco|manpower|ferchau|gulp|brunel|orizon|amadeus fire|"
                           r"tempton|piening|univativ|studitemps|jobvalley|zenjob|westhouse|computer futures)\b", re.I)
SIGN_LABELS = {"own_site": "on the company's own careers page", "size": "{size} company",
               "history": "posted Werkstudent jobs before", "category": "{category}"}
CATEGORY_LABELS = {"insurance": "insurance", "finance": "bank / finance", "cloud": "cloud / data center",
                   "security": "cybersecurity", "software": "IT services", "other": "other"}


def cfg() -> dict:
    return CONFIG["must_apply"]


# ---- distance --------------------------------------------------------------------------

NOMINATIM = "https://nominatim.openstreetmap.org/search"
_geo_lock = threading.Lock()
_last_geo = 0.0


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = [(math.radians(x), math.radians(y)) for x, y in (a, b)]
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def place_of(location: str) -> tuple[float, float] | None:
    """Coordinates of a job's place ("Frankfurt am Main, Hessen"), from OpenStreetMap (Nominatim).
    Each place is looked up once and remembered; at most 1 request per second (their rule)."""
    name = re.sub(r"\s+", " ", re.split(r"[;|/(]| oder | or ", location)[0]).strip(" ,-")
    name = re.sub(r"^\d{5}\s+", "", name)[:80]
    # "Darmstadt bei Frankfurt am Main", "Eschborn, Hessen, Deutschland": also try the first part alone
    first = re.split(r",| bei | near | - ", name)[0].strip()
    for tried in dict.fromkeys([name, first]):
        if len(tried) >= 2 and (found := _look_up(tried)):
            return found
    return None


def _look_up(name: str) -> tuple[float, float] | None:
    global _last_geo
    known = db.get_place(name)
    if known is not None:
        return known if known[0] is not None else None
    with _geo_lock:
        time.sleep(max(0.0, _last_geo + 1.1 - time.monotonic()))
        _last_geo = time.monotonic()
        try:
            r = httpx.get(NOMINATIM, params={"q": name, "format": "json", "limit": 1, "countrycodes": "de"},
                          headers={"User-Agent": BOT_HEADERS["User-Agent"]}, timeout=20)
            r.raise_for_status()
            hits = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("Must apply: could not look up the place %r (%s)", name, type(e).__name__)
            return None  # not remembered: tried again next time
    found = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
    db.save_place(name, found)
    return found


def km_from_center(location: str) -> float | None:
    center = CONFIG["companies"]["places"].get(cfg()["center"])
    found = place_of(location) if location else None
    return round(_km(found, tuple(center)), 1) if found and center else None


# ---- the six conditions ------------------------------------------------------------------

def _max_hours(job: dict) -> float | None:
    nums = re.findall(r"\d+(?:[.,]\d+)?", job.get("hours") or "")
    if nums:
        return max(float(n.replace(",", ".")) for n in nums)
    found = detect_hours(job.get("description") or "")
    return found.high if found else None


def _age_days(job: dict) -> float:
    when = job["posted_at"] or job["first_seen"]
    if len(when) == 10:  # a day only: that day in Germany
        return (date.today() - date.fromisoformat(when)).days
    return (datetime.now(timezone.utc) - datetime.fromisoformat(when)).total_seconds() / 86400


def _company(job: dict, by_key: dict[str, dict]) -> dict | None:
    from .companies import clean_name
    from .discover import _same, company_key
    key = company_key(clean_name(job.get("company") or ""))
    if len(key) < 2:
        return None
    return by_key.get(key) or next((c for k, c in by_key.items() if k[:5] == key[:5] and _same(k, key)), None)


def check(job: dict, company: dict | None, history: int, site: str) -> tuple[bool, list[str], str]:
    """(all met, the conditions it met as short texts, the first one it failed)."""
    from .company_jobs import size_of
    c, met = cfg(), []
    if not STUDENT.search(f"{job['title']} | {job.get('employment') or ''}"):
        return False, met, "not a Werkstudent job"
    hours = _max_hours(job)
    if hours and hours > c["max_hours"]:
        return False, met, f"more than {c['max_hours']} h/week"
    met.append("Werkstudent" + (f", max {hours:g} h/week" if hours else ""))
    if (job.get("ai_score") or 0) < c["min_score"]:
        return False, met, f"AI score below {c['min_score']}"
    met.append(f"AI score {job['ai_score']}/10")
    age = _age_days(job)
    if age > c["max_days"]:
        return False, met, f"older than {c['max_days']} days"
    met.append("posted " + ("today" if age < 1 else f"{int(age)} day{'s' if int(age) > 1 else ''} ago")
               if job["posted_at"] else f"first seen {'today' if age < 1 else f'{int(age)} d ago'} (no posting date)")
    place = job.get("location") or ""
    if REMOTE.search(f"{place} | {job['title']}"):
        met.append("remote / nationwide")
    else:
        km = km_from_center(place) if place else None
        if km is None and " · " in job["title"]:  # some alert emails put "Company · City (Remote)" into the title
            km = km_from_center(job["title"].rsplit(" · ", 1)[1])
        if km is None and company and company.get("lat") is not None and not place:
            center = CONFIG["companies"]["places"].get(c["center"])
            km = round(_km((company["lat"], company["lon"]), tuple(center)), 1) if center else None
        if km is None:
            return False, met, "place unknown"
        if km > c["max_km"]:
            return False, met, f"{round(km)} km from {c['center']}"
        met.append(f"{round(km)} km from {c['center']}")
    if job.get("ai_blocker") is None:
        return False, met, "blockers not checked yet"
    if job["ai_blocker"]:
        return False, met, f"blocker: {job['ai_blocker']}"
    if STAFFING_FIRM.search(job.get("company") or "") or company and size_of(company) == "staffing agency":
        return False, met, "staffing agency"
    met.append("no blockers")
    signs = []
    if site == "company":
        signs.append(SIGN_LABELS["own_site"])
    size = size_of(company) if company else None
    if size in ("small", "mid"):
        signs.append(f"{'small' if size == 'small' else 'mid-size'} company")
    if history:
        signs.append(SIGN_LABELS["history"])
    if company and company.get("category") in c["low_competition_categories"]:
        signs.append(CATEGORY_LABELS[company["category"]])
    if not signs:
        return False, met, "no low-competition sign"
    met.append("low competition: " + ", ".join(signs))
    return True, met, ""


def update() -> int:
    """Check the candidates (new, recent, high score) and put the ones that meet everything in the box.
    Returns how many came in."""
    from .companies import clean_name
    from .discover import company_key
    from .source_types import site
    c = cfg()
    candidates = db.must_candidates(c["min_score"], c["max_days"] + 1)
    if not candidates:
        return 0
    by_key = {r["name_key"]: r for r in db.company_rows()}
    # Werkstudent jobs per company: (simplified title, first seen). The same ad from 3 sources counts once.
    student_jobs: dict[str, list[tuple[str, str]]] = {}
    for company, dedup, seen in db.employer_jobs():
        if STUDENT.search(dedup):
            student_jobs.setdefault(company_key(clean_name(company)), []).append((dedup.split("|")[0], seen))
    links = db.link_kinds()
    added = 0
    for job in candidates:
        company = _company(job, by_key)
        key = company_key(clean_name(job.get("company") or ""))
        # "Before": a different Werkstudent ad, found at least a week before this one
        before = (datetime.fromisoformat(job["first_seen"]) - timedelta(days=7)).isoformat()
        own = job["dedup_key"].split("|")[0]
        history = len({t for t, seen in student_jobs.get(key, []) if t != own and seen < before})
        ok, met, why_not = check(job, company, history, site(links.get(job["link_id"]), job["source"], job["url"]))
        db.save_must(job["id"], ok, met, why_not)
        added += ok
    if added:
        log.info("Must apply: %d new job(s)", added)
    return added


RULE = "1"  # bump when the conditions change: recent good jobs are checked again


def prepare() -> None:
    """Once: recent good jobs were scored before the AI also checked blockers. Score them again."""
    if db.get_setting("must_rule") == RULE:
        return
    n = db.rescore_recent(cfg()["min_score"], cfg()["max_days"] + 1)
    db.set_setting("must_rule", RULE)
    log.info("Must apply: %d recent good job(s) will be scored again (blocker check)", n)
