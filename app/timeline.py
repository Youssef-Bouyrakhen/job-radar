"""The Timeline page: the day plan of the next 7 days (German time), what is running now with its progress,
the last result of each run, and whether a deploy is safe now.

Everything comes from the real plan (schedule.py, config.yaml): change an hour there and the page follows.
Times on the board are German wall-clock minutes (0-1440); every event also has its exact time (ISO, UTC)
for the countdowns.
"""
import math
from datetime import date, datetime, time, timedelta, timezone

from . import ai, auto_resume, companies, company_jobs, db, linkedin, schedule, scheduler
from .checker import checking_all
from .clock import GERMANY
from .config import CONFIG

# How long a fixed run usually takes (minutes) until it has run once; a longer last run counts instead.
# Also the "don't deploy" windows: DEPLOY_BEFORE minutes before a run until this long after its start.
USUAL = {"night": 30, "night_full": 60, "linkedin": 15, "midday": 30, "evening": 45}
DEPLOY_BEFORE = 15
DAY = 24 * 60

RUN_LABELS = {"run": "Night run", "auto": "Auto-resume", "landing": "Landing pages (auto-resume)",
              "recheck": "Careers pages looked for again", "read": "Never-read job lists",
              "import": "Imported list", "lookalikes": "Look-alike companies"}


def _iso(at: datetime | None) -> str | None:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds") if at else None


def _minute(at: datetime) -> int:
    at = at.astimezone(GERMANY)
    return at.hour * 60 + at.minute


def _round5(seconds: float | None) -> int:
    return int(math.ceil((seconds or 0) / 300) * 5)


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def last_results() -> dict:
    """The last result of each kind of work: {"night", "night_full", "auto", "landing", "linkedin", "midday",
    "evening", "hourly"}, each with finished_at (ISO) and a short text of what it found."""
    runs, found = companies.last_runs(), {}
    for key, kind in (("night", "run"), ("night_full", "run_full"), ("auto", "auto"), ("landing", "landing")):
        r = runs.get(kind)
        if not r:
            continue
        parts = []
        if r.get("collected"):
            parts.append(f"{r['collected']} businesses collected, {r['new']} new companies")
        if r.get("total"):
            parts.append(f"{r['done']} of {r['total']} checked")
        if r.get("jobs_read"):
            parts.append(f"{r['jobs_read']} job lists read, {r['new_jobs']} new jobs")
        if r.get("careers"):
            parts.append(f"{r['careers']} careers pages found")
        if r.get("error"):
            parts.append(f"stopped: {r['error']}")
        found[key] = {"finished_at": r.get("finished_at"), "seconds": r.get("seconds"),
                      "text": ", ".join(parts) or "nothing new to do"}
    li = linkedin.last()
    if li.get("at"):
        text = (f"LinkedIn blocked the search: {li['error']}" if li.get("blocked") else
                f"{', '.join(li.get('keywords') or []) or 'no keywords'}: {li.get('jobs', 0)} jobs, "
                f"{li.get('new', 0)} new companies" + (f" ({li['error']})" if li.get("error") else ""))
        found["linkedin"] = {"finished_at": li.get("finished_at") or li["at"],
                             "seconds": _seconds_between(li["at"], li.get("finished_at")), "text": text}
    for kind in ("midday", "evening"):
        r = company_jobs.last_watched_read(kind)
        if r:
            text = f"{r['read']} of {r['companies']} companies read, {r['new_jobs']} new jobs, {r['new_good']} match your filter"
            if r.get("ai_skipped"):
                text += f", {r['ai_skipped']} skipped (no AI left)"
            found[kind] = {"finished_at": r["at"], "seconds": r.get("seconds"), "text": text}
    found["hourly"] = _hourly_result()
    return found


def _hourly_result() -> dict:
    """The watched links (incl. Arbeitsagentur and Gmail) of the last interval: how many were checked, new jobs."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=CONFIG["check_interval_minutes"])).isoformat(timespec="seconds")
    with db.connect() as conn:
        links = conn.execute("SELECT COUNT(*) FROM links WHERE active = 1 AND last_checked >= ?", (since,)).fetchone()[0]
        new, good = conn.execute("SELECT COUNT(*), COALESCE(SUM(filter_passed), 0) FROM jobs WHERE link_id IS NOT NULL "
                                 "AND first_seen >= ?", (since,)).fetchone()
    return {"finished_at": db.last_check(), "seconds": None,
            "text": f"{links} links checked in the last hour, {new} new jobs, {good} passed the keyword filter"}


def _minutes(key: str, last: dict) -> int:
    """How long the run usually takes: the usual length, or the last run's if that was longer."""
    return max(USUAL[key], min(_round5((last.get(key) or {}).get("seconds")), 4 * 60))


def _ai_resets(day: date) -> list[dict]:
    """The AI providers whose free day starts again on this German day: [{"name", "at"}]. Groq has no fixed reset."""
    resets = {}
    for name in ai.providers():
        if name in ai.ROLLING:
            continue
        zone = ai._zone(name)
        for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
            at = datetime.combine(d, time(), zone).astimezone(GERMANY)
            if at.date() == day:
                resets.setdefault(at, []).append(name)
    return [{"at": _iso(at), "minute": _minute(at), "name": " + ".join(names)} for at, names in sorted(resets.items())]


