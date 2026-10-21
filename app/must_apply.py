"""🔥 Must apply: the few jobs worth applying to today. A job goes in only if ALL are true:

1. Werkstudent / working student / studentische Hilfskraft, the ad states the hours and they are max 20 h/week
2. AI score 8 or higher
3. posted in the last 3 days (no posting date: first seen)
4. in your area: within 40 km of Darmstadt (area: in config.yaml), or remote / Homeoffice / Bundesweit.
   A job without a known place is never in the area (never "0 km").
5. no blockers: the AI checks "2+ years experience", staffing agency, Master-only in the scoring request
6. at least one low-competition sign: from the company's own careers page / applicant system, a small or
   mid-size company, the company posted Werkstudent jobs before, or its category is Insurance,
   Banks/Finance, Cloud/Data centers/Networks or Cybersecurity

The numbers are in config.yaml (must_apply: and area:).
Jobs outside your area are never sent to Telegram either (update_areas, before every Telegram round). Once in, a job stays until you mark it Applied or hide it.
Telegram: a "🔥 MUST APPLY" message right away, and one reminder after 24 h if you haven't applied or hidden it.
"""
import json
import logging
import math
import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import httpx

from . import db, details
from .config import CONFIG
from .fetcher import BOT_HEADERS
from .filters import hours_of, max_per_week

log = logging.getLogger("jobradar")

STUDENT = re.compile(r"werk\s?-?student|working[- ]student|studentische[rn]?\s+(?:hilfskraft|aushilfe|mitarbeit|"
                     r"besch[äa]ftigung)|student(?:ische)?\s+assistant|studentjob|\bhiwi\b", re.I)
FULL_TIME = re.compile(r"vollzeit|full[- ]?time", re.I)
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


def area() -> dict:
    return CONFIG["area"]


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
    # A postcode is the surest: "Sankt Augustin (53757)", "64283 Darmstadt"
    if (plz := re.search(r"\b(\d{5})\b", location)) and (found := _look_up(plz[1])):
        return found
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
    center = CONFIG["companies"]["places"].get(area()["center"])
    found = place_of(location) if location else None
    return round(_km(found, tuple(center)), 1) if found and center else None


# ---- your area -------------------------------------------------------------------------

NOT_A_PLACE = re.compile(r"^(?:de|deu|deutschland|germany|d)$", re.I)


def _places(location: str) -> list[str]:
    parts = [re.sub(r"\s*\(.*?\)", "", p).strip(" -") for p in re.split(r"[,;/|]| oder | or | und | and ", location)]
    return [p for p in dict.fromkeys(parts) if len(p) >= 2 and not NOT_A_PLACE.match(p)][:8] or [location]


def area_of(job: dict) -> tuple[str, float | None]:
    """("remote" / "near" / "outside" / "unknown", km). No place = "unknown", never the company's office:
    Debeka has an office in Darmstadt, its Werkstudent job was in Koblenz."""
    place = job.get("location") or ""
    if REMOTE.search(f"{place} | {job['title']}"):
        return "remote", None
    # "Dortmund, Berlin, Darmstadt, Dresden": the nearest of the places counts
    found = [km for part in _places(place) if (km := km_from_center(part)) is not None]
    km = min(found) if found else None
    if km is None and " · " in job["title"]:  # some alert emails put "Company · City (Remote)" into the title
        km = km_from_center(job["title"].rsplit(" · ", 1)[1])
    if km is None:
        return "unknown", None
    return ("near" if km <= area()["max_km"] else "outside"), km


def area_note(job: dict, km: float | None) -> str:
    """Why a job isn't in your area, for Must apply."""
    if km is None:
        return "Location unknown"
    city = re.split(r"[,;/(]| oder | or ", job.get("location") or "")[0].strip() or job["title"].rsplit(" · ", 1)[-1]
    return f"Outside your area ({city}, {round(km)} km from {area()['center']})"


def update_areas() -> int:
    """Remote / near / outside / unknown for shown jobs that don't have it yet. "Unknown" ones are
    tried again (places are remembered, so that costs nothing unless the place lookup failed)."""
    jobs = db.jobs_without_area()
    for j in jobs:
        db.save_area(j["id"], *area_of(j))
    return len(jobs)


# ---- the six conditions ------------------------------------------------------------------

