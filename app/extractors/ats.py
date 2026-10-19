"""Applicant tracking systems (ATS) with public job feeds.

Many companies don't host their own job list but use a service like Personio or
Workday. Those services offer official JSON/XML feeds, which are far more reliable
than reading the HTML page. We recognise them from the link itself or from links
embedded in a company's career page.
"""
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlparse

from ..fetcher import fetch_api
from . import Job, clean, html_to_text, relative_date, to_iso


@dataclass
class Board:
    system: str          # e.g. "Personio"
    key: str             # the company's id inside that system
    extra: dict = field(default_factory=dict)


def detect(text: str) -> Board | None:
    """Find an ATS board in a URL or in a page's HTML."""
    text = text.replace("\\/", "/")  # links inside JSON data are written as https:\/\/...
    if m := re.search(r"partner\.studysmarter\.de/careerkit/jobs(\?[^\s\"'<>]*)?", text, re.I):
        query = {k: v[0] for k, v in parse_qs((m[1] or "?")[1:]).items()}
        return Board("StudySmarter", "careerkit", query)
    if m := re.search(r"([a-z0-9-]+)\.jobs\.personio\.(de|com)", text, re.I):
        return Board("Personio", m[1].lower(), {"tld": m[2].lower()})
    if m := re.search(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)", text, re.I):
        return Board("Greenhouse", m[1])
    if m := re.search(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed\b)([\w-]+)", text, re.I):
        return Board("Greenhouse", m[1])
    if m := re.search(r"jobs\.(eu\.)?lever\.co/([\w.-]+)", text, re.I):
        return Board("Lever", m[2], {"eu": bool(m[1])})
    if m := re.search(r"(?:jobs|careers)\.smartrecruiters\.com/(?!oneclick|sr-jobs)([\w-]+)", text, re.I):
        return Board("SmartRecruiters", m[1])
    if m := re.search(r"\b(?!www\.|app\.|api\.)([a-z0-9-]+)\.recruitee\.com", text, re.I):
        return Board("Recruitee", m[1].lower())
    for m in re.finditer(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:([a-z]{2}-[A-Z]{2})/)?([\w-]+)", text):
        if m[4] != "wday":
            return Board("Workday", m[1], {"wd": m[2], "lang": m[3] or "", "site": m[4]})
    return None


def search_terms(url: str) -> str:
    """If the link contains a search (e.g. ?q=Werkstudent), reuse it for the feed."""
    query = parse_qs(urlparse(url).query)
    for key in ("q", "query", "search", "searchText", "keyword", "keywords"):
        if query.get(key):
            return query[key][0]
    return ""


def fetch_jobs(board: Board, search: str = "") -> list[Job]:
    return FETCHERS[board.system](board, search)


def _personio(b: Board, search: str) -> list[Job]:
    base = f"https://{b.key}.jobs.personio.{b.extra['tld']}"
    root = ET.fromstring(fetch_api(f"{base}/xml?language=de", as_json=False))
    jobs = []
    for pos in root.iter("position"):
        get = lambda tag: (pos.findtext(tag) or "").strip()
        description = " ".join(html_to_text(d.findtext("value")) for d in pos.iter("jobDescription"))
        employment = ", ".join(filter(None, [get("employmentType"), get("schedule")]))
        jobs.append(Job(
            title=clean(get("name")), url=f"{base}/job/{get('id')}",
            company=get("subcompany") or b.key, location=get("office"),
            description=description[:5000], employment=employment.replace("_", " "),
            posted=to_iso(get("createdAt")),
        ))
    return jobs


def _greenhouse(b: Board, search: str) -> list[Job]:
    data = fetch_api(f"https://boards-api.greenhouse.io/v1/boards/{b.key}/jobs?content=true")
    return [
        Job(
            title=clean(j["title"]), url=j["absolute_url"],
            company=j.get("company_name") or b.key,
            location=(j.get("location") or {}).get("name", ""),
            description=html_to_text(html.unescape(j.get("content") or "")),
            posted=to_iso(j.get("first_published")),
        )
        for j in data.get("jobs", [])
    ]


