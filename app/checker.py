"""Checks links for jobs and saves the ones we haven't seen before."""
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from . import ai, arbeitsagentur, db, gmail, telegram
from .config import CONFIG
from .dedup import dedup_key
from .filters import get_filter
from .sources import CheckResult, check_url

log = logging.getLogger("jobradar")

_running: set[int] = set()
_running_lock = threading.Lock()


def min_score() -> int | None:
    """The Gemini threshold, or None when Gemini is off (then scores are ignored)."""
    return CONFIG["gemini"]["min_score"] if ai.enabled() else None


def hide_old_duplicates() -> None:
    found = db.mark_duplicates()
    if found:
        log.info("Hid %d duplicate job(s)", found)


def refilter_all_jobs() -> None:
    """Run the keyword filter again on every saved job, so edits to config.yaml
    also apply to jobs found earlier."""
    job_filter = get_filter()
    jobs = db.all_jobs_for_filter()
    results = []
    for j in jobs:
        verdict = job_filter.check(j["title"], j["employment"] or "", j["description"] or "")
        results.append((j["id"], verdict.passed, verdict.reason, verdict.hours))
    db.save_filter_results(results)
    if jobs:
        log.info("Keyword filter: %d of %d saved jobs match", sum(r[1] for r in results), len(jobs))


def ensure_builtin_sources() -> None:
    if CONFIG["arbeitsagentur"]["enabled"] and not db.find_link_by_url(arbeitsagentur.LINK_URL):
        db.add_link(arbeitsagentur.LINK_URL, kind="arbeitsagentur", name=arbeitsagentur.NAME)
    if CONFIG["emails"]["enabled"] and not db.find_link_by_url(gmail.LINK_URL):
        db.add_link(gmail.LINK_URL, kind="gmail", name=gmail.NAME)


def builtin_off(link: dict) -> bool:
    """A built-in source that is turned off in config.yaml."""
    return ((link["kind"] == "arbeitsagentur" and not CONFIG["arbeitsagentur"]["enabled"])
            or (link["kind"] == "gmail" and not CONFIG["emails"]["enabled"]))


def _run(link: dict) -> CheckResult:
    if link["kind"] == "arbeitsagentur":
        return arbeitsagentur.check()
    if link["kind"] == "gmail":
        return gmail.check()
    return check_url(link["url"])


class AlreadyRunning(Exception):
    pass


def check_link(link_id: int) -> dict:
    """Check one link now. Returns {"link": ..., "new_jobs": n}."""
    with _running_lock:
        if link_id in _running:
            raise AlreadyRunning()
        _running.add(link_id)
    try:
        link = db.get_link(link_id)
        result = _run(link)
        new = []
        if result.status == "green":
            if not link["name"]:
                link["name"] = result.name
            job_filter = get_filter()
            jobs = []
            for j in result.jobs:
                if not j.url:
                    continue
                verdict = job_filter.check(j.title, j.employment, j.description)
                jobs.append({**asdict(j), "dedup_key": dedup_key(j.title, j.company), "hours": verdict.hours,
                             "filter_passed": verdict.passed, "filter_reason": verdict.reason})
            new = db.insert_new_jobs(link, jobs, baseline=not link["baseline_done"])
            db.remember_emails(result.read_emails)  # only after their jobs are saved
        matching = sum(1 for j in new if j["filter_passed"])
        db.save_check_result(link_id, result.status, result.reason, result.method, result.name,
                             len(result.jobs), len(new), matching)
        log.info("Checked %s: %s, %d new job(s), %d match your filter",
                 link["name"] or link["url"], result.status, len(new), matching)
        if matching:
            if ai.enabled():
                ai.score_in_background()  # sends Telegram messages once the jobs are scored
            else:
                telegram.notify_in_background()
        return {"link": db.get_link(link_id), "new_jobs": len(new), "new_matching": matching}
    finally:
        with _running_lock:
            _running.discard(link_id)


_checking_all = threading.Event()  # set while "Check now" (all sources) is running


def checking_all() -> bool:
    return _checking_all.is_set()


def check_all_in_background() -> bool:
    """The dashboard's "Check now": check every active link right away, not only the due ones.
    Returns False if such a check is already running."""
    with _running_lock:
        if _checking_all.is_set():
            return False
        _checking_all.set()

    def run():
        try:
            check_due_links(only_due=False)
        finally:
            _checking_all.clear()

    threading.Thread(target=run, daemon=True, name="check-all").start()
    return True


def check_due_links(only_due: bool = True) -> None:
    """Check every active link whose last check is at least one interval ago
    (or every active link, if only_due is False)."""
    interval = timedelta(minutes=CONFIG["check_interval_minutes"])
    for link in db.list_links():
        if not link["active"] or builtin_off(link):
            continue
        last = link["last_checked"]
        # one minute of slack, so a link checked at 10:00:05 is due again at the 11:00 run
        if only_due and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < interval - timedelta(minutes=1):
            continue
        try:
            check_link(link["id"])
        except AlreadyRunning:
            pass
        except Exception:
            log.exception("Checking %s failed", link["url"])
    # Also picks up jobs left over from a paused/failed run.
    if ai.enabled():
        ai.score_in_background()
    else:
        telegram.notify_in_background()
