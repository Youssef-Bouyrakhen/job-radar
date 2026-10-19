"""Company Discovery Engine, Phase C: more companies from lists you give it, and look-alikes.

1. Import: you paste company names, or the link of a page that lists companies (career fair
   exhibitors like konaktiva, tech hub members, h_da dual-study partners). A clean list of names
   is read without AI; a web page (or messy pasted text) is read by the AI, a few requests at most.
   The companies are marked with your label ("konaktiva 2026") and go through the normal
   pipeline: website, careers page, jobs, fit score.
2. Look-alikes: one AI request suggests small companies in the area similar to the ones whose
   jobs you saved or applied to. They go through the same pipeline.
Companies you skipped are never added back, and never suggested again.
"""
import json
import io
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

from . import ai, db
from .companies import clean_name, merge
from .company_jobs import _pace
from .config import CONFIG
from .discover import NOT_A_COMPANY, _same, company_key
from .extractors import clean
from .fetcher import BOT_HEADERS, browser_available, fetch_polite, fetch_with_browser, robots_allows

log = logging.getLogger("jobradar")

LOOKALIKE = "lookalike"   # source key of look-alike companies
CHUNK = 10_000            # characters of a page per AI request


class ImportProblem(Exception):
    """Something you can fix (bad link, page blocked, no names found). The message says what."""


def source_key(label: str) -> str:
    return "list:" + clean(label, 60)


# ---- reading the names ------------------------------------------------------------------

NAMES_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]},
}
NAMES_PROMPT = """Below is text from {what}: a list of companies (e.g. career fair exhibitors, members
of a tech hub, partner companies of a university).
List the names of the companies / employers in this list, exactly as written (without "GmbH" is fine).
Not the organiser of the list, not menu items, categories, cities, people or event names.
If there are none, return [].

{text}"""
BULLET = re.compile(r"^\s*(?:[-*•·▪►>]|\d{1,3}[.)])\s*")


def _plain_list(text: str) -> list[str] | None:
    """A clean pasted list: one name per line (or one line with commas / semicolons).
    None if it looks like messy text the AI should read instead."""
    lines = [BULLET.sub("", line).strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        lines = [p.strip() for p in re.split(r"[;,\t]", lines[0]) if p.strip()]
    if not lines:
        return []
    long = sum(len(line) > 70 or len(line.split()) > 8 for line in lines)
    return lines if long <= len(lines) * 0.1 else None


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "form"]):
        tag.decompose()
    # Logos are often the only place an exhibitor's name appears
    for img in soup.find_all("img", alt=True):
        img.replace_with(f"\n{img['alt']}\n")
    lines = [clean(line, 150) for line in soup.get_text("\n").splitlines()]
    return "\n".join(dict.fromkeys(line for line in lines if 2 <= len(line) <= 150))


def ai_names(text: str, what: str) -> list[str]:
    if not ai.enabled():
        raise ImportProblem("Reading this needs the AI, but it is off (no AI key in .env). "
                            "Paste the names one per line instead.")
    names, limit = [], CONFIG["companies"]["import_max_ai_requests"]
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    for chunk in chunks[:limit]:
        _pace()
        try:
            answer = ai.ask_json(NAMES_PROMPT.format(what=what, text=chunk), NAMES_SCHEMA, temperature=0.1, priority="jobs")
        except (ai.RateLimited, ai.Overloaded):
            if names:
                break  # keep what was read so far
            raise ImportProblem("The AI's free limit is used up right now. Try again in 10 minutes.")
        names += [clean(a.get("name"), 120) for a in answer if isinstance(a, dict)] if isinstance(answer, list) else []
    if len(chunks) > limit:
        log.info("Import: page is long, only the first %d parts were read", limit)
    return names