def _span(start: int, end: int) -> list[int]:
    return [max(0, start), min(DAY, end)]


def plan_day(day: date, last: dict) -> dict:
    """One German day: the fixed runs with their usual length, auto-resume pauses, don't-deploy windows, AI resets."""
    start = datetime.combine(day, time(), GERMANY)
    events = []
    night_end = CONFIG["companies"].get("night_end_hour")
    for r in schedule.runs():
        at = r["trigger"].get_next_fire_time(None, start)
        if not at or at.astimezone(GERMANY).date() != day:
            continue
        key, minute, note = r["key"], _minute(at), schedule.DAY_NAMES.get(r["days"], r["days"])
        size, longest = key, None
        if key == "night":
            full = companies.collect_due(at)
            size = "night_full" if full else "night"
            note = "all areas collected again, then the queue" if full else "new companies and the ones left only"
            if night_end is not None:
                longest = night_end * 60 - companies.NIGHT_MARGIN_MINUTES  # stops starting new companies then
        elif key == "midday":
            note = "the busy Watched / Initiativ companies (a new job in the last " \
                   f"{CONFIG['companies']['busy_days']} days)"
        elif key == "evening":
            note = f"busy companies every day, quiet ones every {CONFIG['companies']['quiet_every_days']} days"
        elif key == "linkedin":
            note = f"{CONFIG['linkedin'].get('keywords_per_day', 2)} keywords, new hiring companies go into Companies"
        length = _minutes(size, last)
        end = minute + length
        events.append({"key": key, "label": r["label"], "at": _iso(at), "minute": minute, "minutes": length,
                       "until": max(end, longest) if longest else None, "note": note,
                       "auto_pause": _span(minute - int(auto_resume.BEFORE_FIXED.total_seconds() // 60), end),
                       "no_deploy": _span(minute - DEPLOY_BEFORE, end)})
    events.sort(key=lambda e: e["minute"])
    weekly = CONFIG["companies"].get("weekly_summary") or {}
    extra = []
    if weekly.get("day") and companies.DAYS.index(weekly["day"][:3].lower()) == day.weekday():
        at = start.replace(hour=weekly.get("hour", 19))
        extra.append({"key": "weekly", "label": "Weekly summary", "at": _iso(at), "minute": _minute(at),
                      "note": "one Telegram message with the week's numbers"})
    return {"date": day.isoformat(), "weekday": day.weekday(), "events": events, "extra": extra,
            "ai_resets": _ai_resets(day)}


def running_now() -> list[dict]:
    """What is going now, with its progress: [{"key", "label", "step", "done", "total", "started_at"}]."""
    found = []
    p = companies.progress()
    if p["running"]:
        key = "night" if p["kind"] == "run" else "auto" if p["kind"] in ("auto", "landing") else p["kind"]
        found.append({"key": key, "label": RUN_LABELS.get(p["kind"], p["kind"]), "step": p["step"],
                      "done": p["done"], "total": p["total"], "started_at": p.get("started_at")})
    kind = company_jobs.reading_kind["now"]
    if kind:
        r = company_jobs.reading_progress.get("progress") or {}
        found.append({"key": kind, "label": company_jobs.READS[kind][1], "step": r.get("step", ""),
                      "done": r.get("done", 0), "total": r.get("total", 0),
                      "started_at": company_jobs.reading_progress.get("started_at")})
    if linkedin.running():
        s = linkedin.now_searching
        found.append({"key": "linkedin", "label": "LinkedIn search", "step": f"\"{s['word']}\"" if s["word"] else "",
                      "done": s["done"], "total": s["total"], "started_at": None})
    if checking_all():
        found.append({"key": "hourly", "label": "Checking all watched links", "step": "", "done": 0, "total": 0,
                      "started_at": None})
    return found


def status() -> dict:
    now = datetime.now(GERMANY)
    last = last_results()
    interval = CONFIG["check_interval_minutes"]
    due = db.next_due(interval)
    next_check = scheduler.next_check_time(datetime.fromisoformat(due) if due else None)
    days = [plan_day(now.date() + timedelta(days=n), last) for n in range(8)]
    going = running_now()
    # A deploy restarts Job Radar and stops what is running: not in a window, not while a fixed run goes
    minute, blocked = _minute(now), None
    for e in days[0]["events"]:
        span = e["no_deploy"]
        if span[0] <= minute < span[1]:
            blocked = {"why": f"the {e['label']}", "until": _iso(datetime.combine(now.date(), time(), GERMANY)
                                                                   + timedelta(minutes=span[1]))}
    fixed = [g for g in going if g["key"] in ("night", "linkedin", "midday", "evening")]
    if fixed and not blocked:
        blocked = {"why": f"the {fixed[0]['label']} is running", "until": None}
    return {
        "server_time": _iso(now),
        "days": days,
        "hourly": {"interval": interval, "next_check": _iso(next_check), "last_check": db.last_check(),
                   "links": sum(1 for l in db.list_links() if l["active"])},
        "running": going,
        "auto": auto_resume.status(),
        "last": last,
        "rolling_ai": [p for p in ai.providers() if p in ai.ROLLING],
        "deploy": {"safe": blocked is None, **(blocked or {})},
        "deploy_before": DEPLOY_BEFORE,
        "auto_before": int(auto_resume.BEFORE_FIXED.total_seconds() // 60),
    }
