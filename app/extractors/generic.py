"""Finds job postings on an ordinary HTML career page.

Two strategies:
1. Structured data (schema.org "JobPosting"), which many sites add for Google Jobs.
2. Links that look like job ads: by their text ("Werkstudent Softwareentwicklung (m/w/d)")
   or by their address (".../stelle/software-engineer-m-w-d/", ".../job/12345").
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import Job, clean, html_to_text, to_iso

# "(m/w/d)", "(w/m/d)", "(f/m/x)", "(all genders)", "Entwickler*in", ...
GENDER = re.compile(r"\(\s*(?:[mwfdxih]\s*[/|,]\s*){1,3}[mwfdxih]\s*\)|\(\s*all genders?\s*\)|\(\s*gn\s*\*?\s*\)|"
                    r"\b[mwfd]\s*/\s*[mwfd]\s*/\s*[mwfdx]\b|\w\*in\b", re.I)
# The same in an address: ".../software-engineer-m-w-d-ai/", ".../werkstudent-mwd"
GENDER_SLUG = re.compile(r"[-_/](?:[mwfd]-[mwfd]-[mwfdx]|mwd|wmd|mfd|all-genders?)(?:[-_/.?]|$)", re.I)
JOB_WORD = re.compile(
    r"werkstudent|student|praktik|\bintern\b|internship|trainee|ausbildung|duales studium|"
    r"entwickler|developer|engineer|ingenieur|informatiker|programmier|consultant|berater|"
    r"analyst|architekt|architect|administrator|manager|specialist|spezialist|scientist|"
    r"designer|techniker|referent|sachbearbeiter|mitarbeiter|devops|software|teilzeit|vollzeit|"
    r"hilfskraft|werkstudium|abschlussarbeit|bachelorarbeit|masterarbeit|thesis|azubi|aushilfe|minijob",
    re.I,
)
JOB_HREF = re.compile(
    r"/(?:jobs?|stellen?\w*|stellenangebot\w*|karriere|careers?|positions?|vacanc\w*|"
    r"job-?offers?|openings?|ausschreibung\w*|offene-stellen|jobdetail\w*)(?:[/\-_?.#]|$)|"
    r"[?&](?:job_?id|jobid|stelle|id)=",
    re.I,
)
# Real job ad links almost always carry an id number (".../job/12345") or a long
# title slug (".../werkstudent-software-entwicklung-frankfurt"). Menu links and
# "related searches" ("/jobs/it-berater/in-darmstadt") don't.
JOB_ID = re.compile(r"\d{4,}|[a-z0-9]+(?:-[a-z0-9]+){4,}", re.I)
# Addresses that only ever hold ONE job ad: ".../stelle/<title>/", ".../job/<id>". Unlike
# "/jobs/" or "/karriere/", which are also lists and menus ("/karriere/benefits").
ONE_JOB_HREF = re.compile(
    r"/(?:stelle|stellenangebot|stellenanzeige|stellenausschreibung|job|vacancy|position|jobdetails?|job-detail)"
    r"/(?:[^/?#]*-[^/?#]+|[^/?#]*\d{3,}[^/?#]*)", re.I)
# "Initiativbewerbung": a link on every careers page, but not an open job
NOT_A_JOB = re.compile(r"initiativ|unsolicited|speculative|blindbewerbung|open application", re.I)
# Articles about jobs, not job ads: ".../karriere/blog/von-der-azubine-zur-ausbilderin.html"
ARTICLE_HREF = re.compile(r"/(?:blogs?|magazin|magazine|news|newsroom|presse?|aktuelles|stories|events?)/", re.I)
# Words that show a page lists open jobs (stricter than JOB_WORD: "Software" is on every IT site).
# Only then the AI is asked to read the job list.
HIRING = re.compile(r"werk[\s-]*student|praktik|trainee|ausbildung|abschlussarbeit|stellenangebot|offene stellen|"
                    r"stellenausschreibung|open positions|open roles|job openings|wir suchen|we.re hiring", re.I)
NAV_TEXT = {
    "jobs", "karriere", "career", "careers", "stellenangebote", "offene stellen", "alle jobs",
    "alle stellen", "jobs anzeigen", "zu den jobs", "mehr erfahren", "jetzt bewerben",
    "apply now", "view all jobs", "see all jobs", "learn more", "details", "mehr",
    "weiterlesen", "read more", "jobs & karriere", "studenten", "students",
}


def site_name(soup: BeautifulSoup, url: str) -> str:
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return clean(og["content"], 80)
    # "jobs.dzbank.de" -> "dzbank.de"
    return re.sub(r"^(www|jobs?|karriere|careers?|stellen|bewerbung)\.", "", urlparse(url).netloc)


def extract(html: str, page_url: str) -> tuple[list[Job], str]:
    """Returns (jobs, site name)."""
    soup = BeautifulSoup(html, "html.parser")
    name = site_name(soup, page_url)
    jobs: dict[str, Job] = {}
    for job in _from_json_ld(soup, page_url) + _from_links(soup, page_url, name):
        jobs.setdefault(job.url, job)
    return list(jobs.values()), name


def _walk(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _walk(item)
    elif isinstance(obj, dict):
        types = obj.get("@type")
        if "JobPosting" in (types if isinstance(types, list) else [types]):
            yield obj
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in obj:
                yield from _walk(obj[key])


def _from_json_ld(soup: BeautifulSoup, page_url: str) -> list[Job]:
    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "", strict=False)
        except ValueError:
            continue
        for p in _walk(data):
            org = p.get("hiringOrganization")
            company = org.get("name", "") if isinstance(org, dict) else (org or "")
            places = p.get("jobLocation") or []
            places = places if isinstance(places, list) else [places]
            cities = [
                (pl.get("address") or {}).get("addressLocality", "")
                for pl in places if isinstance(pl, dict) and isinstance(pl.get("address"), dict)
            ]
            employment = p.get("employmentType") or ""
            jobs.append(Job(
                title=clean(p.get("title")), url=urljoin(page_url, p.get("url") or page_url),
                company=clean(company), location=", ".join(filter(None, cities)),
                description=html_to_text(p.get("description")),
                employment=", ".join(employment) if isinstance(employment, list) else employment,
                posted=to_iso(p.get("datePosted")),
            ))
    return [j for j in jobs if j.title]


HEADINGS = ["h1", "h2", "h3", "h4", "h5", "strong"]


def _link_title(a) -> str:
    heading = a.find(HEADINGS)
    text = clean((heading or a).get_text(" "), 200)
    if len(text) >= 6 and text.lower() not in NAV_TEXT:
        return text
    # An empty link laid over a job card (<a class="full-link" title="...">): its title attribute
    for attr in ("title", "aria-label"):
        label = clean(a.get(attr), 200)
        if len(label) >= 6 and label.lower() not in NAV_TEXT:
            return label
    # ... or the heading of the card around it, as long as the card holds only this one link
    node = a.parent
    for _ in range(3):
        if node is None or node.name in ("body", "html") or len({x["href"] for x in node.find_all("a", href=True)}) > 1:
            break
        heading = node.find(HEADINGS)
        if heading and len(label := clean(heading.get_text(" "), 200)) >= 6:
            return label
        node = node.parent
    return text


def looks_like_job(title: str, url: str) -> bool:
    """A job ad link: a job title ("... (m/w/d)", "Werkstudent ...") and / or a job ad address."""
    path = urlparse(url).path
    if NOT_A_JOB.search(title) or NOT_A_JOB.search(path) or ARTICLE_HREF.search(path):
        return False
    if GENDER.search(title) or (GENDER_SLUG.search(path) and JOB_HREF.search(url)):
        return True
    if ONE_JOB_HREF.search(path):
        return True
    return bool(JOB_WORD.search(title) and JOB_HREF.search(url) and JOB_ID.search(url))


def _resolve(a, page_url: str) -> str:
    href = a.get("href", "").strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return ""
    url = urljoin(page_url, href).split("#")[0]
    return url if url.startswith("http") else ""


def _from_links(soup: BeautifulSoup, page_url: str, company: str) -> list[Job]:
    page_no_fragment = page_url.split("#")[0]
    found = []
    for a in soup.find_all("a", href=True):
        url = _resolve(a, page_url)
        if not url or url == page_no_fragment:
            continue
        title = _link_title(a)
        if len(title) < 6 or title.lower() in NAV_TEXT:
            continue
        if looks_like_job(title, url):
            found.append((a, Job(title=title, url=url, company=company)))

    job_urls = {job.url for _, job in found}
    for a, job in found:
        _fill_from_card(_card(a, job_urls, page_url), a, job)
    return [job for _, job in found]


# ---- reading company / location / date from the "card" around a job link ------

FIELD_HINTS = {
    "company": ("company", "employer", "arbeitgeber", "firma", "hiringorganization"),
    "location": ("location", "standort", "city", "ort", "place", "joblocation"),
    "posted": ("timeago", "date", "posted", "published", "datum", "dateposted"),
}
FIELD_LABELS = {
    "standort", "location", "ort", "einsatzort", "arbeitsort", "datum", "date", "unternehmen",
    "company", "firma", "arbeitgeber", "employer", "posted", "veröffentlicht", "veröffentlicht am",
}


def _card(a, job_urls: set[str], page_url: str):
    """The biggest element around the link that still belongs to only this one job."""
    node = a
    for _ in range(6):
        parent = node.parent
        if parent is None or parent.name in ("body", "html"):
            break
        urls = {_resolve(x, page_url) for x in parent.find_all("a", href=True)} & job_urls
        if len(urls) > 1 or len(parent.get_text()) > 3000:
            break
        node = parent
    return node


def _hint(el) -> str | None:
    attrs = " ".join([el.get("data-at") or "", el.get("data-testid") or "", el.get("itemprop") or "",
                      " ".join(el.get("class") or [])])
    tokens = [t for t in re.split(r"[^a-z]+", attrs.lower()) if t and "logo" not in t]
    for field, hints in FIELD_HINTS.items():
        if any(t == h or (len(h) > 4 and h in t) for t in tokens for h in hints):
            return field
    return None


def _fill_from_card(card, a, job: Job) -> None:
    if card is a:
        return
    time_tag = card.find("time")
    if time_tag:
        job.posted = to_iso(time_tag.get("datetime") or time_tag.get_text(" "))
    found: dict[str, str] = {}
    for el in card.find_all(True):
        if el is a or a in el.parents:
            continue
        field = _hint(el)
        text = clean(el.get_text(" "), 120)
        if text.lower().rstrip(": ") in FIELD_LABELS:  # the label "Standort", not the value
            continue
        if field and text and field not in found:
            found[field] = text
    if "company" in found:  # the card's company beats the website's name
        job.company = found["company"]
    if "location" in found:
        job.location = found["location"]
    if "posted" in found and not job.posted:
        job.posted = to_iso(found["posted"])
    if not job.description:  # the card's teaser text helps the filter (hours, keywords)
        job.description = clean(card.get_text(" "), 1500)
