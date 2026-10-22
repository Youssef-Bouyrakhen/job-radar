"""German time for everything Job Radar shows or plans (dashboard, Telegram, logs, the 03:00 / 19:00 runs).

The server's clock runs on UTC, so "local time" there is NOT German time. Europe/Berlin switches between
summer time (UTC+2) and winter time (UTC+1) by itself: last Sunday of October and of March.
Times are saved in the database as UTC and only turned into German time when shown.
"""
from datetime import date, datetime

from zoneinfo import ZoneInfo

GERMANY = ZoneInfo("Europe/Berlin")


def now() -> datetime:
    return datetime.now(GERMANY)


def today() -> date:
    """Today's date in Germany (on the UTC server, 00:00-02:00 would otherwise still be yesterday)."""
    return now().date()


def german(when: datetime) -> datetime:
    """A saved time in German time. A time without a zone counts as German time."""
    return when.replace(tzinfo=GERMANY) if when.tzinfo is None else when.astimezone(GERMANY)


def log_time(seconds: float):
    """For the log lines: the time of a log entry in German time (logging.Formatter.converter)."""
    return datetime.fromtimestamp(seconds, GERMANY).timetuple()
