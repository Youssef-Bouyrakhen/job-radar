"""The SQLite database. It is a single file: data/jobradar.db"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import dedup
from .config import ROOT

DB_PATH = Path(os.environ.get("JOBRADAR_DB") or ROOT / "data" / "jobradar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL UNIQUE,
    name              TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',  -- green / red / pending
    reason            TEXT,                             -- short explanation of the status
    method            TEXT,                             -- how jobs were read, e.g. "Personio feed"
    sample_jobs       TEXT,                             -- (Phase 1, no longer used)
    jobs_in_last_test INTEGER NOT NULL DEFAULT 0,       -- jobs on the page at the last check
    active            INTEGER NOT NULL DEFAULT 1,       -- 0 = paused
    created_at        TEXT NOT NULL,
    last_checked      TEXT,
    last_new_job_at   TEXT,
    total_jobs        INTEGER NOT NULL DEFAULT 0        -- jobs saved from this link
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id     INTEGER,             -- which link found it (empty if the link was deleted)
    source      TEXT,                -- name of that link, kept even after deleting it
    url         TEXT NOT NULL UNIQUE,
    dedup_key   TEXT NOT NULL,       -- simplified "title|company" to catch the same job twice
    title       TEXT NOT NULL,
    company     TEXT,
    location    TEXT,
    description TEXT,
    employment  TEXT,
    posted_at   TEXT,                -- when the job was posted, ONLY if the source says so
    first_seen  TEXT NOT NULL,       -- when Job Radar found it
    seen        INTEGER NOT NULL DEFAULT 0,  -- 1 once you opened it (no more "NEW" badge)
    baseline    INTEGER NOT NULL DEFAULT 0   -- 1 = found in the first check of a new link
);
CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs (dedup_key);

CREATE TABLE IF NOT EXISTS emails (
    gmail_id TEXT PRIMARY KEY,   -- Gmail's id of a job-alert email Job Radar has already read
    read_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    company_key TEXT NOT NULL UNIQUE,  -- simplified name: a skipped company never comes back
    city        TEXT,
    size        TEXT,                  -- big / mid / startup
    focus       TEXT,                  -- Gemini: what IT students do there
    data_count  INTEGER NOT NULL DEFAULT 0,  -- how often it appears in your alerts / Arbeitsagentur jobs
    url         TEXT,                  -- the best careers link that actually loaded
    method      TEXT,                  -- e.g. "Personio feed"
    state       TEXT,                  -- green (good jobs) / amber (jobs, none good) / red (no job list)
    reason      TEXT,
    total_jobs  INTEGER NOT NULL DEFAULT 0,
    good_jobs   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'new',  -- new / added / skipped / no_link (nothing loaded, never shown)
    found_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    name_key      TEXT NOT NULL UNIQUE,  -- simplified name: "ACME GmbH" and "Acme" are one company
    website       TEXT,                  -- homepage that loaded and belongs to the company
    website_from  TEXT,                  -- osm / name / ai
    website_hint  TEXT,                  -- website to try first (not checked yet)
    hint_from     TEXT,                  -- osm (website tag) / name (the name was a web address)
    career_url    TEXT,                  -- careers page that loaded
    ats_type      TEXT,                  -- applicant system, e.g. "Personio"
    feed_url      TEXT,                  -- direct job list / feed of that applicant system (loaded)
    career_state  TEXT,                  -- green (job feed) / amber (careers page) / red (none found)
    city          TEXT,
    distance_km   REAL,                  -- to Darmstadt or Frankfurt, whichever is closer
    lat           REAL,
    lon           REAL,
    sources       TEXT NOT NULL DEFAULT '{}',  -- JSON: where it was found and how often
    size_hint     TEXT,                  -- big / mid (many IT job ads) / staffing agency
    status        TEXT NOT NULL DEFAULT 'new',  -- new / watched / initiativ / no_career_page / skipped
    notes         TEXT,
    first_seen    TEXT NOT NULL,
    last_checked  TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    day      TEXT NOT NULL,      -- UTC date
    provider TEXT NOT NULL,      -- Gemini / Groq / OpenRouter / Mistral / Cerebras
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

CREATE TABLE IF NOT EXISTS places (
    name TEXT PRIMARY KEY,       -- a job's place as written, e.g. "Frankfurt am Main"
    lat  REAL,                   -- empty: OpenStreetMap doesn't know it
    lon  REAL
);

CREATE TABLE IF NOT EXISTS app_emails (
    gmail_id    TEXT PRIMARY KEY,    -- an email from the "Bewerbungen" label (each is read once)
    received_at TEXT,
    sender      TEXT,
    subject     TEXT,
    body        TEXT,                -- start of the text (empty for emails that aren't about an application)
    kind        TEXT,                -- confirmation / rejection / interview / test / other
    kind_from   TEXT,                -- rules / ai / you
    company     TEXT,                -- employer the AI read from the email
    job_id      INTEGER,             -- the applied job it belongs to (while in review: the best guess)
    state       TEXT NOT NULL,       -- matched / review / ignored (not about an application) / dismissed
    note        TEXT,                -- why it needs review
    notified    INTEGER NOT NULL DEFAULT 0,  -- 1 = the 🎉 / 📝 Telegram message went out
    read_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,      -- switches you flip in the dashboard, e.g. "telegram_paused"
    value TEXT NOT NULL
);
"""

