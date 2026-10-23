"""Runs the checks in the background.

Every 5 minutes it looks for links that are "due" (last check older than the interval
in config.yaml). This way each link is checked at most once per interval, even if you
restart the app often.
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .checker import check_due_links
from .clock import GERMANY, now
from .config import CONFIG

# German time, summer / winter time included: "19:00" is 19:00 in Germany all year (the server's clock runs on UTC)
scheduler = BackgroundScheduler(timezone=GERMANY)


def start() -> None:
    scheduler.add_job(check_due_links, "interval", seconds=TICK.total_seconds(), id="check_due_links",
                      max_instances=1, coalesce=True, misfire_grace_time=None,
                      next_run_time=now() + timedelta(seconds=20))
    apps = CONFIG["applications"]
    if apps.get("enabled"):  # replies to your applications (the "Bewerbungen" label)
        from . import applications
        scheduler.add_job(applications.scheduled, "interval", minutes=apps["check_every_minutes"], id="applications",
                          max_instances=1, coalesce=True, next_run_time=now() + timedelta(seconds=60))
    # The fixed runs of the day plan (schedule.py): 08:00 LinkedIn, 13:00 read (Monday to Friday), 19:00 read,
    # 03:00 night run. Imported here: companies needs the checker's modules first
    from . import companies, company_jobs, linkedin, schedule
    what = {"linkedin": linkedin.scheduled, "midday": company_jobs.read_midday, "evening": company_jobs.read_watched,
            "night": companies.scheduled_run}
    for run in schedule.runs():  # a read or search that is late (busy server) still runs within the hour
        late = {} if run["key"] == "night" else {"misfire_grace_time": 3600}
        scheduler.add_job(what[run["key"]], run["trigger"], id=run["key"], max_instances=1, coalesce=True, **late)
    if CONFIG["companies"].get("auto_resume", True):  # Companies runs go on by themselves while there is work
        from . import auto_resume
        scheduler.add_job(auto_resume.tick, "interval", seconds=TICK.total_seconds(), id="auto_resume",
                          max_instances=1, coalesce=True, next_run_time=now() + timedelta(minutes=2))
    weekly = CONFIG["companies"].get("weekly_summary") or {}
    if weekly.get("day"):  # Sunday evening: one Telegram message with the week's numbers
        from . import weekly as summary
        scheduler.add_job(summary.scheduled, "cron", day_of_week=weekly["day"], hour=weekly.get("hour", 19),
                          minute=0, id="weekly_summary", coalesce=True, misfire_grace_time=3 * 3600, timezone=GERMANY)
    scheduler.start()


TICK = timedelta(minutes=5)


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
