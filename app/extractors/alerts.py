"""Reads the jobs out of one job-alert email (Indeed, StepStone, LinkedIn).

Every job in such an email is a "card": a link to the job (often several: logo, title,
button) plus a few lines of text - title, company, location, sometimes a teaser and
"vor 2 Tagen". We find the job links, group them per job, and read the card around them.
"""
import base64
import gzip
import re
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from . import Job, clean, relative_date, to_iso

LINKEDIN = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})", re.I)
STEPSTONE = re.compile(r"stepstone\.de/stellenangebote--([^?#\s]+?)--(\d{5,})-inline\.html", re.I)
INDEED_HOST = re.compile(r"(^|\.)indeed\.(com|de)$", re.I)
INDEED_JK = re.compile(r"(?:\b|_)v?jk[=:\"' ]+([0-9a-f]{16})\b", re.I)
URL_IN_TEXT = re.compile(r"https?%3A%2F%2F[^&\"'\s]+|https?://[^&\"'\s<>]+", re.I)

# Card lines that are buttons or badges, not information.
NOISE = re.compile(
    r"^(neu|new|jetzt bewerben|bewerben|einfach bewerben|schnell bewerben|easy apply|apply( now)?|"
    r"view job|job ansehen|stelle ansehen|ansehen|mehr (anzeigen|erfahren)|details|promoted|gesponsert|"
    r"anzeige|sponsored|top[- ]?job|actively recruiting|aktiv auf der suche|be an early applicant|"
    r"frühzeitig bewerben|in deinem netzwerk|alumni|verified|verifiziert|\W*)$", re.I)
# Links we must never follow, even to find out where they lead.
DANGEROUS = re.compile(r"unsubscri|abmeld|abbestell|optout|opt-out|lösch|loesch|delete|deaktiv|kündig|"
                       r"pause|stop|settings|einstellung|preferences|verwalten|manage", re.I)
# A card line that says when the job was posted ("vor 2 Tagen", "Gerade veröffentlicht"),
# so "15 € pro Stunde" or "20 Stunden pro Woche" aren't mistaken for a date.
POSTED_HINT = re.compile(r"^(vor |seit )|\bago\b|heute|today|gestern|yesterday|gerade|just posted|"
                         r"veröffentlicht|posted", re.I)
MAX_CARD_CHARS = 1500


def site_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for name in ("LinkedIn", "StepStone", "Indeed"):
        if name.lower() in host:
            return name
    return ""


def job_url(href: str) -> str | None:
    """The real, short job link behind a (tracking) link from an alert email, or None if
    it isn't a job link (unsubscribe, settings, app store, ...). Works without the internet."""
    if not href or not href.lower().startswith("http"):
        return None
    m = LINKEDIN.search(href)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m[1]}/"
    m = STEPSTONE.search(href)
    if m:
        return f"https://www.stepstone.de/stellenangebote--{m[1]}--{m[2]}-inline.html"
    parsed = urlparse(href)
    if INDEED_HOST.search(parsed.netloc):
        jk = (parse_qs(parsed.query).get("jk") or parse_qs(parsed.query).get("vjk") or [""])[0]
        if re.fullmatch(r"[0-9a-f]{16}", jk, re.I):
            return f"https://de.indeed.com/viewjob?jk={jk.lower()}"
    # A tracking link with the real link inside, e.g. "...?url=https%3A%2F%2Fwww.linkedin.com..."
    for inner in URL_IN_TEXT.findall(href[8:]):
        found = job_url(unquote(inner))
        if found:
            return found
    # Indeed's newer tracking links carry the target zipped and base64-encoded ("H4sI...").
    for blob in re.findall(r"H4sI[A-Za-z0-9_\-+/=]{20,}", href):
        try:
            text = gzip.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))).decode("utf-8", "ignore")
        except Exception:
            continue
        m = INDEED_JK.search(text)
        if m and "indeed" in href.lower():
            return f"https://de.indeed.com/viewjob?jk={m[1].lower()}"
        for inner in URL_IN_TEXT.findall(text):
            found = job_url(unquote(inner))
            if found:
                return found
    return None


