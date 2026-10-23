"""The day plan (German time, summer and winter time): the fixed runs, in one place for the scheduler,
auto-resume (it pauses before and during them), the Coverage page and the log.

  08:00 every day       LinkedIn search (linkedin.py)
  13:00 Monday-Friday   the busy Watched / Initiativ companies (company_jobs.read_midday)
  19:00 every day       busy companies, the quiet ones every third day (company_jobs.read_watched)
  03:00 every night     the queue; all areas collected again only in the night to Sunday (companies.scheduled_run)

Telegram alerts and your watched links are not part of it: they go on all day (scheduler.py).
"""
import logging
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

from .clock import GERMANY
from .config import CONFIG

log = logging.getLogger("jobradar")

DAY_NAMES = {"*": "every day", "mon-fri": "Monday to Friday", "sat,sun": "weekends"}


def runs() -> list[dict]:
    """[{"key", "label", "hour", "days", "trigger"}] of the fixed runs that are on (config)."""
    cfg, linkedin = CONFIG["companies"], CONFIG["linkedin"]
    found = []
    for key, label, hour, days, on in (
            ("linkedin", "LinkedIn search", linkedin.get("hour"), "*", linkedin.get("enabled")),
            ("midday", "{h} read", cfg.get("midday_read_hour"), cfg.get("midday_read_days") or "*", True),
            ("evening", "{h} read", cfg.get("watched_read_hour"), "*", True),
            ("night", "night run", cfg.get("daily_run_hour"), "*", True)):
        if on and hour is not None:
            found.append({"key": key, "label": label.format(h=f"{hour:02d}:00"), "hour": hour, "days": days,
                          "trigger": CronTrigger(day_of_week=days, hour=hour, minute=0, timezone=GERMANY)})
    return found


def next_at(run: dict, now: datetime) -> datetime:
    return run["trigger"].get_next_fire_time(None, now.astimezone(GERMANY))


def fixed_times(now: datetime) -> list[tuple[datetime, str]]:
    """The next start of each fixed run, soonest first: [(when, "the 13:00 read")]. For auto-resume."""
    return sorted((next_at(r, now), f"the {r['hour']:02d}:00 {r['label']}" if r["key"] in ("linkedin", "night")
                   else f"the {r['label']}") for r in runs())


def running() -> set[str]:
    """Which fixed runs are going now ("linkedin", "midday", "evening", "night") and "auto" (auto-resume)."""
    from . import companies, company_jobs, linkedin  # they import modules that import this one
    now = set()
    p = companies.progress()
    if p["running"]:
        now.add("night" if p["kind"] == "run" else "auto" if p["kind"] == "auto" else p["kind"])
    if company_jobs.reading_kind["now"]:
        now.add(company_jobs.reading_kind["now"])
    if linkedin.running():
        now.add("linkedin")
    return now


def status(now: datetime | None = None) -> list[dict]:
    """For the Coverage page: each fixed run with its next start and whether it is running now."""
    from .companies import collect_due
    now, going = (now or datetime.now(GERMANY)).astimezone(GERMANY), running()
    plan = []
    for r in runs():
        at = next_at(r, now)
        note = DAY_NAMES.get(r["days"], r["days"])
        if r["key"] == "night":
            note = "all areas collected again" if collect_due(at) else "new companies and the ones left only"
        elif r["key"] == "midday":
            note += ", busy companies only"
        elif r["key"] == "evening":
            note += f", quiet companies every {CONFIG['companies']['quiet_every_days']} days"
        plan.append({"key": r["key"], "label": r["label"], "next_at": at.isoformat(timespec="seconds"),
                     "running": r["key"] in going, "note": note})
    return sorted(plan, key=lambda p: p["next_at"])


def log_plan() -> None:
    """Once at startup: the real plan, with the next start of each run."""
    now = datetime.now(GERMANY)
    for p in status(now):
        at = datetime.fromisoformat(p["next_at"])
        when = at.strftime("%H:%M") if at.date() == now.date() else at.strftime("%a %d %b %H:%M")
        log.info("Plan: %s at %02d:00 (%s), next: %s%s", p["label"], at.hour, p["note"], when,
                 " - running now" if p["running"] else "")
