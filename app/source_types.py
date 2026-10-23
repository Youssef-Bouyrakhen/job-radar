"""Sorts every job into one of three source types for the dashboard filter:

  "email"    job-alert emails from your Gmail label
  "portal"   job search sites: Bundesagentur für Arbeit, StepStone, Indeed, ...
  "company"  a company's own career page (KfW, DZ Bank, Personio/Workday/Greenhouse links, ...)
"""
from urllib.parse import urlparse

from . import arbeitsagentur, gmail

# Job search sites. Everything else counts as a company's own career page.
PORTALS = (
    "arbeitsagentur.de", "stepstone.de", "indeed.com", "indeed.de", "linkedin.com", "xing.com",
    "glassdoor.de", "glassdoor.com", "monster.de", "jobware.de", "stellenanzeigen.de",
    "studysmarter.de", "absolventa.de", "joblift.de", "kununu.com", "jobs.de", "meinestadt.de",
    "get-in-it.de", "jobvector.de", "heyjobs.co", "workwise.io", "jobteaser.com", "praktikum.info",
    "stellenwerk.de", "ingenieur.de", "wellfound.com", "karriere.at", "adzuna.de", "careerjet.de",
    "jooble.org", "yourfirm.de", "experteer.de", "studentjob.de", "jobmensa.de", "unicum.de",
)


def _host(text: str) -> str:
    text = (text or "").strip().lower()
    try:
        host = urlparse(text if "://" in text else "https://" + text).hostname or ""
    except ValueError:  # a source name, not an address: "[x] AG (Companies)" looks like a broken IPv6 address
        return ""
    return host.removeprefix("www.")


def is_portal(url_or_host: str) -> bool:
    host = _host(url_or_host)
    return any(host == p or host.endswith("." + p) for p in PORTALS)


def source_type(link: dict | None, job_source: str) -> str:
    """link: the link that found the job (None if it was deleted); job_source: its saved name."""
    kind = link["kind"] if link else None
    if kind == "gmail" or job_source == gmail.NAME:
        return "email"
    if kind == "arbeitsagentur" or job_source == arbeitsagentur.NAME:
        return "portal"
    return "portal" if is_portal(link["url"] if link else job_source) else "company"


# The boxes in the dashboard's source filter. Checked in this order against the job's own
# link first (alert emails link straight to LinkedIn/Indeed/StepStone), then the watched link.
SITES = (
    ("linkedin", ("linkedin.com",)),
    ("indeed", ("indeed.com", "indeed.de")),
    ("stepstone", ("stepstone.de",)),
    ("arbeitsagentur", ("arbeitsagentur.de",)),
)


def _site_of_host(url_or_host: str) -> str | None:
    host = _host(url_or_host)
    for site, domains in SITES:
        if any(host == d or host.endswith("." + d) for d in domains):
            return site
    return None


def site(link: dict | None, job_source: str, job_url: str = "") -> str:
    """linkedin | indeed | stepstone | arbeitsagentur | company | other (another job portal)."""
    kind = link["kind"] if link else None
    if kind == "arbeitsagentur" or job_source == arbeitsagentur.NAME:
        return "arbeitsagentur"
    found = _site_of_host(job_url) or (_site_of_host(link["url"]) if link and kind != "gmail" else None)
    if found:
        return found
    if kind == "gmail" or job_source == gmail.NAME:
        return "other"  # an alert email from a site without its own box
    return "company" if source_type(link, job_source) == "company" else "other"
