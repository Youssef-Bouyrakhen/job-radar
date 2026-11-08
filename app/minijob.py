"""Minijob section: 100% home office Minijobs anywhere in Germany, any field.

Same flow as the Werkstudent jobs, but completely separate (own table `minijobs`, own dashboard page, own Telegram
channel, own AI budget share): collect -> free filter (minijob_filter.py) -> AI score -> dashboard -> Telegram.

Sources (job boards only): the Arbeitsagentur API (Minijob filter arbeitszeit=mj + home office words),
Arbeitnow's free public API (remote jobs; only those that say Minijob) and Remotely (two list pages once a day,
see minijob_sources.py) and job-alert emails from your own Gmail label (flagged "unverified": only the title is known).
"""
import html
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import ai, arbeitsagentur, clock, db, dedup, minijob_sources, telegram
from .config import CONFIG
from .extractors import clean, html_to_text, to_iso
from .fetcher import fetch_api
from .minijob_filter import MIXED_TYPE, Verdict, check as filter_check, check_unverified

log = logging.getLogger("jobradar")

ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api"
SOURCE_BA = "Arbeitsagentur"
SOURCE_AN = "Arbeitnow"
SOURCE_RM = minijob_sources.SOURCE_RM
SOURCE_GM = minijob_sources.SOURCE_GM
QUALIFICATION_MAX_SCORE = 4   # the ad requires a formal qualification or licence (nurse, tax clerk, payroll ...)
MIXED_MAX_SCORE = 6        # offered as "Vollzeit/Teilzeit/Minijob" together: never above 6
MAX_AGE_DAYS = {"Remotely": 30}   # a source whose page shows a date: older ads are skipped
UNVERIFIED_MAX_SCORE = 7   # an ad from an alert email was never checked for Minijob / home office: never above 7
_lock = threading.Lock()  # one collect + score + notify round at a time


def cfg() -> dict:
    return CONFIG["minijob"]


def chat_id() -> str | None:
    return os.environ.get("MINIJOB_TELEGRAM_CHAT_ID") or None


# ---- 1. collect ------------------------------------------------------------------------

def collect_arbeitsagentur(days: int, pages: int) -> list[dict]:
    """Minijobs (arbeitszeit=mj) whose search words point to home office, all over Germany (no place given).
    Home office itself is checked by the filter, from the ad text and the ad's own home office percent."""
    found: dict[str, dict] = {}
    for word in cfg()["searches"]:
        for page in range(1, pages + 1):
            params = {"arbeitszeit": "mj", "was": word, "size": 100, "page": page, "veroeffentlichtseit": days}
            offers = fetch_api(arbeitsagentur.API, params=params, headers=arbeitsagentur.HEADERS).get("ergebnisliste", [])
            for o in offers:
                ref = o.get("referenznummer")
                if ref and ref not in found:
                    found[ref] = {**o, "_found_by": word}
            if len(offers) < 100:
                break
    jobs = []
    for ref, o in found.items():
        places = o.get("stellenlokationen") or [{}]
        jobs.append({
            "url": f"{arbeitsagentur.DETAIL_URL}{ref}", "source": SOURCE_BA, "title": clean(o.get("stellenangebotsTitel")),
            "company": clean(o.get("firma")), "location": (places[0].get("adresse") or {}).get("ort", ""),
            "posted_at": to_iso(o.get("datumErsteVeroeffentlichung")), "description": "",
            "flag_minijob": bool(o.get("istGeringfuegigeBeschaeftigung")),
            "home_percent": o.get("homeofficeprozent"), "found_by": o["_found_by"],
        })
    return jobs


def collect_arbeitnow(days: int, pages: int = 2) -> list[dict]:
    """Arbeitnow's free API: remote jobs only (its own flag). Whether it is a Minijob the filter reads from the text."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    jobs = []
    for page in range(1, pages + 1):
        data = fetch_api(ARBEITNOW, params={"page": page}).get("data", [])
        for o in data:
            created = datetime.fromtimestamp(o.get("created_at") or 0, timezone.utc)
            if not o.get("remote") or created < since:
                continue
            jobs.append({
                "url": o["url"], "source": SOURCE_AN, "title": clean(html.unescape(o.get("title", ""))),
                "company": clean(o.get("company_name")), "location": clean(o.get("location")),
                "posted_at": created.isoformat(timespec="seconds"),
                "description": html_to_text(html.unescape(o.get("description", "")), 6000),
                "flag_minijob": False, "home_percent": 100, "remote_flag": True,
            })
        if not data:
            break
    return jobs


# ---- 2. filter + save ---------------------------------------------------------------------

def known_urls() -> set[str]:
    with db.connect() as conn:
        return {r["url"] for r in conn.execute("SELECT url FROM minijobs")}


def _kept_for_dedup() -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT title, company, location FROM minijobs WHERE passed = 1 AND first_seen >= ?", (since,))]


def _age_days(iso: str | None) -> int | None:
    """Age of an ad in days from its ISO date ("" or unreadable: None, then the ad is not skipped)."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - d).days)


