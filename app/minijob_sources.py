"""Extra Minijob sources that are not job APIs: Remotely (remotely.de).

Polite on purpose: only the two list pages, once a day (night run); then only ads we have not seen; one request
at a time with a pause; and only paths robots.txt allows (fetcher.fetch_polite asks robots.txt for every page and
says honestly who we are). Nothing is clicked, no login, no search page (robots.txt forbids /search).
"""
import html as htmllib
import json
import logging
import re
import time
from urllib.parse import urljoin

from . import db, gmail
from .config import CONFIG
from .extractors import clean, relative_date, to_iso
from .fetcher import fetch_polite

log = logging.getLogger("jobradar")

SOURCE_RM = "Remotely"
BASE = "https://www.remotely.de"
LIST_PAGES = (f"{BASE}/remote-jobs/minijobs", f"{BASE}/home-office-jobs/minijobs")
PAUSE_SECONDS = 4          # between two pages, on top of the fetcher's own 1 request/second rule
JOB_LINK = re.compile(r'href="(/job/[a-z0-9][a-z0-9-]*)"')


def list_ads() -> list[dict]:
    """The ad links on the two list pages (about 15 each). Raises if no list page could be read at all."""
    found: dict[str, dict] = {}
    errors = []
    for i, page_url in enumerate(LIST_PAGES):
        if i:
            time.sleep(PAUSE_SECONDS)
        page = fetch_polite(page_url)
        if not page.ok:
            errors.append(f"{page_url}: {page.reason}")
            continue
        for path in JOB_LINK.findall(page.html):
            url = urljoin(BASE, path)
            found.setdefault(url, {"url": url, "source": SOURCE_RM, "title": path.rsplit("/", 1)[-1].replace("-", " "),
                                   "company": "", "location": "", "posted_at": "", "description": ""})
    if not found and errors:
        raise RuntimeError("; ".join(errors))
    return list(found.values())


# ---- one ad page ---------------------------------------------------------------------------------

def _lines(html: str) -> list[str]:
    """Visible text of the page's <main> part, one entry per line (scripts, styles and templates removed)."""
    start = html.find("<main")
    main = html[start:html.find("</main>", start)] if start >= 0 else html
    main = re.sub(r"<(script|style|template)\b.*?</\1>", "", main, flags=re.S)
    return [htmllib.unescape(t).strip() for t in re.sub(r"<[^>]+>", "\n", main).split("\n") if t.strip()]


