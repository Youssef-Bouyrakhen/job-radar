"""Auto-resume: Companies / Coverage runs go on by themselves, you don't start them by hand.

Every 5 minutes (tick): if no Companies run is going and there is still work - companies never checked,
or companies with a careers page whose job list was never read - an "auto" run starts. It works through the
queue (the same steps as the night run, without collecting the sources again: that's the 03:00 run's job).
Before that, a "landing" run when job lists behind landing pages are due (landing.py; needs no AI).
After a restart or deploy the scheduler starts again, the next tick (within 5 minutes) finds the work that
is left and goes on: nothing is lost, a company is only saved when its check is finished.

No endless loops: a run that ended without any progress (the work left didn't get smaller) is not repeated
right away. It waits until an AI is available again (all free providers at their limit: until the first one's
real reset; the rest of today kept for scoring jobs: until a provider's free day starts again),
until midnight (the free web searches of today are used up), or 1, 2, 4 ... max 24 hours (anything else).
It also starts again when the amount of work changes (the night run found new companies).

Never at the same time as the fixed runs of the day plan (schedule.py): no start in the 30 minutes before
03:00 (night run), 08:00 (LinkedIn), 13:00 (read, Monday to Friday) and 19:00 (read), an auto run stops
starting new companies 15 minutes before them, and nothing starts while one of them is going. The polite crawler and the web search limits are the same as always.
The state is kept in the database (settings "auto_resume"), so it survives a restart.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta

from . import ai, clock, companies, company_jobs, db, landing, linkedin, schedule, websearch
from .config import CONFIG

log = logging.getLogger("jobradar")

GERMANY = clock.GERMANY
BEFORE_FIXED = timedelta(minutes=30)  # no start this long before the fixed runs (schedule.py)
STOP_BEFORE = timedelta(minutes=15)   # an auto run stops starting new companies this long before them
RETRY_AFTER = timedelta(hours=1)      # no progress although AI and web search were there: try again later
BOOT = uuid.uuid4().hex               # this start of Job Radar: a run of an earlier start was stopped by a restart


def enabled() -> bool:
    return bool(CONFIG["companies"].get("auto_resume", True))


def _load() -> dict:
    return json.loads(db.get_setting("auto_resume") or "{}")


def _save(state: dict) -> None:
    db.set_setting("auto_resume", json.dumps(state))


def work() -> dict:
    """What is left: companies never checked, careers pages whose job list was never read, and job lists
    that changed but no AI was left to read them (e.g. skipped by the 19:00 read: read the same evening
    or when an AI is back, not a week later)."""
    rows, waiting = db.company_rows(), company_jobs.ai_waiting()
    return {"unchecked": sum(1 for c in rows if not c["last_checked"] and c["status"] not in ("skipped", "initiativ")),
            "unread": sum(1 for c in rows if c["career_url"] and c["status"] != "skipped" and not c["jobs_read_at"]),
            "waiting_ai": sum(1 for c in rows if c["id"] in waiting and c["status"] != "skipped" and c["jobs_read_at"])}


def ai_blocked() -> bool:
    """The AI is on, but no free provider can take a company request now (used up, paused, or the rest of
    today is kept for scoring jobs)."""
    return ai.enabled() and (ai.next_provider() is None or not ai._allowed("jobs"))


def ai_saved() -> bool:
    """An AI could take the request, but the rest of today is kept for scoring jobs (ai_budget.keep_percent)."""
    return ai.next_provider() is not None and not ai._allowed("jobs")


def ai_back_at(now: datetime) -> datetime:
    """When an AI is available again. Every provider at its limit or paused: when the first one is back (each at its
    own real reset). Kept for scoring jobs: when the next provider's free day starts again (more requests left)."""
    back = ai.next_reset() if ai_saved() else ai.provider_status()["next_back_at"]
    if isinstance(back, str):
        back = datetime.fromisoformat(back)
    return back.astimezone(GERMANY) if back else now + timedelta(minutes=5)


def _fixed_times(now: datetime) -> list[tuple[datetime, str]]:
    """The next start of each fixed run of the day plan (schedule.py): 08:00 LinkedIn, 13:00 read (Monday
    to Friday), 19:00 read, 03:00 night run (German time)."""
    return schedule.fixed_times(now)


def _next_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _decide(now: datetime, state: dict, left: dict) -> tuple[str, datetime | None, str]:
    """(state, until, why) without starting anything: running / busy / done / window / ai / ai_off / search /
    retry / start."""
    p = companies.progress()
    if p["running"]:
        return ("running", None, "") if p["kind"] == "auto" else ("busy", None, "another Companies run is going")
    if company_jobs.reading_watched.is_set():
        return "busy", None, f"the {company_jobs.READS[company_jobs.reading_kind['now'] or 'evening'][1]} is going"
    if linkedin.running():
        return "busy", None, "the LinkedIn search is going"
    total = sum(left.values())
    if not total:
        return "done", None, ""
    fixed = _fixed_times(now)
    if fixed and fixed[0][0].timestamp() - now.timestamp() <= BEFORE_FIXED.total_seconds():  # real time, also on clock-change night
        return "window", fixed[0][0], fixed[0][1]
    # The last run got nowhere: start again only when its reason is gone, or the work changed (the night
    # run found new companies, you changed something)
    why = state.get("waiting_for") if state.get("left") == total else None
    if why == "ai" and ai_blocked():
        if ai_saved():
            return "ai_saved", ai_back_at(now), "the AI left today is kept for scoring jobs"
        return "ai", ai_back_at(now), "the companies left need the AI, every free AI is at its limit"
    if why == "ai_off" and not ai.enabled():
        return "ai_off", None, "the companies left need the AI, which is off"
    if why == "search" and websearch.limit_reached():
        return "search", _next_midnight(now), "the companies left need the web search, today's free searches are used up"
    if why == "retry" and now < (retry := datetime.fromisoformat(state["retry_at"])):
        return "retry", retry, "the last run made no progress"
    return "start", None, ""


