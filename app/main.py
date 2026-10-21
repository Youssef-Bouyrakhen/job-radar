"""The web server: serves the dashboard and a small API it talks to."""
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (VERSION, ai, applications, categories, companies, company_jobs, db, must_apply, discover, gmail, region,
               scheduler, telegram, weekly)
from .checker import (AlreadyRunning, check_all_in_background, check_link, checking_all, recheck_hours,
                      ensure_builtin_sources, hide_old_duplicates, min_score, recheck_no_jobs, refilter_all_jobs)
from .config import CONFIG, ROOT

load_dotenv(ROOT / ".env")  # before anything reads GEMINI_API_KEY etc.
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M")
for noisy in ("httpx", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    ensure_builtin_sources()
    recheck_hours()  # once per hours rule: hours read again (per month -> per week), the changes logged
    refilter_all_jobs()
    hide_old_duplicates()
    must_apply.prepare()  # once: recent good jobs are scored again with the blocker check
    region.skip_staffing()  # staffing agencies with a careers page stay Skipped (some were Watched in v9.9)
    categories.apply_rules()  # no AI: every company the name / jobs make clear gets its category right away
    ai.requeue_unscored()  # once: jobs the AI gave up on wait for a score again
    company_jobs.release_first_reads()  # once: good jobs from companies' first reads can go to Telegram
    logging.getLogger("jobradar").info(
        "AI scoring: %s", f"on ({' -> '.join(ai.providers())}, min. score {CONFIG['gemini']['min_score']})"
        if ai.enabled() else "off (no GEMINI_API_KEY / GROQ_API_KEY / CLOUDFLARE_API_TOKEN / OPENROUTER_API_KEY in .env)")
    logging.getLogger("jobradar").info(
        "Telegram: %s", "on" if telegram.enabled() else "off (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env)")
    logging.getLogger("jobradar").info(
        "Job-alert emails: %s", f"on (Gmail label \"{gmail.label()}\", read-only)" if gmail.configured()
        else "off (GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env)")
    logging.getLogger("jobradar").info(
        "Application emails: %s", f"on (Gmail label \"{applications.settings()['label']}\", read-only)"
        if applications.enabled() else "off")
    # JOBRADAR_NO_SCHEDULER=1: a test copy that never checks sources or sends alerts by itself
    background = os.environ.get("JOBRADAR_NO_SCHEDULER") != "1"
    if background:
        scheduler.start()
        threading.Thread(target=recheck_no_jobs, daemon=True, name="recheck-no-jobs").start()  # once per rule change
        # once per area / hours rule change: places and hours read again, the Must apply box checked again
        threading.Thread(target=must_apply.recheck_jobs, daemon=True, name="recheck-jobs").start()
        if companies.recheck_due():  # once per finder change: the "No careers page" companies (shown on the Companies page)
            companies.start("recheck")
    yield
    if background:
        scheduler.stop()


app = FastAPI(title="Job Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def no_cache(request: Request, call_next):
    # The browser must always load the newest page files, never a cached old app.js
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


class NewLink(BaseModel):
    url: str


class LinkUpdate(BaseModel):
    active: bool


def _normalize(url: str) -> str:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _check(link_id: int) -> dict:
    try:
        return check_link(link_id)
    except AlreadyRunning:
        raise HTTPException(409, "This link is being checked right now. Try again in a moment.")


def _stamp(match: re.Match) -> str:
    # /static/app.js -> /static/app.js?v=<time the file was last saved>
    path = match.group(1)
    return f'"{path}?v={int((ROOT / path.lstrip("/")).stat().st_mtime)}"'


@app.get("/")
def dashboard():
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(re.sub(r'"(/static/[\w.-]+)"', _stamp, page))


@app.get("/api/info")
def info():
    due = db.next_due(CONFIG["check_interval_minutes"])
    next_check = scheduler.next_check_time(datetime.fromisoformat(due) if due else None)
    return {
        "version": VERSION,
        "check_interval_minutes": CONFIG["check_interval_minutes"],
        "last_check": db.last_check(),
        "next_check": next_check.isoformat() if next_check else None,
        "checking": checking_all(),
        "telegram_enabled": telegram.enabled(),
        "ai_enabled": ai.enabled(),
        "ai": ai.provider_status() if ai.enabled() else None,  # the AI usage bar at the top
        "min_score": CONFIG["gemini"]["min_score"],
        "must": {**{k: CONFIG["must_apply"][k] for k in ("min_score", "max_days")},  # older Must apply jobs turn red
                 "center": CONFIG["area"]["center"]},
        **db.count_jobs(min_score()),
    }


@app.post("/api/check")
def check_all():
    """"Check now" in the dashboard: checks every active source in the background."""
    check_all_in_background()  # if one is already running, just report on that one
    applications.check_in_background()
    return info()


@app.get("/api/settings")
def settings():
    g = CONFIG["gemini"]
    return {
        "check_interval_minutes": CONFIG["check_interval_minutes"],
        "telegram_configured": telegram.configured(),
        "telegram_enabled": telegram.enabled(),
        "telegram_off_in_config": not CONFIG["telegram"].get("enabled", True),
        "gmail_label": gmail.label(),
        "applications": "Turned off" if not CONFIG["applications"]["enabled"] else
                        f"Label \"{applications.settings()['label']}\" (read-only, emails stay unread), checked every "
                        f"{CONFIG['applications']['check_every_minutes']} min" if gmail.configured()
                        else "Not set up: GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env",
        "applications_label": applications.settings()["label"],
        "ai_enabled": ai.enabled(),
        "ai_key_present": bool(ai.providers()),
        "ai_model": g["model"],
        "ai_provider": ai.provider_status(),
        "min_score": g["min_score"],
        "profile": g["profile"].strip(),
        "arbeitsagentur_searches": CONFIG["arbeitsagentur"]["searches"] if CONFIG["arbeitsagentur"]["enabled"] else [],
        "emails": "Turned off" if not CONFIG["emails"]["enabled"] else
                  f"Label \"{gmail.label()}\" in {gmail.credentials()[0]} (read-only, emails stay unread)"
                  if gmail.configured() else "Not set up: GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env",
    }


class TelegramSwitch(BaseModel):
    on: bool


@app.post("/api/telegram/switch")
def telegram_switch(body: TelegramSwitch):
    if body.on and not telegram.configured():
        raise HTTPException(400, "Telegram is not set up: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
    if body.on and not CONFIG["telegram"].get("enabled", True):
        raise HTTPException(400, "Telegram is turned off in config.yaml (telegram: enabled: false)")
    telegram.set_paused(not body.on)
    if body.on:
        telegram.notify_in_background()
    return {"telegram_enabled": telegram.enabled()}


@app.post("/api/telegram/test")
def telegram_test():
    example = db.latest_good_job(min_score())
    text = "✅ <b>Job Radar test message</b>\nTelegram works! New jobs will look like this:"
    if example:
        text += "\n\n" + telegram.job_message(example)
    try:
        telegram.send(text)
    except telegram.TelegramError as e:
        raise HTTPException(400, f"Telegram said: {e}")
    return {"ok": True}


# ---- links -------------------------------------------------------------------
# Plain "def" (not "async def"): FastAPI runs these in a background thread,
# so a slow website never freezes the dashboard.

@app.get("/api/links")
def list_links():
    return db.list_links(min_score())


@app.post("/api/links")
def add_link(body: NewLink):
    url = _normalize(body.url)
    if "." not in url:
        raise HTTPException(400, "That doesn't look like a web link.")
    if db.find_link_by_url(url):
        raise HTTPException(409, "This link is already on your list.")
    return _check(db.add_link(url))


@app.post("/api/links/{link_id}/check")
def check_now(link_id: int):
    if not db.get_link(link_id):
        raise HTTPException(404, "Link not found.")
    return _check(link_id)


@app.patch("/api/links/{link_id}")
def update_link(link_id: int, body: LinkUpdate):
    db.set_link_active(link_id, body.active)
    return {"ok": True}


@app.delete("/api/links/{link_id}")
def delete_link(link_id: int):
    link = db.get_link(link_id)
    if link and link["kind"] != "url":
        raise HTTPException(400, "Built-in sources can't be deleted. Pause it, or turn it off in config.yaml.")
    db.delete_link(link_id)
    return {"ok": True}


# ---- companies (Company Discovery Engine) ------------------------------------------

COMPANY_STATUSES = ("new", "watched", "initiativ", "no_career_page", "skipped")


class CompanyUpdate(BaseModel):
    status: str | None = None
    category: str | None = None


@app.get("/api/companies")
def list_companies():
    rows = db.list_companies()
    for r in rows:
        r["sources"] = json.loads(r["sources"] or "{}")
        r["size"] = company_jobs.size_of(r)
    company_jobs.next_reads(rows)  # "next: today 19:00"
    return {"progress": companies.progress(), "companies": rows}


@app.get("/api/coverage")
def coverage():
    """Region coverage: per town how many companies, careers pages, and how many have a final status."""
    return {**region.coverage(), "progress": companies.progress()}


@app.post("/api/companies/run")
def run_companies(until_done: bool = False, kind: str = "run"):
    # until_done: no morning stop (for a run started by hand at night); a run already going is just reported on
    # kind=recheck: look again for the careers pages of all "No careers page" companies
    # kind=read: read the jobs of every company never read (AI pages beyond the per-run limit wait for the night)
    companies.start(kind if kind in ("recheck", "read") else "run", night_limit=not until_done)
    return {"progress": companies.progress()}


class CompanyImport(BaseModel):
    text: str    # company names (one per line) or the link of a page that lists companies
    label: str   # where they come from, e.g. "konaktiva 2026"


@app.post("/api/companies/import")
def import_companies(body: CompanyImport):
    if not body.text.strip():
        raise HTTPException(400, "Paste company names (one per line) or the link of a page that lists companies.")
    if not body.label.strip():
        raise HTTPException(400, "Give the list a name, e.g. \"konaktiva 2026\".")
    if not companies.start("import", body.text, body.label.strip()):
        raise HTTPException(409, "Discovery is running right now. Try again when it has finished.")
    return {"progress": companies.progress()}


@app.post("/api/companies/lookalikes")
def find_lookalikes():
    if not companies.start("lookalikes"):
        raise HTTPException(409, "Discovery is running right now. Try again when it has finished.")
    return {"progress": companies.progress()}


@app.post("/api/companies/weekly-summary")
def send_weekly_summary():
    """Sends the Sunday summary now (to see what it looks like)."""
    try:
        if not weekly.send_summary(force=True):
            raise HTTPException(400, "Telegram is off or paused.")
    except telegram.TelegramError as e:
        raise HTTPException(400, f"Telegram said: {e}")
    return {"ok": True, "text": weekly.message()}


@app.patch("/api/companies/{company_id}")
def update_company(company_id: int, body: CompanyUpdate):
    if body.status is not None and body.status not in COMPANY_STATUSES:
        raise HTTPException(400, f"Status must be one of: {', '.join(COMPANY_STATUSES)}")
    if body.category is not None and body.category not in categories.LABELS:
        raise HTTPException(400, f"Category must be one of: {', '.join(categories.LABELS)}")
    found = (body.status is None or db.set_company_status(company_id, body.status)) \
        and (body.category is None or db.set_company_category(company_id, body.category))
    if not found:
        raise HTTPException(404, "Company not found.")
    return {"ok": True}


# ---- source suggestions (Phase 8) ------------------------------------------------

@app.get("/api/suggestions")
def list_suggestions():
    return {"progress": discover.progress(), "suggestions": db.list_suggestions()}


@app.post("/api/suggestions/find")
def find_suggestions():
    if not ai.enabled():
        raise HTTPException(400, "AI is off: add GEMINI_API_KEY (or GROQ_API_KEY / OPENROUTER_API_KEY) "
                                 "to .env, or turn it on in config.yaml.")
    discover.start()  # if a search is already running, just report on that one
    return list_suggestions()


def _open_suggestion(suggestion_id: int) -> dict:
    s = db.get_suggestion(suggestion_id)
    if not s or s["status"] != "new":
        raise HTTPException(404, "This suggestion is gone. Reload the page.")
    return s


@app.post("/api/suggestions/{suggestion_id}/add")
def add_suggestion(suggestion_id: int):
    s = _open_suggestion(suggestion_id)
    db.set_suggestion_status(suggestion_id, "added")
    if db.find_link_by_url(s["url"]):
        raise HTTPException(409, "This link is already on your list.")
    return _check(db.add_link(s["url"]))


@app.post("/api/suggestions/{suggestion_id}/skip")
def skip_suggestion(suggestion_id: int):
    _open_suggestion(suggestion_id)
    db.set_suggestion_status(suggestion_id, "skipped")
    return {"ok": True}


# ---- jobs --------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(view: str = "all"):
    if view == "everything":
        return db.list_jobs(view, min_score(), limit=3000)
    if view not in db.VIEWS:
        raise HTTPException(400, f"view must be one of {list(db.VIEWS)}")
    return db.list_jobs(view, min_score())


class JobStatus(BaseModel):
    status: str  # "saved", "applied", "hidden" or "" (undo)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.post("/api/jobs/{job_id}/status")
def set_job_status(job_id: int, body: JobStatus):
    if body.status not in db.STATUSES:
        raise HTTPException(400, f"status must be one of {list(db.STATUSES)}")
    db.set_status(job_id, body.status)
    if body.status == "applied":  # replies that came before you clicked Applied find their job now
        applications.rematch_review()
    return db.count_jobs(min_score())


# ---- application tracking (emails in the "Bewerbungen" label) ------------------------

@app.get("/api/applications")
def application_overview():
    return applications.overview()


@app.post("/api/applications/check")
def check_applications():
    """Read new emails in the label now (takes a few seconds, longer if the AI is needed)."""
    applications.rematch_review()
    result = applications.check()
    if not result.get("ok"):
        raise HTTPException(400, result.get("note") or "Could not read the emails")
    return applications.overview()


class EmailAssign(BaseModel):
    job_id: int | None = None   # None = dismiss (not about one of your applications)
    kind: str | None = None     # confirmation / rejection / interview / test / other


@app.post("/api/applications/emails/{gmail_id}/assign")
def assign_email(gmail_id: str, body: EmailAssign):
    if body.kind is not None and body.kind not in applications.KINDS:
        raise HTTPException(400, f"kind must be one of {list(applications.KINDS)}")
    if not applications.assign(gmail_id, body.job_id, body.kind):
        raise HTTPException(404, "Email not found.")
    return applications.overview()


@app.post("/api/applications/emails/{gmail_id}/unassign")
def unassign_email(gmail_id: str):
    """"Not this job": the email goes back to Needs review."""
    if not applications.unassign(gmail_id):
        raise HTTPException(404, "Email not found.")
    return applications.overview()


@app.post("/api/jobs/{job_id}/seen")
def mark_seen(job_id: int):
    db.mark_seen(job_id)
    return db.count_jobs(min_score())


@app.post("/api/jobs/seen-all")
def mark_all_seen():
    db.mark_seen()
    return db.count_jobs(min_score())


class SeenList(BaseModel):
    ids: list[int]
    seen: bool = True


@app.post("/api/jobs/seen")
def set_seen(body: SeenList):
    db.set_seen(body.ids, body.seen)
    return db.count_jobs(min_score())