def _jsonld(html: str) -> dict:
    m = re.search(r'<script id="job-posting-jsonld"[^>]*>(.*?)</script>', html, re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def parse_ad(html: str) -> dict:
    """What we keep from an ad page: the ad text only (between the buttons 'Job melden' and 'Hilfe bei deiner
    Bewerbung'), so the site menu, footer and 'similar ads' never reach the filter or the AI.
    Returns title, company, location, posted_at, remote_flag (the site's own 'Vollständig remote') and description.
    An empty description means the page layout was not recognised (the caller skips the ad)."""
    lines = _lines(html)
    ld = _jsonld(html)
    out = {"title": clean(ld.get("title")), "company": clean((ld.get("hiringOrganization") or {}).get("name")),
           "location": "", "posted_at": to_iso(ld.get("datePosted")) if ld.get("datePosted") else "",
           "remote_flag": False, "mixed_type": False, "description": ""}
    try:
        h1 = html.index("<h1")
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", html[h1:html.index("</h1>", h1)])).strip()
        out["title"] = out["title"] or clean(title)
    except ValueError:
        pass
    # facts block "Eckdaten ... Arbeitsmodell ... Jetzt bewerben": hours, pay, place, work model
    if "Eckdaten" in lines and "Jetzt bewerben" in lines:
        facts = lines[lines.index("Eckdaten") + 1:lines.index("Jetzt bewerben")]
        out["remote_flag"] = "Vollständig remote" in facts
        before_model = facts[:facts.index("Arbeitsmodell")] if "Arbeitsmodell" in facts else facts
        items = [f for f in before_model if "€" not in f and not re.search(r"teilzeit|vollzeit|minijob|werkstudent|praktik", f, re.I)]
        out["location"] = items[0] if len(items) > 1 else ""   # "place | field"; with only one entry it is the field
        out["facts"] = " | ".join(facts)
        out["mixed_type"] = bool(re.search(r"vollzeit.*minijob|minijob.*vollzeit", " ".join(before_model), re.I))
    # company and age stand just above the facts: "... | <company> | vor 2 Wochen | <title> | Eckdaten"
    when = next((i for i, l in enumerate(lines[:lines.index("Eckdaten")] if "Eckdaten" in lines else [])
                 if re.match(r"vor \d+ |vor einem |vor einer |heute$|gestern$", l)), None)
    if when:
        out["company"] = out["company"] or clean(lines[when - 1])
        out["posted_at"] = out["posted_at"] or relative_date(lines[when])
    if "Job melden" in lines:
        body = lines[lines.index("Job melden") + 1:]
        if "Hilfe bei deiner Bewerbung" in body:
            body = body[:body.index("Hilfe bei deiner Bewerbung")]
        # the grey advert box between the buttons and the ad ("Immer mehr Remote-Jobs ... Passende Kurse finden")
        if "Passende Kurse finden" in body[:6]:
            body = body[body.index("Passende Kurse finden") + 1:]
        out["description"] = "\n".join(body)
    if not out["description"] and ld.get("description"):  # layout changed: fall back to the ad's own data block
        from .extractors import html_to_text
        out["description"] = html_to_text(htmllib.unescape(ld["description"]), 8000)
    out["description"] = out["description"][:8000]
    return out


def read_ad(url: str) -> dict:
    """Download one ad page (polite, robots.txt) and return parse_ad(). Raises if the page cannot be read."""
    time.sleep(PAUSE_SECONDS)
    page = fetch_polite(url)
    if not page.ok:
        raise RuntimeError(page.reason)
    ad = parse_ad(page.html)
    if not ad["description"]:
        raise RuntimeError("ad text not found on the page")
    return ad


# ---- Gmail: job-alert emails you put in your own label ------------------------------------------------
# Same safe rules as gmail.py: only this one label, read-only (EXAMINE), emails stay unread, nothing is changed.
# The emails only carry title / company / place, so these ads are flagged "unverified" (see minijob.py).

SOURCE_GM = "Gmail alert"


def gmail_label() -> str:
    return str(CONFIG["minijob"].get("gmail_label") or "").strip()


def collect_gmail() -> tuple[list[dict], list[str]]:
    """New ads from the Minijob label. Returns (ads, ids of the emails read). The caller remembers the ids with
    remember_gmail() after the ads are saved. No label set or Gmail not set up: nothing happens."""
    name = gmail_label()
    if not name or not gmail.configured():
        return [], []
    try:
        imap = gmail.open_label(name)
    except gmail.GmailError as e:
        log.info("Minijob Gmail: %s", e)
        return [], []
    ads: list[dict] = []
    read: list[str] = []
    try:
        ids = gmail.ids_since(imap, CONFIG["minijob"].get("gmail_days_back", 14))
        known = db.known_emails(["mj:" + g for g in ids.values()])
        new = [(uid, gid) for uid, gid in ids.items() if "mj:" + gid not in known][-CONFIG["minijob"].get("gmail_max_emails", 30):]
        resolver = gmail.Resolver()
        for uid, gid in new:
            raw = gmail.fetch(imap, uid)
            if raw is None:
                continue
            try:
                jobs = gmail.jobs_from_email(raw, resolver)
            except Exception:
                log.exception("Minijob Gmail: could not read email %s", gid)
                jobs = []
            for j in jobs:
                ads.append({"url": j.url, "source": SOURCE_GM, "title": clean(j.title), "company": clean(j.company),
                            "location": clean(j.location), "posted_at": to_iso(j.posted) if j.posted else "",
                            "description": clean(j.description, 3000), "unverified": True})
            read.append(gid)
    finally:
        gmail._logout(imap)
    return ads, read


def remember_gmail(gids: list[str]) -> None:
    db.remember_emails(["mj:" + g for g in gids])