# Columns added after Phase 1. Existing databases get them automatically.
MIGRATIONS = {
    "links": {
        "kind": "TEXT NOT NULL DEFAULT 'url'",               # 'url' or 'arbeitsagentur'
        "baseline_done": "INTEGER NOT NULL DEFAULT 0",       # 1 after the first successful check
        "auto_added": "INTEGER NOT NULL DEFAULT 0",          # 1 = added by the Company Discovery Engine
    },
    "jobs": {
        "hours": "TEXT",                # working hours if the ad mentions them, e.g. "15–20 h/week"
        "filter_passed": "INTEGER",     # keyword filter: 1 = shown, 0 = filtered out
        "filter_reason": "TEXT",        # why, e.g. "Werkstudent + Software" or "Full-time ('Vollzeit')"
        "ai_score": "INTEGER",          # Gemini: 1-10 (empty = not scored yet)
        "ai_reason": "TEXT",            # Gemini: one sentence why
        "ai_full_time": "INTEGER",      # Gemini: 1 if it decided the job is really full-time
        "ai_model": "TEXT",
        "ai_attempts": "INTEGER NOT NULL DEFAULT 0",   # failed scoring tries (gives up after 3)
        "details_fetched": "INTEGER NOT NULL DEFAULT 0",  # 1 = we already tried to load the full ad
        "duplicate_of": "INTEGER",      # id of the job this one duplicates (then it's hidden)
        "notified": "INTEGER NOT NULL DEFAULT 0",  # Telegram: 0 = not yet, 1 = sent, 2 = failed
        "status": "TEXT NOT NULL DEFAULT ''",      # '' / saved / applied / hidden (set by you)
        # 🔥 Must apply (must_apply.py)
        "ai_blocker": "TEXT",           # AI: '' = no blocker, else why (2+ years experience, staffing agency, Master only)
        "must_apply": "INTEGER NOT NULL DEFAULT 0",  # 1 = in the Must apply box (until applied / hidden)
        "must_reasons": "TEXT",         # JSON: the conditions it met
        "must_why_not": "TEXT",         # the first condition it failed (for recent good jobs)
        "must_at": "TEXT",              # when it went into the box
        "must_sent_at": "TEXT",         # when the "🔥 MUST APPLY" Telegram message went out
        "must_reminded": "INTEGER NOT NULL DEFAULT 0",  # 1 = the 24 h reminder went out
        # Application tracking (applications.py)
        "applied_at": "TEXT",           # when you marked it Applied
        "app_status": "TEXT",           # from the emails: confirmation / test / interview / rejection (empty = no reply yet)
    },
}

# Cloudflare's free limit is counted in "neurons" (its own unit of AI work), not requests
MIGRATIONS["ai_usage"] = {"neurons": "REAL NOT NULL DEFAULT 0"}

# When a column is first added, run this once. Jobs found before Telegram existed
# count as "already notified", so you don't get a flood of old jobs.
# Company Discovery Engine, Phase B: the jobs read from each company and its fit score
MIGRATIONS["companies"] = {
    "status_by": "TEXT NOT NULL DEFAULT 'engine'",  # engine / you: the engine never changes your choice
    "jobs_read_at": "TEXT",          # when its job list was last read
    "jobs_method": "TEXT",           # e.g. "Personio feed", "Structured data", "AI from page text"
    "jobs_note": "TEXT",             # why reading failed, if it did
    "jobs_total": "INTEGER",         # jobs on its list (anywhere)
    "jobs_near": "INTEGER",          # ... in the Darmstadt / Frankfurt area (or remote, or no place given)
    "it_jobs": "INTEGER",            # IT jobs in the area
    "student_jobs": "INTEGER",       # Werkstudent / part-time jobs in the area
    "good_jobs": "INTEGER",          # jobs in the area that pass your keyword filter
    "fresh_jobs": "INTEGER",         # jobs posted in the last 14 days
    "it_titles": "TEXT",             # JSON: a few IT job titles (for the Initiativ reason)
    "student_history": "INTEGER NOT NULL DEFAULT 0",  # Werkstudent jobs from it already in your job history
    "size_ai": "TEXT",               # big / mid / small / unknown, from the AI (famous names count as big)
    "fit_score": "INTEGER",          # 0-100, see company_jobs.fit()
    "fit_reasons": "TEXT",
    "initiativ_reason": "TEXT",      # one line from the AI: why an Initiativbewerbung could fit
    "link_id": "INTEGER",            # the watched link it was auto-added as
    # Phase C
    "origin_note": "TEXT",           # e.g. "like Software AG" (look-alike), shown under the name
    "initiativ_at": "TEXT",          # when it went on the Initiativ list (for the weekly summary)
    "good_note": "TEXT",             # why its jobs are (not) good, e.g. "1 good · 4 outside the area (Bundesweit)"
    # Categories (industry method), see categories.py
    "category": "TEXT",              # insurance / finance / cloud / security / software / other (empty = not sorted yet)
    "category_from": "TEXT",         # name / jobs / osm / ai / you: who decided
    "ad_titles": "TEXT",             # JSON: a few titles of its Arbeitsagentur IT ads (for the category)
    # Region coverage, see region.py
    "town": "TEXT",                  # e.g. "Darmstadt": in a town whose businesses are all covered
    "status_note": "TEXT",           # why the engine gave it this status, e.g. "Skipped by rule: a restaurant"
    "contact_email": "TEXT",         # from its Impressum (small IT companies without a careers page)
    "contact_url": "TEXT",           # the page the email was on
    "contact_checked_at": "TEXT",    # when the Impressum was read (once)
    "jobs_part_hash": "TEXT",        # the page's job part when the AI last read it: unchanged = no AI request
}