def _lever(b: Board, search: str) -> list[Job]:
    api = "api.eu.lever.co" if b.extra.get("eu") else "api.lever.co"
    data = fetch_api(f"https://{api}/v0/postings/{b.key}?mode=json")
    jobs = []
    for p in data:
        cats = p.get("categories") or {}
        jobs.append(Job(
            title=clean(p["text"]), url=p["hostedUrl"], company=b.key,
            location=cats.get("location") or "", description=clean(p.get("descriptionPlain"), 5000),
            employment=cats.get("commitment") or "", posted=to_iso(p.get("createdAt")),
        ))
    return jobs


def _smartrecruiters(b: Board, search: str) -> list[Job]:
    url = f"https://api.smartrecruiters.com/v1/companies/{b.key}/postings?limit=100"
    if search:
        url += f"&q={quote(search)}"
    jobs = []
    for p in fetch_api(url).get("content", []):
        loc = p.get("location") or {}
        jobs.append(Job(
            title=clean(p["name"]), url=f"https://jobs.smartrecruiters.com/{b.key}/{p['id']}",
            company=(p.get("company") or {}).get("name") or b.key,
            location=loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("country")])),
            employment=(p.get("typeOfEmployment") or {}).get("label") or "",
            posted=to_iso(p.get("releasedDate")),
        ))
    return jobs


def _recruitee(b: Board, search: str) -> list[Job]:
    jobs = []
    for o in fetch_api(f"https://{b.key}.recruitee.com/api/offers/").get("offers", []):
        employment = (o.get("employment_type_code") or "").replace("_", " ")
        if o.get("min_hours") or o.get("max_hours"):
            employment += f" ({o.get('min_hours') or '?'}-{o.get('max_hours') or '?'} h/week)"
        jobs.append(Job(
            title=clean(o["title"]), url=o.get("careers_url") or o.get("url") or "",
            company=o.get("company_name") or b.key, location=o.get("location") or o.get("city") or "",
            description=html_to_text(o.get("description")), employment=employment.strip(),
            posted=to_iso(o.get("published_at") or o.get("created_at")),
        ))
    return jobs


def _workday(b: Board, search: str) -> list[Job]:
    host = f"{b.key}.{b.extra['wd']}.myworkdayjobs.com"
    api = f"https://{host}/wday/cxs/{b.key}/{b.extra['site']}/jobs"
    prefix = f"https://{host}/" + (f"{b.extra['lang']}/" if b.extra["lang"] else "") + b.extra["site"]
    jobs, offset = [], 0
    while offset < 100:  # newest 100 jobs is plenty; big companies have thousands
        body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search}
        postings = fetch_api(api, method="POST", body=body).get("jobPostings", [])
        for p in postings:
            if p.get("title"):
                jobs.append(Job(
                    title=clean(p["title"]), url=prefix + p.get("externalPath", ""),
                    company=b.key.capitalize(), location=p.get("locationsText") or "",
                    posted=relative_date(p.get("postedOn")),  # e.g. "Posted 3 Days Ago"
                ))
        if len(postings) < 20:
            break
        offset += 20
    return jobs


def _studysmarter(b: Board, search: str) -> list[Job]:
    """StudySmarter CareerKit job search (a student job board). Uses the same data feed
    as their website; the search words and city come from the link you added."""
    params = {"limit": 100, "q": b.extra.get("q", ""), "location": b.extra.get("city", ""),
              "radius": b.extra.get("radius", 50), "smart": 1}
    data = fetch_api("https://partner.studysmarter.de/careerkit/api/jobboard/feed", params=params,
                     headers={"Referer": "https://partner.studysmarter.de/careerkit/jobs"})
    jobs = []
    for r in data.get("results", []):
        url = r.get("studysmarter_url") or r.get("apply_url")
        if not url or not r.get("title"):
            continue
        place = r.get("city") or ""
        if r.get("work_model") in ("hybrid", "remote"):
            place = f"{place} ({r['work_model']})".strip()
        jobs.append(Job(
            title=clean(r["title"]), url=url, company=clean(r.get("company")), location=place,
            employment=r.get("listing_type") or "",
            posted=to_iso(r.get("posted_at")) if r.get("posted_known") else "",
        ))
    return jobs


FETCHERS = {
    "StudySmarter": _studysmarter,
    "Personio": _personio,
    "Greenhouse": _greenhouse,
    "Lever": _lever,
    "SmartRecruiters": _smartrecruiters,
    "Recruitee": _recruitee,
    "Workday": _workday,
}