def _max_hours(job: dict) -> float | None:
    """Most hours a week ("40 h/month ≈ 9 h/week" -> 9)."""
    if (saved := max_per_week(job.get("hours"))) is not None:
        return saved
    found = hours_of(job["title"], job.get("employment") or "", job.get("description") or "",
                     f"{job.get('company') or ''} | {job.get('source') or ''} | {job.get('url') or ''}")
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
    head = f"{job['title']} | {job.get('employment') or ''}"
    if not STUDENT.search(head):  # "Vollzeit" alone is full-time; with Werkstudent the hours decide (below)
        return False, met, "full-time ('Vollzeit')" if FULL_TIME.search(head) else "not a Werkstudent job"
    hours = _max_hours(job)
    if hours is None:
        return False, met, "hours unknown (the ad doesn't say)"
    if hours > c["max_hours"]:
        return False, met, f"more than {c['max_hours']} h/week"
    if job.get("ai_full_time"):  # "Vollzeit" without student hours (the AI read the whole ad)
        return False, met, "full-time"
    met.append(f"Werkstudent, max {hours:g} h/week")
    # The place before the score: a job outside your area has its score capped at 5, and that's the real reason
    where, km = area_of(job)
    if where in ("unknown", "outside"):
        return False, met, area_note(job, km)
    if (job.get("ai_score") or 0) < c["min_score"]:
        return False, met, f"AI score below {c['min_score']}"
    met.append(f"AI score {job['ai_score']}/10")
    age = _age_days(job)
    if age > c["max_days"]:
        return False, met, f"older than {c['max_days']} days"
    met.append("posted " + ("today" if age < 1 else f"{int(age)} day{'s' if int(age) > 1 else ''} ago")
               if job["posted_at"] else f"first seen {'today' if age < 1 else f'{int(age)} d ago'} (no posting date)")
    met.append("remote / nationwide" if where == "remote" else f"{round(km)} km from {area()['center']}")
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
    c = cfg()
    added = 0
    for job, ok, met, why_not in _judge(db.must_candidates(c["min_score"], c["max_days"] + 1)):
        db.save_must(job["id"], ok, met, why_not)
        added += ok
    if added:
        log.info("Must apply: %d new job(s)", added)
    return added


def _judge(jobs: list[dict]) -> list[tuple[dict, bool, list[str], str]]:
    """(job, all met, the conditions it met, the first one it failed) for each job."""
    from .companies import clean_name
    from .discover import company_key
    from .source_types import site
    if not jobs:
        return []
    by_key = {r["name_key"]: r for r in db.company_rows()}
    # Werkstudent jobs per company: (simplified title, first seen). The same ad from 3 sources counts once.
    student_jobs: dict[str, list[tuple[str, str]]] = {}
    for company, dedup, seen in db.employer_jobs():
        if STUDENT.search(dedup):
            student_jobs.setdefault(company_key(clean_name(company)), []).append((dedup.split("|")[0], seen))
    links = db.link_kinds()
    judged = []
    for job in jobs:
        company = _company(job, by_key)
        key = company_key(clean_name(job.get("company") or ""))
        # "Before": a different Werkstudent ad, found at least a week before this one
        before = (datetime.fromisoformat(job["first_seen"]) - timedelta(days=7)).isoformat()
        own = job["dedup_key"].split("|")[0]
        history = len({t for t, seen in student_jobs.get(key, []) if t != own and seen < before})
        judged.append((job, *check(job, company, history, site(links.get(job["link_id"]), job["source"], job["url"]))))
    return judged


RULE = "1"  # bump when the conditions change: recent good jobs are checked again


def prepare() -> None:
    """Once: recent good jobs were scored before the AI also checked blockers. Score them again."""
    if db.get_setting("must_rule") == RULE:
        return
    n = db.rescore_recent(cfg()["min_score"], cfg()["max_days"] + 1)
    db.set_setting("must_rule", RULE)
    log.info("Must apply: %d recent good job(s) will be scored again (blocker check)", n)


AREA_RULE = "2"  # bump when the area / hours rules change: current jobs and the Must apply box are checked again


def recheck_jobs() -> Counter | None:
    """Once per rule change: the full ad for shown jobs without a place (or hours), every job's area again,
    and the Must apply box with the new rules. A job that fails a rule leaves the box; one that is only
    older than max_days stays (the box keeps a job until you apply or hide it).
    Returns {why jobs left the box: how many}."""
    if db.get_setting("area_rule") == AREA_RULE:
        return None
    from .checker import refilter_all_jobs  # it imports modules that import this one
    from .dedup import dedup_key
    from .extractors.alerts import linkedin_card
    fixed = 0
    for j in db.linkedin_misread():  # the card's lines are in the description: "Title · Company · Place · ..."
        if card := linkedin_card(j["title"], (j["description"] or "").split(" · ")):
            db.fix_job(j["id"], *card, dedup_key(card[0], card[1]))
            fixed += 1
    if fixed:
        log.info("Job-alert emails: %d LinkedIn jobs had the company as their place: title, company and place fixed", fixed)
        refilter_all_jobs()  # the keyword filter on the real titles
    todo = db.jobs_missing_details()
    for j in todo:
        details.complete(j)
    db.reset_areas()
    update_areas()
    left, box = Counter(), db.must_box()
    for job, ok, met, why_not in _judge(box):
        if ok or why_not.startswith("older than"):
            continue
        db.drop_must(job["id"], why_not)
        left["outside your area" if why_not.startswith("Outside") else why_not] += 1
        log.info("Must apply: '%s' (%s) left the box: %s", job["title"][:70], job["company"], why_not)
    for job, ok, met, why_not in _judge(db.must_explained()):  # "Not in Must apply" with the new rules
        db.save_must(job["id"], ok, met, why_not)
    db.set_setting("area_rule", AREA_RULE)
    log.info("Must apply: new area / hours rules: %d job(s) without a place or hours read again, %d of %d Must apply "
             "job(s) left the box%s", len(todo), sum(left.values()), len(box),
             "".join(f"; {n}x {why}" for why, n in left.items()))
    update()  # recent good jobs with the new rules (one may come in now)
    return left