AFTER_ADDING = {
    ("jobs", "notified"): "UPDATE jobs SET notified = 1",
    # Before Phase B, Initiativ and Skipped could only be set by you
    ("companies", "status_by"): "UPDATE companies SET status_by = 'you' WHERE status IN ('initiativ', 'skipped')",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    if (table, column) in AFTER_ADDING:
                        conn.execute(AFTER_ADDING[(table, column)])
        # Texts saved before the grammar fix ("Found 1 job postings")
        conn.execute("UPDATE links SET reason = REPLACE(reason, 'Found 1 job postings', 'Found 1 job posting') "
                     "WHERE reason LIKE 'Found 1 job postings%'")


# ---- links -------------------------------------------------------------------

def _link_dict(row: sqlite3.Row) -> dict:
    link = dict(row)
    link.pop("sample_jobs", None)
    link["active"] = bool(link["active"])
    return link


def list_links(min_score: int | None = None) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT links.*,
                      (SELECT COUNT(*) FROM jobs WHERE jobs.link_id = links.id AND {shown_sql(min_score)})
                          AS matching_jobs
               FROM links ORDER BY kind = 'url', created_at DESC"""
        ).fetchall()
    return [_link_dict(r) for r in rows]


def get_link(link_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    return _link_dict(row) if row else None


def find_link_by_url(url: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM links WHERE url = ?", (url,)).fetchone()
    return _link_dict(row) if row else None


def add_link(url: str, kind: str = "url", name: str = "") -> int:
    with connect() as conn:
        cur = conn.execute("INSERT INTO links (url, kind, name, created_at) VALUES (?, ?, ?, ?)",
                           (url, kind, name, now()))
        return cur.lastrowid


def save_check_result(link_id: int, status: str, reason: str, method: str, name: str,
                      jobs_on_page: int, new_jobs: int, new_matching: int) -> None:
    """new_matching = new jobs that passed the filter; only those count as "last new job"."""
    with connect() as conn:
        conn.execute(
            """UPDATE links SET status = ?, reason = ?, method = ?, jobs_in_last_test = ?,
                   last_checked = ?, name = COALESCE(NULLIF(name, ''), ?),
                   baseline_done = MAX(baseline_done, ?),
                   total_jobs = total_jobs + ?,
                   last_new_job_at = CASE WHEN ? > 0 THEN ? ELSE last_new_job_at END
               WHERE id = ?""",
            (status, reason, method, jobs_on_page, now(), name, int(status == "green"),
             new_jobs, new_matching, now(), link_id),
        )


def set_link_active(link_id: int, active: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE links SET active = ? WHERE id = ?", (int(active), link_id))


def delete_link(link_id: int) -> None:
    """Removes the link but keeps the jobs it found (they stay in your job history)."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET link_id = NULL WHERE link_id = ?", (link_id,))
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))


# ---- jobs --------------------------------------------------------------------

