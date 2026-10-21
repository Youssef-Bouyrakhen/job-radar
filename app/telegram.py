"""Telegram messages: one short message for every good new job.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
"""
import html
import json
import logging
import os
import threading
import time
from datetime import date, datetime

import httpx

from . import ai, db, must_apply
from .config import CONFIG

log = logging.getLogger("jobradar")

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def enabled() -> bool:
    return configured() and CONFIG["telegram"].get("enabled", True) and not paused()


def paused() -> bool:
    """Paused with the switch in the dashboard (config.yaml can still turn it off for good)."""
    return db.get_setting("telegram_paused") == "1"


def set_paused(value: bool) -> None:
    db.set_setting("telegram_paused", "1" if value else "0")


def send(text: str) -> None:
    """Send one message. Raises TelegramError with a readable reason if it fails."""
    if not configured():
        raise TelegramError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in .env")
    try:
        r = httpx.post(API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method="sendMessage"),
                       json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
                             "link_preview_options": {"is_disabled": True}},
                       timeout=20)
    except httpx.HTTPError as e:
        raise TelegramError(f"Could not reach Telegram ({type(e).__name__})") from e
    data = r.json()
    if data.get("ok"):
        return
    reason = data.get("description", f"HTTP {r.status_code}")
    hints = {
        401: "the bot token is wrong",
        400: "check TELEGRAM_CHAT_ID, and that you sent /start to your bot",
        403: "the bot is blocked. Open the chat with your bot and press Start / Unblock",
        429: "too many messages, Telegram asks us to slow down",
    }
    hint = hints.get(r.status_code)
    raise TelegramError(f"{reason} ({hint})" if hint else reason)


# ---- message text --------------------------------------------------------------

def _parse(value: str) -> datetime | date:
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value).astimezone()  # local time


def posted_label(value: str) -> str:
    when = _parse(value)
    if isinstance(when, datetime):
        hours = (datetime.now().astimezone() - when).total_seconds() / 3600
        if hours < 1:
            return "posted just now"
        if hours < 24:
            return f"posted {int(hours)} h ago"
        when = when.date()
    days = (date.today() - when).days
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    if days < 30:
        return f"posted {days} days ago"
    year = "" if when.year == date.today().year else f" {when.year}"  # "13 Oct 2025" for old ads
    return f"posted on {when.day} {when:%b}{year}"


def job_message(job: dict) -> str:
    e = lambda s: html.escape(s or "")
    lines = []
    if job.get("ai_score") is not None:
        lines.append(f"<b>{job['ai_score']}/10</b> · <a href=\"{e(job['url'])}\"><b>{e(job['title'])}</b></a>")
    else:
        lines.append(f"<a href=\"{e(job['url'])}\"><b>{e(job['title'])}</b></a>")
    where = " · ".join(filter(None, [job.get("company"), job.get("location") or "Location unknown"]))
    if where:
        lines.append(f"🏢 {e(where)}")
    if job.get("hours"):
        lines.append(f"⏱ {e(job['hours'])}")
    if job.get("posted_at"):
        lines.append(f"📅 {posted_label(job['posted_at'])}")
    else:
        found = _parse(job["first_seen"])
        lines.append(f"🔎 first seen {found:%d %b} (the site doesn't show a posting date)")
    if job.get("ai_reason"):
        lines.append(f"✦ <i>{e(job['ai_reason'])}</i>")
    lines.append(f"<a href=\"{e(job['url'])}\">Open job</a> · via {e(job.get('source'))}")
    return "\n".join(lines)


# ---- sending the pending notifications --------------------------------------

_lock = threading.Lock()


def min_score() -> int | None:
    return CONFIG["gemini"]["min_score"] if ai.enabled() else None


def notify_pending() -> int:
    """Send a message for every good new job that hasn't been sent yet. Returns how many."""
    if not enabled() or not _lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        must_apply.update_areas()  # jobs outside your area are never sent
        for job in db.jobs_to_notify(min_score(), ai.enabled(), CONFIG["telegram"]["max_messages_per_run"]):
            try:
                send(job_message(job))
            except TelegramError as e:
                if any(w in str(e).lower() for w in ("can't parse", "too long")):
                    db.mark_notified([job["id"]], failed=True)  # this one message is broken, skip it
                    log.warning("Telegram: could not send '%s': %s", job["title"][:60], e)
                    continue
                log.warning("Telegram: %s. Will try again later.", e)
                break
            db.mark_notified([job["id"]])
            sent += 1
            time.sleep(1.1)  # Telegram allows about 1 message per second per chat
    finally:
        _lock.release()
    if sent:
        log.info("Telegram: sent %d job(s)", sent)
    return sent


def must_message(job: dict, reminder: bool = False) -> str:
    head = "⏰ <b>Reminder · 🔥 MUST APPLY</b> (not applied yet)" if reminder else "🔥 <b>MUST APPLY</b>"
    met = json.loads(job.get("must_reasons") or "[]")
    return "\n".join([head, job_message(job)] + [f"✓ {html.escape(m)}" for m in met])


def notify_must() -> int:
    """🔥 MUST APPLY right away for jobs that just went into the box, and one reminder after
    must_apply.reminder_hours if you haven't applied or hidden them. Returns how many messages."""
    if not enabled() or not _lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        hours = CONFIG["must_apply"]["reminder_hours"]
        # Jobs that came in while Telegram was off don't arrive late (the reminder would follow at once)
        todo = [(j, False) for j in db.must_to_send(hours)] + [(j, True) for j in db.must_to_remind(hours)]
        for job, reminder in todo:
            try:
                send(must_message(job, reminder))
            except TelegramError as e:
                log.warning("Telegram: must-apply message for '%s' failed: %s", job["title"][:60], e)
                break
            db.mark_must_sent(job["id"], reminder)
            sent += 1
            time.sleep(1.1)
    finally:
        _lock.release()
    if sent:
        log.info("Telegram: sent %d must-apply message(s)", sent)
    return sent


def notify_in_background() -> None:
    if enabled():
        threading.Thread(target=notify_pending, daemon=True, name="telegram").start()