def could_be_job_link(href: str, text: str) -> bool:
    """A tracking link we can't decode offline, but which may lead to a job: it's from one of
    the three job sites, its text looks like a job title, and it is not an unsubscribe/settings link."""
    if site_of(href) == "" or DANGEROUS.search(href) or DANGEROUS.search(text):
        return False
    return 6 <= len(text) <= 150 and not NOISE.match(text)


def _lines(element) -> list[str]:
    lines = []
    for raw in element.get_text("\n").split("\n"):
        line = clean(raw, 400)
        if line and not NOISE.match(line) and (not lines or lines[-1] != line):
            lines.append(line)
    return lines


def _card(anchors: list, other_ids: set[int]):
    """The biggest element around this job's links that holds no other job's links."""
    card = anchors[0]
    while card.parent is not None and card.parent.name not in ("body", "html", "[document]"):
        parent = card.parent
        if any(id(a) in other_ids for a in parent.find_all("a")):
            break
        if len(parent.get_text(" ", strip=True)) > MAX_CARD_CHARS:
            break
        card = parent
    return card


def linkedin_card(title: str, lines: list[str]) -> tuple[str, str, str] | None:
    """LinkedIn emails since October 2026 put one link around the whole card, so its text is
    "Title Company · City (Hybrid) Actively recruiting" (or "... Company City See who ... has hired").
    The card's lines are still title, company, place. "Jobs similar to · Title · at · Company": no place."""
    if len(lines) >= 4 and lines[0] == "Jobs similar to" and lines[2] == "at":
        return lines[1], lines[3], ""
    if len(lines) >= 3 and lines[0] != title and title.startswith(lines[0]) \
            and title[len(lines[0]):].strip().startswith(lines[1]):
        return lines[0], lines[1], lines[2]
    return None


def _job_from_card(url: str, anchors: list, card) -> Job | None:
    texts = [clean(a.get_text(" "), 200) for a in anchors]
    texts = [t for t in texts if len(t) >= 3 and not NOISE.match(t)]
    lines = _lines(card)
    title = max(texts, key=len) if texts else (lines[0] if lines else "")
    if not title:
        return None
    company = location = posted = ""
    rest = []
    for line in lines:
        if line == title:
            continue
        if not posted and len(line) < 40 and POSTED_HINT.search(line) and relative_date(line):
            posted = to_iso(relative_date(line))
            continue
        rest.append(line)
    info = [l for l in rest if len(l) <= 80 and "€" not in l and not re.search(r"\d+\s*(k|tsd)?\s*(€|eur)", l, re.I)]
    if card_lines := linkedin_card(title, rest):
        title, company, location = card_lines
    elif info and " · " in info[0]:  # LinkedIn: "Company · City, Hessen, Deutschland"
        company, _, location = info[0].partition(" · ")
    else:
        company = info[0] if info else ""
        location = info[1] if len(info) > 1 else ""
    if company == title:
        company = ""
    return Job(
        title=clean(title),
        url=url,
        company=clean(company, 120),
        location=clean(location.split(" · ")[0], 120),
        description=clean(" · ".join(rest), MAX_CARD_CHARS),
        posted=posted,
    )


def extract(html: str, resolve=None) -> list[Job]:
    """All jobs in one email. `resolve(href)` may follow a tracking link to the real job
    link (returns None if it doesn't lead to a job); without it such links are skipped."""
    soup = BeautifulSoup(html or "", "html.parser")
    groups: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        url = job_url(href)
        if url is None and resolve and could_be_job_link(href, clean(a.get_text(" "))):
            url = resolve(href)
        if url:
            groups.setdefault(url, []).append(a)
    jobs = []
    for url, anchors in groups.items():
        others = {id(a) for u, group in groups.items() if u != url for a in group}
        job = _job_from_card(url, anchors, _card(anchors, others))
        if job:
            jobs.append(job)
    return jobs