def insert_new_jobs(link: dict, jobs: list[dict], baseline: bool) -> list[dict]:
    """Saves the jobs we don't know yet. Known = same URL, same simplified title + company,
    or a similar title at a similar company (see dedup.py). Returns the new ones.
    A job dict can override the baseline with its own "baseline" value."""
    found_at = now()
    new = []
    with connect() as conn:
        known_jobs = [dict(r) for r in conn.execute(
            "SELECT title, company, location FROM jobs WHERE duplicate_of IS NULL")]
        for j in jobs:
            known = conn.execute("SELECT 1 FROM jobs WHERE url = ? OR dedup_key = ?",
                                 (j["url"], j["dedup_key"])).fetchone()
            if known or dedup.find_duplicate(j, known_jobs):
                continue
            known_jobs.append({"title": j["title"], "company": j["company"], "location": j["location"]})
            conn.execute(
                """INSERT INTO jobs (link_id, source, url, dedup_key, title, company, location,
                                     description, employment, posted_at, first_seen, baseline,
                                     hours, filter_passed, filter_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (link["id"], link["name"] or link["url"], j["url"], j["dedup_key"], j["title"],
                 j["company"], j["location"], j["description"], j["employment"],
                 j["posted"] or None, found_at, int(j.get("baseline", baseline)),
                 j["hours"] or None, int(j["filter_passed"]), j["filter_reason"]),
            )
            new.append(j)
    return new


def all_jobs_for_filter() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, title, employment, description FROM jobs").fetchall()
    return [dict(r) for r in rows]


def save_filter_results(results: list[tuple[int, bool, str, str]]) -> None:
    """results: (job id, passed, reason, hours)"""
    with connect() as conn:
        conn.executemany(
            "UPDATE jobs SET filter_passed = ?, filter_reason = ?, hours = ? WHERE id = ?",
            [(int(passed), reason, hours or None, job_id) for job_id, passed, reason, hours in results],
        )


def shown_sql(min_score: int | None) -> str:
    """Jobs in your main list: passed the keyword filter, not a duplicate, and - when Gemini
    is on (min_score given) - scored at least min_score or not scored yet."""
    base = "filter_passed = 1 AND duplicate_of IS NULL"
    if min_score is None:
        return base
    return f"{base} AND (ai_score IS NULL OR ai_score >= {int(min_score)})"


def view_sql(view: str, min_score: int | None) -> str:
    visible = f"{shown_sql(min_score)} AND status != 'hidden'"
    # Best new: not opened yet, not applied/hidden, and (with Gemini) scored at least min_score
    best = f"{visible} AND seen = 0 AND status != 'applied'"
    if min_score is not None:
        best += f" AND ai_score >= {int(min_score)}"
    return {
        "best": best,
        "all": visible,
        "saved": "status = 'saved' AND duplicate_of IS NULL",
        "applied": "status = 'applied' AND duplicate_of IS NULL",
        "low": f"filter_passed = 1 AND duplicate_of IS NULL AND status != 'hidden' AND ai_score < {int(min_score or 0)}",
        # Dropped by the keyword filter, or hidden by you
        "filtered": "duplicate_of IS NULL AND (filter_passed = 0 OR status = 'hidden')",
        # Every job except duplicates. The dashboard sorts them into the views itself.
        "everything": "duplicate_of IS NULL",
    }[view]


VIEWS = ("best", "all", "saved", "applied", "low", "filtered")
STATUSES = ("", "saved", "applied", "hidden")

LIST_COLUMNS = """id, link_id, source, url, title, company, location, employment, hours,
                  posted_at, first_seen, seen, status, filter_passed, filter_reason,
                  ai_score, ai_reason, ai_full_time, ai_attempts, ai_blocker,
                  must_apply, must_reasons, must_at, must_why_not, applied_at, app_status"""


def list_jobs(view: str = "all", min_score: int | None = None, limit: int = 500) -> list[dict]:
    # "Best new" is sorted by score, everything else newest first
    order = "ai_score DESC, " if view == "best" else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT {LIST_COLUMNS} FROM jobs WHERE {view_sql(view, min_score)}
                ORDER BY {order}first_seen DESC, COALESCE(posted_at, '') DESC, id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        links = {r["id"]: dict(r) for r in conn.execute("SELECT id, kind, url FROM links")}
    from .source_types import site, source_type  # here, because source_types imports gmail, which imports db
    return [{**dict(r), "source_type": source_type(links.get(r["link_id"]), r["source"]),
             "site": site(links.get(r["link_id"]), r["source"], r["url"])} for r in rows]


def get_job(job_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(f"SELECT {LIST_COLUMNS}, description FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def set_status(job_id: int, status: str) -> None:
    """saved / applied / hidden, or "" to undo. Marking a job also counts as having seen it."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET status = ?, seen = 1, applied_at = CASE WHEN ? = 'applied' "
                     "THEN COALESCE(applied_at, ?) ELSE applied_at END WHERE id = ?", (status, status, now(), job_id))


def count_jobs(min_score: int | None = None) -> dict:
    counts = ",\n".join(f'COALESCE(SUM({view_sql(v, min_score)}), 0) AS "{v}"' for v in VIEWS)
    with connect() as conn:
        row = conn.execute(
            f"""SELECT {counts},
                       COALESCE(SUM({shown_sql(None)} AND ai_score IS NULL AND ai_attempts < 3), 0)
                           AS ai_pending
                FROM jobs"""
        ).fetchone()
    return dict(row)


def last_check() -> str | None:
    with connect() as conn:
        return conn.execute("SELECT MAX(last_checked) FROM links").fetchone()[0]


def next_due(interval_minutes: int) -> str | None:
    """When the next active link is due for its check (ISO time), None if there are none."""
    with connect() as conn:
        rows = conn.execute("SELECT last_checked FROM links WHERE active = 1").fetchall()
    if not rows:
        return None
    if any(r[0] is None for r in rows):
        return now()
    earliest = min(datetime.fromisoformat(r[0]) for r in rows)
    return (earliest + timedelta(minutes=interval_minutes)).isoformat(timespec="seconds")


