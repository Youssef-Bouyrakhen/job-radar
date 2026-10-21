"""Applicant tracking systems (ATS) with public job feeds.

Many companies don't host their own job list but use a service like Personio or
Workday. Those services offer official JSON/XML feeds, which are far more reliable
than reading the HTML page. We recognise them from the link itself or from links
embedded in a company's career page.
"""
import base64
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx

from ..fetcher import API_HEADERS, _polite_wait, fetch_api, robots_allows
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
    if m := re.search(r"\b([a-z0-9-]+)\.powerappsportals\.com(/[a-z]{2}-[A-Z]{2}/[\w/-]*)?", text, re.I):
        key = m[1].lower()  # Microsoft Power Pages (e.g. C24 Bank); the page with the job list, if the link says
        path = re.sub(r"job-overview.*", "", m[2] or "/")
        return Board("PowerApps", key, {"page": f"https://{key}.powerappsportals.com{path}"})
    # Oracle Recruiting: on Oracle's own host, or on the company's (jobs.sana.de) with Oracle's host in the page
    if (m := re.search(r"\b([\w-]+\.fa\.(?:[\w-]+\.)*oraclecloud\.com)", text, re.I)) \
            and (s := re.search(r"/(?:hcmUI/CandidateExperience/)?([a-z]{2}(?:-[A-Z]{2})?)/sites/([\w-]+)", text)):
        return Board("Oracle", m[1].lower(), {"lang": s[1], "site": s[2]})
    # SuccessFactors job site (jobs.adesso-group.com/search/?q=&locale=de_DE). Not the classic
    # career*.successfactors.eu pages: their robots.txt asks automated visitors not to read them.
    if m := re.search(r"https?://([\w.-]+?)((?:/[\w-]+)?)/search/\?[^\s\"'<>]*?locale=([a-z]{2}_[A-Z]{2})", text):
        return Board("SuccessFactors", m[1].lower(), {"path": m[2], "locale": m[3]})
    return None


def detect_job_site(url: str, html_text: str) -> Board | None:
    """A SuccessFactors job site recognised from the page itself (its files come from rmkcdn)."""
    if not re.search(r"rmkcdn\.successfactors|jobs2web", html_text[:200000]):
        return None
    p = urlparse(url)
    path = re.split(r"/(?:search|job)/", p.path)[0].rstrip("/") if re.search(r"/(?:search|job)/", p.path) else ""
    locale = re.search(r"locale=([a-z]{2}_[A-Z]{2})", html_text)
    return Board("SuccessFactors", p.netloc.lower(), {"path": path, "locale": locale[1] if locale else "de_DE"})


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


def _polite_get(client: httpx.Client, url: str) -> httpx.Response:
    if not robots_allows(url):
        raise PermissionError(f"robots.txt asks automated visitors not to read {urlparse(url).netloc}")
    _polite_wait(url, 2)
    r = client.get(url)
    r.raise_for_status()
    return r


GRID = re.compile(r"<div[^>]*class=\"entity-grid entitylist\"[^>]*>")


