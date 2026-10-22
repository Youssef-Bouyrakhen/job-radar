"""Company Discovery Engine, Phase C: one short Telegram message every Sunday evening.

"X new companies, Y careers pages found, Z good jobs, top 3 companies by fit score,
N new Initiativ candidates" for the last 7 days. No AI.
"""
import html
import logging
from datetime import datetime, timedelta, timezone

from . import ai, clock, db, telegram
from .config import CONFIG

log = logging.getLogger("jobradar")


def message() -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    n = db.week_numbers(since, CONFIG["gemini"]["min_score"] if ai.enabled() else None)
    s = lambda count, word, words: f"{count} {word if count == 1 else words}"
    lines = [
        "<b>Job Radar · your week</b>",
        " · ".join([s(n["new_companies"], "new company", "new companies"),
                    s(n["career_pages"], "careers page found", "careers pages found"),
                    s(n["good_jobs"], "good job", "good jobs"),
                    s(n["initiativ"], "new Initiativ candidate", "new Initiativ candidates")]),
    ]
    if n["top"]:
        lines.append("Top companies: " + " · ".join(
            f"{i}. {html.escape(c['name'])} ({c['fit_score']})" for i, c in enumerate(n["top"], 1)))
    return "\n".join(lines)


def send_summary(force: bool = False) -> bool:
    """Send the summary (once per day at most, unless force). False if Telegram is off."""
    if not telegram.enabled():
        log.info("Weekly summary not sent: Telegram is off or paused")
        return False
    today = clock.today().isoformat()
    if not force and db.get_setting("weekly_summary_sent") == today:
        return False
    telegram.send(message())
    db.set_setting("weekly_summary_sent", today)
    log.info("Telegram: sent the weekly summary")
    return True


def scheduled() -> None:
    try:
        send_summary()
    except telegram.TelegramError as e:
        log.warning("Weekly summary could not be sent: %s", e)