# ---- Gemini scoring ------------------------------------------------------------

def jobs_to_score(limit: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, url, title, company, location, employment, hours, description, details_fetched
                FROM jobs
                WHERE {shown_sql(None)} AND ai_score IS NULL AND ai_attempts < 3
                ORDER BY first_seen DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_description(job_id: int, description: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE jobs SET description = ?, details_fetched = 1 WHERE id = ?", (description, job_id))


def count_ai_attempt(job_ids: list[int]) -> None:
    with connect() as conn:
        conn.executemany("UPDATE jobs SET ai_attempts = ai_attempts + 1 WHERE id = ?", [(i,) for i in job_ids])


def save_scores(results: list[dict], model: str) -> None:
    with connect() as conn:
        conn.executemany(
            """UPDATE jobs SET ai_score = ?, ai_reason = ?, ai_full_time = ?, ai_model = ?,
                   hours = COALESCE(hours, NULLIF(?, '')), ai_blocker = ?
               WHERE id = ?""",
            [(r["score"], r["reason"], int(r["full_time"]), model, r["hours"], r.get("blocker", ""), r["id"])
             for r in results],
        )


# ---- 🔥 Must apply -----------------------------------------------------------------

def must_candidates(min_score: int, days: int) -> list[dict]:
    """Recent good jobs that aren't in the Must apply box yet (and not applied / hidden)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""SELECT id, link_id, source, url, dedup_key, title, company, location, employment, hours, description,
                       posted_at, first_seen, ai_score, ai_blocker
                FROM jobs WHERE {shown_sql(None)} AND must_apply = 0 AND status NOT IN ('applied', 'hidden')
                AND ai_score >= ? AND COALESCE(posted_at, first_seen) >= ?""", (min_score, since[:10]))]


def save_must(job_id: int, ok: bool, met: list[str], why_not: str) -> None:
    with connect() as conn:
        if ok:
            conn.execute("UPDATE jobs SET must_apply = 1, must_reasons = ?, must_at = ?, must_why_not = NULL WHERE id = ?",
                         (json.dumps(met, ensure_ascii=False), now(), job_id))
        else:
            conn.execute("UPDATE jobs SET must_why_not = ? WHERE id = ?", (why_not, job_id))


def rescore_recent(min_score: int, days: int) -> int:
    """Recent good jobs scored before the blocker check: score them again (they keep 'notified')."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            f"""UPDATE jobs SET ai_score = NULL, ai_attempts = 0 WHERE {shown_sql(None)} AND ai_blocker IS NULL
                AND ai_score >= ? AND status NOT IN ('applied', 'hidden')
                AND COALESCE(posted_at, first_seen) >= ?""", (min_score, since[:10])).rowcount


def requeue_unscored(days: int) -> int:
    """Recent jobs the AI gave up on (3 failed tries): back into the waiting line."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            f"""UPDATE jobs SET ai_attempts = 0 WHERE {shown_sql(None)} AND ai_score IS NULL AND ai_attempts >= 3
                AND first_seen >= ?""", (since,)).rowcount


def must_to_send(max_age_hours: int) -> list[dict]:
    """Must apply jobs whose Telegram message hasn't gone out (only ones that came in recently)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, url, title, company, location, hours, posted_at, first_seen, source, ai_score, ai_reason,
                      must_reasons FROM jobs WHERE must_apply = 1 AND must_sent_at IS NULL AND must_at >= ?
               AND status NOT IN ('applied', 'hidden') ORDER BY ai_score DESC""", (since,))]


def must_to_remind(after_hours: int) -> list[dict]:
    before = (datetime.now(timezone.utc) - timedelta(hours=after_hours)).isoformat(timespec="seconds")
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, url, title, company, location, hours, posted_at, first_seen, source, ai_score, ai_reason,
                      must_reasons FROM jobs WHERE must_apply = 1 AND must_sent_at <= ? AND must_reminded = 0
               AND status NOT IN ('applied', 'hidden')""", (before,))]


def mark_must_sent(job_id: int, reminder: bool = False) -> None:
    with connect() as conn:
        if reminder:
            conn.execute("UPDATE jobs SET must_reminded = 1 WHERE id = ?", (job_id,))
        else:  # the normal job message isn't needed any more
            conn.execute("UPDATE jobs SET must_sent_at = ?, notified = 1 WHERE id = ?", (now(), job_id))


def get_place(name: str) -> tuple | None:
    """(lat, lon); (None, None) = OpenStreetMap doesn't know it; None = never looked up."""
    with connect() as conn:
        row = conn.execute("SELECT lat, lon FROM places WHERE name = ?", (name.lower(),)).fetchone()
    return (row[0], row[1]) if row else None


