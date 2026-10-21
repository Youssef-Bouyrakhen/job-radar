"""Runs the checks in the background.

Every 5 minutes it looks for links that are "due" (last check older than the interval
in config.yaml). This way each link is checked at most once per interval, even if you
restart the app often.
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .checker import check_due_links
from .config import CONFIG

scheduler = BackgroundScheduler()


def start() -> None:
    scheduler.add_job(check_due_links, "interval", seconds=TICK.total_seconds(), id="check_due_links",
                      max_instances=1, coalesce=True, misfire_grace_time=None,
                      next_run_time=datetime.now() + timedelta(seconds=20))
    apps = CONFIG["applications"]
    if apps.get("enabled"):  # replies to your applications (the "Bewerbungen" label)
        from . import applications
        scheduler.add_job(applications.scheduled, "interval", minutes=apps["check_every_minutes"], id="applications",
                          max_instances=1, coalesce=True, next_run_time=datetime.now() + timedelta(seconds=60))
    hour = CONFIG["companies"].get("daily_run_hour")
    if hour is not None:  # Company Discovery Engine once a day (German time), like pressing "Run discovery"
        from . import companies  # imported here: companies needs the checker's modules first
        scheduler.add_job(companies.start, "cron", hour=hour, minute=0, id="companies", coalesce=True, timezone=GERMANY)
    read_hour = CONFIG["companies"].get("watched_read_hour")
    if read_hour is not None:  # the jobs of every watched company, once a day (your links: every hour anyway)
        from . import company_jobs
        scheduler.add_job(company_jobs.read_watched, "cron", hour=read_hour, minute=0, id="watched_read",
                          max_instances=1, coalesce=True, misfire_grace_time=3600, timezone=GERMANY)
    weekly = CONFIG["companies"].get("weekly_summary") or {}
    if weekly.get("day"):  # Sunday evening: one Telegram message with the week's numbers
        from . import weekly as summary
        scheduler.add_job(summary.scheduled, "cron", day_of_week=weekly["day"], hour=weekly.get("hour", 19),
                          minute=0, id="weekly_summary", coalesce=True, misfire_grace_time=3 * 3600, timezone=GERMANY)
    scheduler.start()


TICK = timedelta(minutes=5)
GERMANY = "Europe/Berlin"  # the server's clock runs on UTC: "19:00" means 19:00 in Germany


def next_check_time(due: datetime | None) -> datetime | None:
    """The scheduler round in which a link that is due at `due` will actually be checked."""
    job = scheduler.get_job("check_due_links")
    tick = job.next_run_time if job else None
    if due is None or tick is None:
        return due or tick
    if due <= tick:
        return tick
    rounds = -(-(due - tick) // TICK)  # round up to the next 5-minute round
    return tick + rounds * TICK


def stop() -> None:
    scheduler.shutdown(wait=False)