def pdf_text(url: str) -> str:
    """Exhibitor catalogues and partner brochures are often PDFs."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportProblem("This link is a PDF, and the PDF reader isn't installed (run setup.bat again). "
                            "Or open the PDF, copy the company names and paste them here.")
    r = httpx.get(url, headers=BOT_HEADERS, timeout=30, follow_redirects=True)
    r.raise_for_status()
    lines = (clean(line, 150) for p in PdfReader(io.BytesIO(r.content)).pages[:60]
             for line in (p.extract_text() or "").splitlines())
    return "\n".join(dict.fromkeys(line for line in lines if 2 <= len(line) <= 150))


def names_from_url(url: str) -> tuple[list[str], str]:
    """(names, note)."""
    page = fetch_polite(url)
    if not page.ok:
        raise ImportProblem(f"The page could not be loaded: {page.reason}. "
                            "Open it in your browser, copy the company names and paste them here instead.")
    note = ""
    if page.html.lstrip().startswith("%PDF"):
        text, note = pdf_text(page.url), "a PDF"
    else:
        text = page_text(page.html)
        # Exhibitor lists are often built with JavaScript: then the plain download is almost empty
        if len(text) < 1500 and browser_available() and robots_allows(url):
            shown = fetch_with_browser(url)
            if shown.ok and len(page_text(shown.html)) > len(text):
                text, note = page_text(shown.html), "read with the hidden browser"
    if len(text) < 30:
        raise ImportProblem("The page has (almost) no text. Copy the names from it and paste them here instead.")
    return ai_names(text, f"the web page {url}"), note


# A postcode followed by a city ("60311 Frankfurt am Main")
POSTCODE = re.compile(r"(?<![\d.,/-])(\d{5})(?=\s*,?\s*[A-ZÄÖÜa-zäöü])")
STREET = re.compile(r"\d+\s*[a-z]?(?:\s*[-–]\s*\d+\s*[a-z]?)?$|stra(?:ß|ss)e|str\.|\bweg\b|platz|allee|\bring\b|"
                    r"postfach|\bgasse\b|ufer\b|landstra", re.I)
BRANCH_OFFICE = re.compile(r"niederlassung|repr[äa]sentanz|\bbranch\b|\bfiliale\b|zweigstelle|\bewr\b", re.I)


def postcode_names(text: str) -> tuple[list[str], str] | None:
    """A pasted list with addresses (e.g. copied from the BaFin company search): only companies whose
    address is in companies.import_postcodes (60000–65999), head offices only.
    None if the text has (almost) no postcodes: then it's an ordinary list."""
    low, high = CONFIG["companies"]["import_postcodes"]
    lines = [line.strip() for line in text.splitlines()]
    hits = [(i, m) for i, line in enumerate(lines) if (m := POSTCODE.search(line))]
    if len(hits) < 3:
        return None
    names, outside, branches = [], 0, 0
    for i, m in hits:
        # The name: the first cell before the postcode that isn't a street ...
        cells = [c.strip(" ,;|") for c in re.split(r"\t|;|\||,", lines[i][:m.start()])]
        name = next((c for c in cells if len(c) >= 3 and not STREET.search(c)), None)
        # ... or the nearest line above that isn't an address
        for j in range(i - 1, max(-1, i - 4), -1):
            if name:
                break
            above = lines[j]
            if len(above) >= 3 and not STREET.search(above) and not POSTCODE.search(above):
                name = above.split("\t")[0].strip()
        if not name:
            continue
        if not low <= int(m[1]) <= high:
            outside += 1
        elif BRANCH_OFFICE.search(name):
            branches += 1
        else:
            names.append(BULLET.sub("", name))
    how = f"addresses in postcodes {low}–{high}: {len(names)} kept, {outside} outside left out"
    return list(dict.fromkeys(names)), how + (f", {branches} branch offices left out" if branches else "")


def read_names(text: str) -> tuple[list[str], str]:
    """Names from what you pasted: a link, a list with addresses, a clean list, or messy text.
    Returns (names, how)."""
    text = (text or "").strip()
    if re.fullmatch(r"https?://\S+", text):
        names, note = names_from_url(text)
        return names, "AI read the page" + (f", {note}" if note else "")
    if found := postcode_names(text):
        return found
    if (names := _plain_list(text)) is not None:
        return names, "one name per line"
    return ai_names(text[:CHUNK * CONFIG["companies"]["import_max_ai_requests"]], "pasted text"), "AI read the text"


# ---- adding them ---------------------------------------------------------------------

def _skipped_keys() -> list[str]:
    return [k for k in map(company_key, db.skipped_company_names()) if len(k) >= 2]


def add_companies(records: list[dict], source: str) -> dict:
    """Merge the records into the companies table (like the normal collecting step).
    Returns {"ids": ids to run through the pipeline, "new", "known", "skipped", "dropped"}."""
    skipped = _skipped_keys()
    clean_records, seen, dropped, was_skipped = [], set(), 0, 0
    for r in records:
        name = clean_name(r["name"])
        key = company_key(name)
        if len(key) < 2 or len(name) > 100 or NOT_A_COMPANY.search(name):
            dropped += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        if any(_same(key, k) for k in skipped):
            was_skipped += 1
            continue
        clean_records.append({**r, "name": name, "source": source})
    before = {c["name_key"]: c for c in db.company_rows()}
    rows = merge(clean_records, {source})
    new = db.save_companies(rows)
    keys = [r["name_key"] for r in rows if source in json.loads(r["sources"])]
    found = db.companies_by_key(keys)
    for c in found:  # say why it's here, on companies that are new to the list
        note = next((r.get("note") for r in clean_records if r.get("note") and _same(company_key(r["name"]), c["name_key"])), None)
        if note and c["name_key"] not in before:
            db.update_company(c["id"], {"origin_note": note})
    ids = {c["id"] for c in found if c["status"] != "skipped"}
    return {"ids": ids, "new": new, "known": len(found) - new, "skipped": was_skipped, "dropped": dropped}