def save_place(name: str, found: tuple[float, float] | None) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO places (name, lat, lon) VALUES (?, ?, ?)",
                     (name.lower(), *(found or (None, None))))


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def ai_usage_today() -> dict[str, int]:
    with connect() as conn:
        return dict(conn.execute("SELECT provider, requests FROM ai_usage WHERE day = ?", (_today(),)).fetchall())


def count_ai_request(provider: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO ai_usage (day, provider, requests) VALUES (?, ?, 1) "
                     "ON CONFLICT(day, provider) DO UPDATE SET requests = requests + 1", (_today(), provider))


def ai_neurons_today() -> dict[str, float]:
    with connect() as conn:
        return dict(conn.execute("SELECT provider, neurons FROM ai_usage WHERE day = ?", (_today(),)).fetchall())


def count_ai_neurons(provider: str, neurons: float) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO ai_usage (day, provider, neurons) VALUES (?, ?, ?) "
                     "ON CONFLICT(day, provider) DO UPDATE SET neurons = neurons + excluded.neurons",
                     (_today(), provider, neurons))


def use_up_ai(provider: str, budget: int) -> None:
    """The provider said its daily quota is gone: count it as used up for today."""
    with connect() as conn:
        conn.execute("INSERT INTO ai_usage (day, provider, requests) VALUES (?, ?, ?) "
                     "ON CONFLICT(day, provider) DO UPDATE SET requests = MAX(requests, excluded.requests)",
                     (_today(), provider, budget))


def link_kinds() -> dict[int, dict]:
    with connect() as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT id, kind, url FROM links")}


# ---- Telegram ------------------------------------------------------------------

def jobs_to_notify(min_score: int | None, ai_on: bool, limit: int) -> list[dict]:
    """Good new jobs not sent yet. Not sent: jobs from the very first check of a link
    (that would be a flood), and - while Gemini is on - jobs still waiting for a score."""
    waiting_for_ai = "AND (ai_score IS NOT NULL OR ai_attempts >= 3)" if ai_on else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, url, title, company, location, hours, posted_at, first_seen, source,
                       ai_score, ai_reason
                FROM jobs
                WHERE {shown_sql(min_score)} AND notified = 0 AND baseline = 0 {waiting_for_ai}
                ORDER BY COALESCE(ai_score, 0) DESC, first_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_notified(job_ids: list[int], failed: bool = False) -> None:
    with connect() as conn:
        conn.executemany("UPDATE jobs SET notified = ? WHERE id = ?", [(2 if failed else 1, i) for i in job_ids])


def latest_good_job(min_score: int | None) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, url, title, company, location, hours, posted_at, first_seen, source,
                       ai_score, ai_reason
                FROM jobs WHERE {shown_sql(min_score)}
                ORDER BY first_seen DESC, COALESCE(ai_score, 0) DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


# ---- job-alert emails ------------------------------------------------------------

def known_emails(gmail_ids: list[str]) -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT gmail_id FROM emails").fetchall()
    return {r[0] for r in rows} & set(gmail_ids)


def remember_emails(gmail_ids: list[str]) -> None:
    with connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO emails (gmail_id, read_at) VALUES (?, ?)",
                         [(i, now()) for i in gmail_ids])


# ---- application emails (applications.py) -----------------------------------------

APP_EMAIL_COLUMNS = ("gmail_id", "received_at", "sender", "subject", "body", "kind", "kind_from", "company",
                     "job_id", "state", "note", "notified")


def known_app_emails(gmail_ids: list[str]) -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT gmail_id FROM app_emails").fetchall()
    return {r[0] for r in rows} & set(gmail_ids)


def save_app_email(e: dict) -> None:
    row = {c: e.get(c) for c in APP_EMAIL_COLUMNS}
    row["notified"] = row["notified"] or 0
    with connect() as conn:
        conn.execute(f"""INSERT OR REPLACE INTO app_emails ({', '.join(APP_EMAIL_COLUMNS)}, read_at)
                         VALUES ({', '.join(':' + c for c in APP_EMAIL_COLUMNS)}, :read_at)""", {**row, "read_at": now()})


def app_email(gmail_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM app_emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
    return dict(row) if row else None


def app_emails(state: str) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM app_emails WHERE state = ? ORDER BY received_at", (state,))]


def update_app_email(gmail_id: str, fields: dict) -> None:
    with connect() as conn:
        conn.execute(f"UPDATE app_emails SET {', '.join(f'{k} = :{k}' for k in fields)} WHERE gmail_id = :gmail_id",
                     {**fields, "gmail_id": gmail_id})


def applied_jobs() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, title, company, url, applied_at, app_status FROM jobs
               WHERE status = 'applied' AND duplicate_of IS NULL""")]


def set_app_statuses(statuses: dict[int, str]) -> None:
    """job id -> its status from the emails; every other job has none."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET app_status = NULL WHERE app_status IS NOT NULL")
        conn.executemany("UPDATE jobs SET app_status = ? WHERE id = ?", [(s, i) for i, s in statuses.items()])