def _no_progress(now: datetime, state: dict, total: int) -> dict:
    """What a run that got nowhere waits for. Tried again without a reason: after 1, 2, 4 ... max 24 hours."""
    if ai_blocked():
        return {"waiting_for": "ai", "left": total}
    if websearch.limit_reached():
        return {"waiting_for": "search", "left": total}
    if not ai.enabled():
        return {"waiting_for": "ai_off", "left": total}
    misses = state.get("misses", 0) + 1
    retry = now + min(RETRY_AFTER * 2 ** (misses - 1), timedelta(hours=24))
    return {"waiting_for": "retry", "left": total, "misses": misses, "retry_at": retry.isoformat(timespec="seconds")}


def tick() -> str:
    """Every 5 minutes (scheduler). Starts an auto run if there is work and nothing stands in the way.
    Returns what it decided (for the log and the test)."""
    if not enabled():
        return "off"
    now, state, left = datetime.now(GERMANY), _load(), work()
    total = sum(left.values())
    if state.get("work_before") is not None and not companies.progress()["running"]:
        # The run started last time has ended: did it get anywhere? One that a restart stopped is just
        # started again (it didn't finish, so it can't have failed)
        if state.get("boot") != BOOT:
            log.info("Auto-resume: the last run was stopped by a restart, going on where it stopped")
            state = {k: v for k, v in state.items() if k not in ("work_before", "started_at", "boot")}
        elif total >= state["work_before"]:
            state = _no_progress(now, state, total)
            log.info("Auto-resume: the last run made no progress (%d left): waiting for %s", total, state["waiting_for"])
        else:
            state = {}
        _save(state)
    decision, until, why = _decide(now, state, left)
    if decision == "done" and state:
        _save({})  # all done: nothing waits for anything any more
    fixed = _fixed_times(now)
    stop_at = (fixed[0][0] - STOP_BEFORE).timestamp() if fixed else None
    # Job lists behind landing pages (landing.py) first: they need no AI, so they don't wait for one
    if decision not in ("running", "busy", "window") and not (fixed and fixed[0][0] - now <= BEFORE_FIXED) \
            and (n := landing.work()) and companies.start("landing", stop_at=stop_at):
        log.info("Auto-resume: landing-page run started (%d companies)", n)
        return "landing"
    if decision != "start":
        return decision
    if companies.start("auto", stop_at=stop_at):
        _save({**state, "work_before": total, "started_at": now.isoformat(timespec="seconds"), "boot": BOOT})
        log.info("Auto-resume: run started (%d companies to check, %d job lists to read, %d waiting for the AI)",
                 left["unchecked"], left["unread"], left["waiting_ai"])
        return "started"
    return "busy"


def _clock(at: datetime, now: datetime) -> str:
    at = at.astimezone(GERMANY)
    return at.strftime("%H:%M") + ("" if at.date() == now.date() else " tomorrow")


def status() -> dict:
    """For the Coverage page: {"state", "text", "until", "unchecked", "unread"}."""
    if not enabled():
        return {"state": "off", "text": "Auto-resume: off (companies.auto_resume in config.yaml)"}
    now, left = datetime.now(GERMANY), work()
    decision, until, why = _decide(now, _load(), left)
    todo = f"{left['unchecked']} companies to check, {left['unread']} job lists to read" \
        + (f", {left['waiting_ai']} waiting for the AI" if left["waiting_ai"] else "")
    text = {
        "running": f"Auto-resume: running ({todo})",
        "busy": f"Auto-resume: waits, {why}",
        "done": "Auto-resume: all done (starts again when new work appears)",
        "window": f"Auto-resume: waits for {why}",
        "ai": f"Auto-resume: waiting for AI until {_clock(until, now) if until else 'later'} ({todo})",
        "ai_saved": f"Auto-resume: waits, {why}, goes on at {_clock(until, now) if until else 'later'} ({todo})",
        "ai_off": f"Auto-resume: waits, {why} ({todo})",
        "search": f"Auto-resume: waiting for the web search until {_clock(until, now) if until else 'midnight'} ({todo})",
        "retry": f"Auto-resume: {why}, tries again at {_clock(until, now) if until else 'later'} ({todo})",
        "start": f"Auto-resume: starts within 5 minutes ({todo})",
    }[decision]
    return {"state": decision, "text": text, "until": until.isoformat(timespec="seconds") if until else None, **left}
