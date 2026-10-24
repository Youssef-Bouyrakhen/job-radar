"""Reads the jobs behind one link and decides if the link is trackable (green) or not (red)."""
import hashlib
import logging
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from . import dedup
from .config import CONFIG
from .extractors import Job, ats, generic
from .fetcher import Page, browser_available, fetch_html, fetch_with_browser, robots_allows

log = logging.getLogger("jobradar")


@dataclass
class CheckResult:
    status: str        # "green" or "red"
    reason: str
    method: str = ""
    name: str = ""
    jobs: list[Job] = field(default_factory=list)
    read_emails: list[str] = field(default_factory=list)  # Gmail: emails read in this check
    board: ats.Board | None = None   # the applicant system the jobs came from, if any


def red(reason: str, method: str = "") -> CheckResult:
    return CheckResult("red", reason, method)


def postings(n: int) -> str:
    return f"{n} job posting{'' if n == 1 else 's'}"


def unique(jobs: list[Job]) -> list[Job]:
    """Some sites list the same job twice (e.g. KfW, with two ad numbers). Keep one."""
    kept, seen = [], []
    for job in jobs:
        d = {"title": job.title, "company": job.company, "location": job.location}
        if not dedup.find_duplicate(d, seen):
            seen.append(d)
            kept.append(job)
    return kept


def found(jobs: list[Job], method: str, name: str, extra: str = "") -> CheckResult:
    """A green result. The count matches what gets saved: duplicates on the page don't count."""
    kept = unique(jobs)
    twice = len(jobs) - len(kept)
    note = f" ({twice} listed twice)" if twice else ""
    return CheckResult("green", f"Found {postings(len(kept))}{extra}{note}", method, name, kept)


def check_url(url: str, browser: bool = True) -> CheckResult:
    """browser=False skips the slow hidden-browser step (used when testing many suggestions)."""
    # 1. The link itself points to a known job system -> read its official feed.
    board = ats.detect(url)
    if board:
        return _from_board(board, url)

    # 2. A normal web page.
    if not robots_allows(url):
        return red("The site's robots.txt asks bots not to read this page")
    page = fetch_html(url)
    if not page.ok:
        return red(page.reason)
    result = _from_page(page, url, "Web page")
    if result:
        return result

    # 3. Maybe the job list is built with JavaScript -> try a real (hidden) browser.
    if not browser:
        why = " without a browser"
    elif not CONFIG["fetch"]["browser_fallback"]:
        why = " (browser fallback is turned off)"
    elif not browser_available():
        why = " (browser not installed, run setup.bat)"
    else:
        rendered = fetch_with_browser(url)
        if not rendered.ok:
            return _from_ai(page, url) or red(rendered.reason)
        result = _from_page(rendered, url, "Browser (JavaScript)")
        if result:
            return result
        page, why = rendered, ", even with a browser"

    # 4. The page clearly talks about open jobs, but no rule found them: the AI reads the job list.
    return _from_ai(page, url) or red(f"Page loaded, but no job postings found{why}")


def _from_page(page: Page, url: str, method: str) -> CheckResult | None:
    board = ats.detect(page.html) or ats.detect_job_site(page.url, page.html)  # an embedded Personio/Workday/... list
    if board:
        return _from_board(board, url)
    jobs, name = generic.extract(page.html, page.url)
    if jobs:
        return found(jobs, method, name)
    return None


# {link: (fingerprint of the page part, jobs the AI read from it)}. A watched link is checked
# every hour: the AI is only asked again when the job part of the page changed.
_ai_seen: dict[str, tuple[str, list[Job]]] = {}


def _from_ai(page: Page, url: str) -> CheckResult | None:
    from . import ai  # ai -> details -> arbeitsagentur imports this module: load them late
    from .company_jobs import ai_read, page_parts
    if not ai.enabled():
        return None
    parts = page_parts(page.html, page.url)  # the job part of the page, without scripts, menus, cookie text
    if not generic.HIRING.search(parts["full"]) and not generic.GENDER.search(parts["full"]):
        return None
    name = generic.site_name(BeautifulSoup(page.html, "html.parser"), page.url)
    fingerprint = hashlib.sha1(parts["short"].encode()).hexdigest()
    if url in _ai_seen and _ai_seen[url][0] == fingerprint:
        jobs = _ai_seen[url][1]
    else:
        try:
            jobs = ai_read(name, parts, page.url)
        except (ai.RateLimited, ai.Overloaded):
            return None  # no AI requests left right now: the next check tries again
        except Exception as e:
            log.warning("AI reading the job list of %s failed (%s)", url, e)
            return None
        # Only titles that look like a job ad ("... (m/w/d)", "Werkstudent ..."), not career areas
        jobs = [j for j in jobs if generic.GENDER.search(j.title) or generic.JOB_WORD.search(j.title)]
        _ai_seen[url] = (fingerprint, jobs)
    return found(jobs, "AI from page text", name) if jobs else None


def _from_board(board: ats.Board, url: str) -> CheckResult:
    result = _read_board(board, url)
    result.board = board
    return result


def _read_board(board: ats.Board, url: str) -> CheckResult:
    method = f"{board.system} feed"
    try:
        jobs = ats.fetch_jobs(board, ats.search_terms(url))
    except httpx.HTTPStatusError as e:
        return red(f"Found a {board.system} job board, but its feed answered with an error "
                   f"(HTTP {e.response.status_code}). Check that the link is the company's job page.", method)
    except Exception as e:
        return red(f"Found a {board.system} job board, but its feed could not be read ({type(e).__name__})", method)
    if not jobs:
        return red(f"The {board.system} feed is empty: either no open positions right now, "
                   f"or the company name in the link is wrong", method)
    return found(jobs, method, jobs[0].company)