def app_emails_to_notify(since: str) -> list[dict]:
    """Interview invites and tests that came in after `since` and weren't sent to Telegram yet."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT e.*, j.title AS job_title, j.url AS job_url, j.company AS job_company
               FROM app_emails e LEFT JOIN jobs j ON j.id = e.job_id AND e.state = 'matched'
               WHERE e.kind IN ('interview', 'test') AND e.state IN ('matched', 'review') AND e.notified = 0
               AND e.received_at >= ? ORDER BY e.received_at""", (since,))]


# ---- duplicates ----------------------------------------------------------------

def mark_duplicates() -> int:
    """Look for duplicates among the saved jobs (e.g. saved before dedup got smarter).
    The newer copy is hidden, not deleted. Returns how many were found."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, company, location FROM jobs WHERE duplicate_of IS NULL ORDER BY id")]
        kept, marks = [], []
        for job in rows:
            original = dedup.find_duplicate(job, kept)
            if original:
                marks.append((original["id"], job["id"]))
            else:
                kept.append(job)
        conn.executemany("UPDATE jobs SET duplicate_of = ? WHERE id = ?", marks)
    return len(marks)


def mark_seen(job_id: int | None = None) -> None:
    """Mark one job as seen, or all jobs if no id is given."""
    with connect() as conn:
        if job_id is None:
            conn.execute("UPDATE jobs SET seen = 1 WHERE seen = 0")
        else:
            conn.execute("UPDATE jobs SET seen = 1 WHERE id = ?", (job_id,))


def set_seen(job_ids: list[int], seen: bool) -> None:
    """Mark several jobs as seen, or as new again (for "Undo")."""
    with connect() as conn:
        conn.executemany("UPDATE jobs SET seen = ? WHERE id = ?", [(int(seen), i) for i in job_ids])


# ---- source suggestions (Phase 8) ------------------------------------------------

def suggestion_keys() -> set[str]:
    """Companies already suggested, added or skipped: they are never suggested again."""
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT company_key FROM suggestions")}


def suggestion_names() -> list[str]:
    """Every company ever suggested (open, added, skipped or without a working link)."""
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT company FROM suggestions")]


def add_suggestion(s: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO suggestions (company, company_key, city, size, focus, data_count, url,
                   method, state, reason, total_jobs, good_jobs, status, found_at)
               VALUES (:company, :company_key, :city, :size, :focus, :data_count, :url,
                       :method, :state, :reason, :total_jobs, :good_jobs, :status, :found_at)""",
            {"status": "new", **s, "found_at": now()},
        )


def list_suggestions() -> list[dict]:
    """Open suggestions: good jobs first, then any jobs, then pages without a job list."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM suggestions WHERE status = 'new'
               ORDER BY CASE state WHEN 'green' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END,
                        good_jobs DESC, total_jobs DESC, company""").fetchall()
    return [dict(r) for r in rows]


def get_suggestion(suggestion_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return dict(row) if row else None


def update_suggestion(suggestion_id: int, fields: dict) -> None:
    with connect() as conn:
        conn.execute(f"UPDATE suggestions SET {', '.join(f'{k} = :{k}' for k in fields)} WHERE id = :id",
                     {**fields, "id": suggestion_id})


def set_suggestion_status(suggestion_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id))


def companies_in_jobs(sources: tuple[str, ...]) -> list[str]:
    """Company names of jobs from these sources that passed your keyword filter."""
    marks = ",".join("?" * len(sources))
    with connect() as conn:
        rows = conn.execute(f"""SELECT company FROM jobs WHERE source IN ({marks})
                                AND filter_passed = 1 AND duplicate_of IS NULL AND company != ''""", sources)
        return [r[0] for r in rows]


def all_employer_names() -> list[str]:
    """Every employer name Job Radar has seen: jobs (all sources), suggestions, watched links."""
    with connect() as conn:
        rows = conn.execute("""SELECT company FROM jobs WHERE company != '' AND company IS NOT NULL
                               UNION ALL SELECT company FROM suggestions
                               UNION ALL SELECT name FROM links WHERE kind = 'url' AND name != ''""")
        return [r[0] for r in rows]


# ---- companies (Company Discovery Engine) -----------------------------------------

def company_rows() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM companies")]


def save_companies(rows: list[dict]) -> int:
    """Insert new companies and update known ones (by id). Returns how many are new."""
    added = 0
    with connect() as conn:
        for c in rows:
            if c.get("id"):
                conn.execute("""UPDATE companies SET name = :name, city = :city, distance_km = :distance_km,
                                lat = :lat, lon = :lon, sources = :sources, size_hint = :size_hint,
                                ad_titles = COALESCE(:ad_titles, ad_titles),
                                hint_from = CASE WHEN :website_hint IS NULL THEN hint_from ELSE :hint_from END,
                                website_hint = COALESCE(:website_hint, website_hint) WHERE id = :id""", c)
            else:
                conn.execute("""INSERT INTO companies (name, name_key, city, distance_km, lat, lon, sources,
                                    size_hint, website_hint, hint_from, ad_titles, first_seen)
                                VALUES (:name, :name_key, :city, :distance_km, :lat, :lon, :sources,
                                        :size_hint, :website_hint, :hint_from, :ad_titles, :first_seen)""",
                             {"ad_titles": None, **c, "first_seen": now()})
                added += 1
    return added