def import_list(text: str, label: str, progress: dict) -> set[int]:
    progress["step"] = "Reading the company names…"
    names, how = read_names(text)
    if not names:
        raise ImportProblem("No company names found.")
    result = add_companies([{"name": n} for n in names], source_key(label))
    progress.update(collected=len(result["ids"]), new=result["new"])
    log.info("Import \"%s\": %d names (%s): %d new companies, %d known, %d skipped before, %d not a company",
             label, len(names), how, result["new"], result["known"], result["skipped"], result["dropped"])
    progress["notes"].append(f"Import \"{label}\": {len(names)} names ({how}), {result['new']} new companies, "
                                f"{result['known']} already known"
                                + (f", {result['skipped']} skipped before (left out)" if result["skipped"] else ""))
    return result["ids"]


# ---- look-alikes -------------------------------------------------------------------------

LOOKALIKE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"name": {"type": "STRING"}, "website": {"type": "STRING"},
                             "city": {"type": "STRING"}, "similar_to": {"type": "STRING"}},
              "required": ["name", "website", "city", "similar_to"]},
}
LOOKALIKE_PROMPT = """THE PERSON
{profile}

The person saved or applied to jobs at these companies:
{liked}

Suggest up to {n} OTHER companies that are similar to these (same kind of business and IT work) and have
an office within about 25 km of Darmstadt or Frankfurt am Main, Germany.
- Prefer small and mid-size companies (under about 1,000 employees), not famous big brands.
- Only real companies you are sure about. No staffing agencies or recruiters.
- website: the official homepage, only if you are sure, else "". city: the office near Darmstadt / Frankfurt.
- similar_to: which company from the list above it is most like.
Do NOT suggest any of these: {excluded}
"""


def lookalikes_due() -> bool:
    last = db.get_setting("lookalikes_at")
    if not last:
        return True
    return datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(days=CONFIG["companies"]["lookalikes_every_days"])


def find_lookalikes(progress: dict) -> set[int]:
    liked = db.liked_employers()[:15]
    if not liked:
        raise ImportProblem("Save or apply to a few jobs first: look-alikes are based on those companies.")
    if not ai.enabled():
        raise ImportProblem("Look-alikes need the AI, but it is off (no AI key in .env).")
    progress["step"] = "Asking the AI for similar companies…"
    known = {c["name"] for c in db.company_rows() if c["status"] in ("watched", "initiativ")}
    excluded = sorted(set(db.skipped_company_names()) | {n for n, _ in liked} | known)
    prompt = LOOKALIKE_PROMPT.format(profile=ai.settings()["profile"].strip(), n=CONFIG["companies"]["max_lookalikes"],
                                     liked="\n".join(f"- {c} (e.g. {t})" for c, t in liked),
                                     excluded=", ".join(excluded[:150]) or "(none)")
    _pace()
    try:
        answer = ai.ask_json(prompt, LOOKALIKE_SCHEMA, temperature=0.5, priority="jobs")
    except (ai.RateLimited, ai.Overloaded):
        raise ImportProblem("The AI's free limit is used up right now. Try again in 10 minutes.")
    db.set_setting("lookalikes_at", db.now())
    liked_keys = [company_key(n) for n, _ in liked]
    records = []
    for a in answer if isinstance(answer, list) else []:
        name = clean(a.get("name"), 100)
        if not name or any(_same(company_key(name), k) for k in liked_keys):
            continue
        site = clean(a.get("website"), 200)
        records.append({"name": name, "city": clean(a.get("city"), 60) or None,
                        "website": site or None, "hint_from": "ai" if site else None,
                        "note": f"like {clean(a.get('similar_to'), 60)}" if a.get("similar_to") else None})
    result = add_companies(records, LOOKALIKE)
    progress.update(collected=len(result["ids"]), new=result["new"])
    log.info("Look-alikes: the AI suggested %d, %d new companies, %d known, %d skipped before",
             len(records), result["new"], result["known"], result["skipped"])
    progress["notes"].append(f"Look-alikes: {result['new']} new companies"
                                + (f", {result['known']} already known" if result["known"] else ""))
    return result["ids"]