def is_mixed(job: dict) -> bool:
    """Offered as full time AND Minijob in one ad (the page's facts line or the title says both)."""
    return bool(job.get("mixed_type") or MIXED_TYPE.search(job["title"]))


def evaluate(job: dict):
    """The filter's Verdict (passed, reason, code, words) for one collected ad."""
    if job.get("unverified"):
        return check_unverified(job["title"])
    age = _age_days(job.get("posted_at"))
    if age is not None and age > MAX_AGE_DAYS.get(job["source"], 10**6):
        return Verdict(False, f"Older than {MAX_AGE_DAYS[job['source']]} days ({age} days)", "old", f"{age} days old")
    percent = job.get("home_percent")
    v = filter_check(
        job["title"], job["description"], flag_minijob=job.get("flag_minijob", False),
        flag_remote_100=percent == 100 or bool(job.get("remote_flag")),
        flag_not_full=percent is not None and percent < 100 and not job.get("remote_flag"),
        max_monthly=cfg()["max_monthly_pay"])
    return v


def process(candidates: list[dict]) -> dict:
    """Read the full ad of new Arbeitsagentur jobs, run the free filter, save everything. Returns counts."""
    seen = known_urls()
    new, reads = [], {}  # at most max_new_per_run ads per source are read in full per round (one request each)
    for j in candidates:
        if j["url"] in seen:
            continue
        if j["source"] in (SOURCE_BA, SOURCE_RM):
            reads[j["source"]] = reads.get(j["source"], 0) + 1
            if reads[j["source"]] > cfg()["max_new_per_run"]:
                continue
        new.append(j)
    kept, counts = _kept_for_dedup(), {"new": len(new), "passed": 0, "dropped": 0, "duplicate": 0}
    for job in new:
        if job["source"] == SOURCE_BA:
            try:
                job["description"] = arbeitsagentur.fetch_description(job["url"])
            except Exception as e:  # one broken ad does not stop the rest; it is read again next time
                log.warning("Minijob: could not read %s (%s)", job["url"], type(e).__name__)
                continue
        elif job["source"] == SOURCE_RM:
            try:
                job.update({k: v for k, v in minijob_sources.read_ad(job["url"]).items() if v and k != "facts"})
            except Exception as e:
                log.warning("Minijob: could not read %s (%s)", job["url"], type(e).__name__)
                continue
        verdict = evaluate(job)
        passed, reason = verdict.passed, verdict.reason
        if passed and dedup.find_duplicate(job, kept):
            passed, reason = False, "Duplicate (same job from another source)"
            counts["duplicate"] += 1
        if passed:
            kept.append({k: job[k] for k in ("title", "company", "location")})
            counts["passed"] += 1
        else:
            counts["dropped"] += 1
        with db.connect() as conn:
            conn.execute("""INSERT OR IGNORE INTO minijobs (url, source, title, company, location, description, posted_at,
                            first_seen, passed, filter_reason, unverified, max_score)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (job["url"], job["source"], job["title"], job["company"], job["location"],
                          job["description"][:8000], job["posted_at"], db.now(), int(passed), reason,
                          int(bool(job.get("unverified"))), MIXED_MAX_SCORE if is_mixed(job) else None))
    return counts


# ---- 3. AI score ---------------------------------------------------------------------------

SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "INTEGER"}, "score": {"type": "INTEGER"}, "reason": {"type": "STRING"},
    "customer_calls": {"type": "BOOLEAN"}, "not_remote": {"type": "BOOLEAN"}, "not_minijob": {"type": "BOOLEAN"},
    "needs_qualification": {"type": "BOOLEAN"}},
    "required": ["id", "score", "reason", "customer_calls", "not_remote", "not_minijob", "needs_qualification"]}}

INSTRUCTIONS = """You rate Minijob ads (max. 603 EUR a month) for one person who wants a Minijob that is 100% home office
anywhere in Germany, any field. Be strict and short. The ads already passed a keyword filter.

SCORE (whole number 1-10)
9-10  A real Minijob, clearly 100% home office / remote, no phone work, a serious employer with a real company name.
7-8   Good, with a small doubt (vague tasks, unknown company, unclear hours).
4-6   Doubtful: unclear if it is really home office, or the company looks thin, or the tasks are vague.
1-3   Any of these: not really a Minijob; home office only partly or "possible"; customer calls / phone work are part
      of the duties; looks like a scam, pyramid or reselling scheme, or asks for money or a private bank account.

CHECKS (true/false)
- customer_calls: calling or answering customers / leads by phone is part of the duties (a phone number as a contact
  line is NOT customer calls).
- not_remote: the work is not fully from home (office days, on-site, field work, shop).
- not_minijob: it is clearly not a Minijob (full-time, internship, freelance with high pay).
- needs_qualification: the ad REQUIRES a formal qualification or licence that the person does not have (completed
  training or degree in a trade, e.g. Pflegefachkraft, Steuerfachangestellte/r, Steuerfachwirt, Bilanzbuchhalter,
  Lohnbuchhalter / DATEV payroll, Erzieher, Handwerker, "abgeschlossene Ausbildung als ..."). The person is a computer
  science student with general office skills, NOT a trained nurse, accountant or tax clerk. "Vorteilhaft" / "von Vorteil" /
  "gerne" is NOT a requirement; a normal school leaving certificate or "PC skills" is NOT a qualification.

For EVERY job return: id, score, reason (ONE short English sentence, max 20 words), customer_calls, not_remote, not_minijob,
needs_qualification.

UNVERIFIED ads (they have a "note"): they come from a job-alert email, so only the title, company and place are known.
Judge by title and company only. Do not guess home office or Minijob status; do not mark not_remote / not_minijob just
because the text is missing. Give such an ad at most 7.

JOBS (JSON)
{jobs}
"""


def score_pending() -> int:
    """Score passed ads that have no score yet. Uses only the Minijob share of the free AI (ai.py, purpose "minijob").
    Returns how many were scored. When the share or all providers are used up the ads wait for the next round."""
    if not ai.enabled() or not ai.next_provider("minijob"):
        return 0
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, title, company, location, source, description, unverified, max_score FROM minijobs
               WHERE passed = 1 AND ai_score IS NULL AND ai_attempts < 3 ORDER BY first_seen DESC LIMIT 40""")]
    scored = 0
    for i in range(0, len(rows), cfg()["batch_size"]):
        batch = rows[i:i + cfg()["batch_size"]]
        payload = [{"id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"] or "unknown",
                    "source": j["source"], "description": (j["description"] or "")[:2500],
                    **({"note": "UNVERIFIED alert email: only title and company are known"} if j["unverified"] else {})}
                   for j in batch]
        try:
            answers = ai.ask_json(INSTRUCTIONS.format(jobs=json.dumps(payload, ensure_ascii=False)), SCHEMA, 0.2,
                                  purpose="minijob")
        except (ai.RateLimited, ai.Overloaded) as e:  # includes BudgetSaved: the Minijob share is used up for today
            log.info("Minijob AI: stopped for now (%s)", str(e)[:100])
            break
        except Exception as e:
            _count_attempt([j["id"] for j in batch])
            log.warning("Minijob AI scoring failed: %s", e)
            break
        ids, unverified = {j["id"] for j in batch}, {j["id"] for j in batch if j["unverified"]}
        caps = {j["id"]: j["max_score"] for j in batch if j["max_score"]}
        done = set()
        with db.connect() as conn:
            for a in answers:
                if a.get("id") not in ids:
                    continue
                score = max(1, min(10, int(a.get("score", 1))))
                if a["id"] in caps:
                    score = min(score, caps[a["id"]])
                if a["id"] in unverified:
                    score = min(score, UNVERIFIED_MAX_SCORE)
                if a.get("needs_qualification"):
                    score = min(score, QUALIFICATION_MAX_SCORE)   # needs a licence / training the user does not have
                if a.get("customer_calls"):
                    score = min(score, 4)   # customer calls: the user does not want phone work
                if a.get("not_remote") or a.get("not_minijob"):
                    score = min(score, 3)
                conn.execute("UPDATE minijobs SET ai_score = ?, ai_reason = ? WHERE id = ?",
                             (score, (a.get("reason") or "").strip()[:300], a["id"]))
                done.add(a["id"])
        _count_attempt(list(ids - done))
        scored += len(done)
    if scored:
        log.info("Minijob AI scored %d ad(s)", scored)
    return scored