def save_company_check(company_id: int, fields: dict, stamp: str = "last_checked") -> None:
    """Save what the engine found out. stamp: the time column to set (last_checked / jobs_read_at)."""
    update_company(company_id, {**fields, stamp: now()})


def update_company(company_id: int, fields: dict) -> None:
    fields = dict(fields)
    status = fields.pop("status", None)
    with connect() as conn:
        if fields:
            conn.execute(f"UPDATE companies SET {', '.join(f'{k} = :{k}' for k in fields)} WHERE id = :id",
                         {**fields, "id": company_id})
        if status:  # a status you chose yourself always wins
            conn.execute("UPDATE companies SET status = ? WHERE id = ? AND status_by != 'you'", (status, company_id))


def list_companies() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def set_company_category(company_id: int, category: str) -> bool:
    """Your choice in the dashboard: the rules and the AI won't change it again."""
    with connect() as conn:
        return conn.execute("UPDATE companies SET category = ?, category_from = 'you' WHERE id = ?",
                            (category, company_id)).rowcount > 0


def set_company_status(company_id: int, status: str) -> bool:
    """Your choice in the dashboard: the engine won't change it again."""
    with connect() as conn:
        return conn.execute("UPDATE companies SET status = ?, status_by = 'you', status_note = NULL, initiativ_at = CASE WHEN ? = "
                            "'initiativ' AND status != 'initiativ' THEN ? ELSE initiativ_at END WHERE id = ?",
                            (status, status, now(), company_id)).rowcount > 0


def companies_by_key(keys: list[str]) -> list[dict]:
    with connect() as conn:
        marks = ",".join("?" * len(keys))
        return [dict(r) for r in conn.execute(f"SELECT * FROM companies WHERE name_key IN ({marks})", keys)] if keys else []


def skipped_company_names() -> list[str]:
    """Companies you skipped on the Companies page or in Suggested sources: never suggested again.
    (Not the ones the region rules skipped, like a bakery: an import may still add those.)"""
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT name FROM companies WHERE status = 'skipped' AND status_by = 'you' "
                                           "UNION SELECT company FROM suggestions WHERE status = 'skipped'")]


def liked_employers() -> list[tuple[str, str]]:
    """(company, a job title) for jobs you saved or applied to, applied first: seeds for look-alikes."""
    with connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(
            """SELECT company, MIN(title) FROM jobs WHERE status IN ('saved', 'applied')
               AND company IS NOT NULL AND company != '' GROUP BY company
               ORDER BY MAX(status = 'applied') DESC, MAX(first_seen) DESC""")]


def week_numbers(since: str, min_score: int | None) -> dict:
    """Counts for the weekly Telegram summary (everything after `since`)."""
    with connect() as conn:
        one = lambda sql: conn.execute(sql, (since,)).fetchone()[0]
        return {
            "new_companies": one("SELECT COUNT(*) FROM companies WHERE first_seen >= ?"),
            "career_pages": one("SELECT COUNT(*) FROM companies WHERE career_url IS NOT NULL AND last_checked >= ?"),
            "good_jobs": one("SELECT COUNT(*) FROM jobs WHERE first_seen >= ? AND filter_passed = 1 AND duplicate_of IS NULL"
                             + ("" if min_score is None else f" AND ai_score >= {int(min_score)}")),
            "initiativ": one("SELECT COUNT(*) FROM companies WHERE status = 'initiativ' AND initiativ_at >= ?"),
            "top": [dict(r) for r in conn.execute(
                """SELECT name, fit_score, fit_reasons, good_jobs FROM companies
                   WHERE status != 'skipped' AND fit_score IS NOT NULL ORDER BY fit_score DESC, good_jobs DESC LIMIT 3""")],
        }


def employer_titles() -> list[tuple[str, str]]:
    """(company, title) of every job in your history, for "has posted Werkstudent jobs before"."""
    with connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT company, title FROM jobs WHERE company IS NOT NULL AND company != ''")]


def employer_jobs() -> list[tuple[str, str, str]]:
    """(company, simplified "title|company", first seen) of every job, for "posted Werkstudent jobs before"."""
    with connect() as conn:
        return [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT company, dedup_key, first_seen FROM jobs WHERE company IS NOT NULL AND company != ''")]


def mark_auto_added(link_id: int) -> None:
    with connect() as conn:
        # Its company's jobs were read already and went the normal way: the link's first check is no baseline
        conn.execute("UPDATE links SET auto_added = 1, baseline_done = 1 WHERE id = ?", (link_id,))


# ---- dashboard switches --------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