def _powerapps(b: Board, search: str) -> list[Job]:
    """Power Pages job portal: its page loads the list from /_services/entity-grid-data.json.
    We ask the same address the page asks, all rows at once."""
    with httpx.Client(headers=API_HEADERS, timeout=30, follow_redirects=True) as client:
        page = _polite_get(client, b.extra.get("page") or f"https://{b.key}.powerappsportals.com/")
        grid = GRID.search(page.text)
        if not grid:  # the homepage: follow its link to the job list
            here = urlparse(str(page.url)).netloc
            links = [urljoin(str(page.url), h) for h in re.findall(r'href="([^"#]+)"', page.text)
                     if re.search(r"vacanc|stellen|jobs|karriere|career", h, re.I)]
            for link in dict.fromkeys(l for l in links if urlparse(l).netloc == here):
                page = _polite_get(client, link)
                if grid := GRID.search(page.text):
                    break
        if not grid:
            return []
        get_url = urljoin(str(page.url), re.search(r'data-get-url="([^"]+)"', grid[0])[1])
        layout = json.loads(base64.b64decode(re.search(r"data-view-layouts='([^']+)'", grid[0])[1]))[0]
        config = layout["Configuration"]
        token = re.search(r'value="([^"]+)"', _polite_get(client, urljoin(str(page.url), "/_layout/tokenhtml")).text)[1]
        body = {"base64SecureConfiguration": layout["Base64SecureConfiguration"],
                "sortExpression": layout.get("SortExpression") or "", "search": search or None, "page": 1,
                "pageSize": 150, "pagingCookie": "", "filter": None, "metaFilter": None, "timezoneOffset": -60,
                "customParameters": [], "entityName": config["EntityName"], "entityId": None}
        _polite_wait(get_url, 2)
        r = client.post(get_url, json=body, headers={"__RequestVerificationToken": token,
                                                     "X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
    details = ((config.get("DetailsActionLink") or {}).get("URL") or {}).get("PathWithQueryString")
    columns = [c.get("LogicalName", "") if isinstance(c, dict) else str(c) for c in layout.get("Columns") or []]
    jobs = []
    for rec in r.json().get("Records", []):
        values = {a["Name"]: a.get("DisplayValue") for a in rec.get("Attributes", [])}
        values = {k: (v.get("Name") if isinstance(v, dict) else v) for k, v in values.items()}
        title = (values.get(columns[0]) if columns else None) \
            or next((v for k, v in values.items() if "title" in k or k.endswith("name")), "")
        pick = lambda *words: next((str(v) for k, v in values.items() if v and any(w in k for w in words)), "")
        url = urljoin(str(page.url), f"{details}?id={rec['Id']}") if details else f"{page.url}#job-{rec['Id']}"
        if title:
            jobs.append(Job(title=clean(str(title)), url=url, company="", location=pick("location", "city"),
                            employment=", ".join(filter(None, [pick("workingtime", "schedule"), pick("contract")]))))
    return jobs


def _oracle(b: Board, search: str) -> list[Job]:
    """Oracle Recruiting (Candidate Experience): its public job-search API."""
    api = f"https://{b.key}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    public = f"https://{b.key}/hcmUI/CandidateExperience/{b.extra['lang']}/sites/{b.extra['site']}"
    jobs, offset = [], 0
    while offset < 100:  # the newest 100
        finder = f"findReqs;siteNumber={b.extra['site']},limit=25,offset={offset},sortBy=POSTING_DATES_DESC"
        if search:
            finder += f',keyword="{search}"'
        data = fetch_api(api, params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder})
        reqs = (data.get("items") or [{}])[0].get("requisitionList") or []
        for r in reqs:
            if r.get("Title"):
                jobs.append(Job(title=clean(r["Title"]), url=f"{public}/job/{r['Id']}", company="",
                                location=clean(r.get("PrimaryLocation")),
                                employment=", ".join(filter(None, [r.get("JobSchedule"), r.get("WorkerType")])),
                                posted=(r.get("PostedDate") or "")[:10]))
        if len(reqs) < 25:
            break
        offset += 25
    return jobs


SF_ROW = re.compile(r'<tr class="data-row".*?</tr>', re.S)
SF_LINK = re.compile(r'<a(?=[^>]*class="jobTitle-link")[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def _successfactors(b: Board, search: str) -> list[Job]:
    """SuccessFactors job site: its normal search pages (25 jobs each), like a visitor paging through."""
    base = f"https://{b.key}{b.extra.get('path', '')}"
    text = lambda s: clean(html.unescape(re.sub(r"<[^>]+>", " ", s)))
    jobs, start = {}, 0
    with httpx.Client(headers=API_HEADERS, timeout=30, follow_redirects=True) as client:
        while start < 150:
            page = _polite_get(client, f"{base}/search/?q={quote(search)}&locale={b.extra.get('locale', 'de_DE')}"
                                       f"&startrow={start}")
            rows = SF_ROW.findall(page.text)
            for row in rows:
                if not (link := SF_LINK.search(row)):
                    continue
                place = re.search(r'class="jobLocation"[^>]*>(.*?)</span>', row, re.S)
                url = urljoin(str(page.url), html.unescape(link[1]))
                jobs[url] = Job(title=text(link[2]), url=url, company="", location=text(place[1]) if place else "")
            if not rows:
                break
            start += len(rows)
    return list(jobs.values())


FETCHERS = {
    "StudySmarter": _studysmarter,
    "Personio": _personio,
    "Greenhouse": _greenhouse,
    "Lever": _lever,
    "SmartRecruiters": _smartrecruiters,
    "Recruitee": _recruitee,
    "Workday": _workday,
    "PowerApps": _powerapps,
    "Oracle": _oracle,
    "SuccessFactors": _successfactors,
}