def _count_attempt(ids: list[int]) -> None:
    with db.connect() as conn:
        conn.executemany("UPDATE minijobs SET ai_attempts = ai_attempts + 1 WHERE id = ?", [(i,) for i in ids])


# ---- 4. Telegram -----------------------------------------------------------------------------

def message(job: dict) -> str:
    e = lambda s: html.escape(s or "")
    head = f"<b>{job['ai_score']}/10</b> · " if job.get("ai_score") is not None else ""
    lines = [f"💼 {head}<a href=\"{e(job['url'])}\"><b>{e(job['title'])}</b></a>",
             "🏢 " + e(" · ".join(filter(None, [job.get("company"), job.get("location")])) or "Company unknown"),
             "⚠️ From an alert email: check the ad (Minijob and home office not verified)" if job.get("unverified")
             else "🏠 100% home office Minijob"]
    if job.get("ai_reason"):
        lines.append(f"✦ <i>{e(job['ai_reason'])}</i>")
    lines.append(f"<a href=\"{e(job['url'])}\">Open job</a> · via {e(job['source'])}")
    return "\n".join(lines)


def notify_pending(limit: int | None = None) -> int:
    """Send good scored ads (score >= minijob.min_score) to the Minijob channel. Returns how many."""
    # needs the bot token and MINIJOB_TELEGRAM_CHAT_ID; the dashboard's Telegram switch and config.yaml apply too
    if not (chat_id() and os.environ.get("TELEGRAM_BOT_TOKEN") and CONFIG["telegram"].get("enabled", True)
            and not telegram.paused()):
        return 0
    ai_on = ai.enabled()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT * FROM minijobs WHERE passed = 1 AND notified = 0
                AND {"ai_score >= ?" if ai_on else "? IS NOT NULL"} ORDER BY ai_score DESC, first_seen DESC LIMIT ?""",
            (cfg()["min_score"] if ai_on else 0, limit or cfg()["max_messages_per_run"]))]
    sent = 0
    for job in rows:
        try:
            telegram.send(message(job), chat_id())
            state = 1
        except telegram.TelegramError as err:
            if any(w in str(err).lower() for w in ("can't parse", "too long")):
                state = 2
            else:
                log.warning("Minijob Telegram: %s. Will try again later.", err)
                break
        with db.connect() as conn:
            conn.execute("UPDATE minijobs SET notified = ? WHERE id = ?", (state, job["id"]))
        sent += state == 1
        time.sleep(1.1)
    if sent:
        log.info("Minijob Telegram: sent %d ad(s)", sent)
    return sent


# ---- the scheduled round ------------------------------------------------------------------------

def run(deep: bool = False, notify: bool = True) -> dict:
    """One round: collect, filter, score, send. deep (night run): 30 days back and more pages.
    Returns counts; {"skipped": ...} if another round is still running."""
    if not cfg().get("enabled", True):
        return {"skipped": "off"}
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        days, pages = (30 if deep else cfg()["days_back"]), cfg()["pages"]
        candidates, errors = [], []
        for name, reader in ((SOURCE_BA, lambda: collect_arbeitsagentur(days, pages)),
                             (SOURCE_AN, lambda: collect_arbeitnow(days) if cfg()["arbeitnow"] else []),
                             # Remotely: only the night run, so the site is visited once a day
                             (SOURCE_RM, lambda: minijob_sources.list_ads() if deep and cfg().get("remotely") else [])):
            try:
                candidates += reader()
            except Exception as e:  # one source down does not stop the other
                errors.append(f"{name}: {type(e).__name__}")
                log.warning("Minijob: %s could not be read (%s)", name, e)
        gmail_ads, gmail_read = [], []
        try:   # job-alert emails from your own label (hourly; read-only)
            gmail_ads, gmail_read = minijob_sources.collect_gmail()
            candidates += gmail_ads
        except Exception as e:
            errors.append(f"Gmail: {type(e).__name__}")
            log.warning("Minijob: Gmail could not be read (%s)", e)
        counts = process(candidates)
        minijob_sources.remember_gmail(gmail_read)  # only after the ads are saved
        counts["candidates"] = len(candidates)
        counts["scored"] = score_pending()
        counts["sent"] = notify_pending() if notify else 0
        counts["errors"] = errors
        log.info("Minijob round: %s", counts)
        return counts
    finally:
        _lock.release()


def scheduled() -> None:
    run(deep=False)


def scheduled_night() -> None:
    run(deep=True)


# ---- for the dashboard ----------------------------------------------------------------------------

def listing(limit: int = 300) -> dict:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, url, source, title, company, location, posted_at, first_seen, ai_score, ai_reason, unverified
               FROM minijobs WHERE passed = 1
               ORDER BY COALESCE(ai_score, 0) DESC, first_seen DESC LIMIT ?""", (limit,))]
        total, dropped = (conn.execute("SELECT SUM(passed), COUNT(*) - SUM(passed) FROM minijobs").fetchone())
    return {"jobs": rows, "total_passed": total or 0, "dropped": dropped or 0, "min_score": cfg()["min_score"],
            "telegram": bool(chat_id()), "ai": ai.enabled()}
