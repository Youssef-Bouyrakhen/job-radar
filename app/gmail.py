"""Built-in source: job-alert emails (Indeed, StepStone, LinkedIn) from your Gmail.

What Job Radar does - and does NOT do - with your Gmail:
- It opens ONLY the labels set in config.yaml: "JobRadar" for job alerts and
  "Bewerbungen" for replies to your applications (see applications.py). Never your inbox,
  never any other label.
- It opens that label read-only (IMAP "EXAMINE"), so it cannot move, label, archive or
  delete anything.
- It reads emails with "BODY.PEEK", so they stay unread.
- It remembers which emails it has read (in data/jobradar.db), so each one is read once.
- One exception, initiativ_mail.py: it ADDS Initiativ drafts to your Drafts folder and looks into
  Drafts and Sent to see which you sent. It never sends, deletes or changes an email.
- The application tracker (applications.py) also opens Sent and All Mail read-only (open_folder):
  Sent for the applications you emailed, All Mail only through searches for replies to them
  (their threads, the companies' domains, the "Bewerbungen" label, "Bewerbung" in the subject).

The login uses a Gmail *app password* (GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env),
see the README.
"""
import base64
import email
import imaplib
import logging
import os
import re
from datetime import timedelta
from email import policy
from urllib.parse import urljoin

import httpx

from . import clock, db
from .config import CONFIG
from .extractors import Job, alerts
from .fetcher import USER_AGENT, _polite_wait
from .sources import CheckResult, found, red

log = logging.getLogger("jobradar")

NAME = "Job-alert emails (Gmail)"
LINK_URL = "https://mail.google.com/#job-alert-emails"  # how this source appears in the links list
METHOD = "Gmail (read-only)"
IMAP_HOST = "imap.gmail.com"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_REDIRECT_LOOKUPS = 60  # per check, for tracking links we can't decode offline


def settings() -> dict:
    return CONFIG["emails"]


def credentials() -> tuple[str, str]:
    address = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    return address, password


def configured() -> bool:
    return all(credentials())


def label() -> str:
    return str(settings()["label"]).strip()


def safe_label(name: str) -> bool:
    """Only a label of your own - never the inbox or Gmail's system folders."""
    return bool(name) and name.upper() != "INBOX" and not name.startswith("[Gmail]")


def _imap_name(name: str) -> str:
    """Label name in IMAP's format (quoted, "modified UTF-7" for umlauts etc.)."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return '"' + "".join(out).replace("\\", "\\\\").replace('"', '\\"') + '"'


class GmailError(Exception):
    """Gmail could not be opened; the message says why in plain words."""


def open_label(name: str) -> imaplib.IMAP4_SSL:
    """Log in and open ONE label read-only (IMAP "EXAMINE": nothing in it can be changed).
    The caller must call .logout(). Raises GmailError."""
    address, password = credentials()
    if not (address and password):
        raise GmailError("Not set up: GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env (see README)")
    if not safe_label(name):
        raise GmailError(f"For safety Job Radar only reads its own Gmail labels, not \"{name}\". "
                         "Set the label in config.yaml")
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
    except Exception as e:
        raise GmailError(f"Could not reach Gmail ({type(e).__name__})") from e
    try:
        imap.login(address, password)
        status, _ = imap.select(_imap_name(name), readonly=True)
    except imaplib.IMAP4.error as e:
        _logout(imap)
        raise GmailError("Gmail refused the login. Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env "
                         "(it must be an app password, not your normal password)") from e
    except OSError:  # e.g. a timeout: close the connection, the caller decides (applications: tries once more)
        _logout(imap)
        raise
    if status != "OK":
        _logout(imap)
        raise GmailError(f"Gmail label \"{name}\" not found. Create it, and in Gmail settings > Labels "
                         f"make sure \"Show in IMAP\" is ticked for it")
    return imap


def open_folder(flag: str) -> imaplib.IMAP4_SSL:
    """Log in and open one of Gmail's own folders READ-ONLY, found by its flag (so a German Gmail works too):
    "Sent" (your sent emails) or "All" (All Mail, for replies to your applications - applications.py only
    searches it for those). Same rules as open_label: EXAMINE, nothing can be changed. Raises GmailError."""
    address, password = credentials()
    if not (address and password):
        raise GmailError("Not set up: GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env (see README)")
    if flag not in ("Sent", "All"):
        raise GmailError(f"Job Radar does not open the \"{flag}\" folder")
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
    except Exception as e:
        raise GmailError(f"Could not reach Gmail ({type(e).__name__})") from e
    try:
        imap.login(address, password)
        status, lines = imap.list()
        name = None
        for line in lines or []:
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)', (line or b"").decode(errors="ignore"))
            if m and "\\" + flag in m["flags"].split():
                name = m["name"] if m["name"].startswith('"') else f'"{m["name"]}"'
        if not name:
            raise GmailError(f"Gmail's {'All Mail' if flag == 'All' else flag} folder not found over IMAP "
                             "(Gmail settings > Labels: tick \"Show in IMAP\" for it)")
        status, _ = imap.select(name, readonly=True)
        if status != "OK":
            raise GmailError(f"Gmail's {flag} folder could not be opened")
    except imaplib.IMAP4.error as e:
        _logout(imap)
        raise GmailError("Gmail refused the login. Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env") from e
    except BaseException:
        _logout(imap)
        raise
    return imap


def search(imap: imaplib.IMAP4_SSL, query: str) -> list[bytes]:
    """UIDs in the open folder for a Gmail search ("from:x.de after:2026/05/01"). ASCII only."""
    status, data = imap.uid("search", None, "X-GM-RAW", '"' + query.replace("\\", "").replace('"', '\\"') + '"')
    return data[0].split() if status == "OK" and data and data[0] else []


def thread_uids(imap: imaplib.IMAP4_SSL, thread_id: str) -> list[bytes]:
    """UIDs of one Gmail thread in the open folder."""
    status, data = imap.uid("search", None, "X-GM-THRID", thread_id)
    return data[0].split() if status == "OK" and data and data[0] else []


def ids_of(imap: imaplib.IMAP4_SSL, uids: list[bytes]) -> dict[bytes, tuple[str, str]]:
    """UID -> (Gmail's message id, thread id), in chunks."""
    out = {}
    for i in range(0, len(uids), 300):
        status, data = imap.uid("fetch", b",".join(uids[i:i + 300]), "(X-GM-MSGID X-GM-THRID)")
        for line in data or []:
            text = (line[0] if isinstance(line, tuple) else line or b"").decode(errors="ignore")
            uid, gid, tid = (re.search(rf"{k} (\d+)", text) for k in ("UID", "X-GM-MSGID", "X-GM-THRID"))
            if uid and gid:
                out[uid[1].encode()] = (gid[1], tid[1] if tid else "")
    return out


def _logout(imap: imaplib.IMAP4_SSL) -> None:
    try:
        imap.logout()
    except Exception:
        pass


def ids_since(imap: imaplib.IMAP4_SSL, days_back: int) -> dict[bytes, str]:
    """Emails of the open label from the last N days: UID -> Gmail's own message id."""
    since = clock.today() - timedelta(days=days_back)
    status, data = imap.uid("search", None, "SINCE", f"{since.day}-{MONTHS[since.month - 1]}-{since.year}")
    uids = data[0].split() if status == "OK" and data and data[0] else []
    return _gmail_ids(imap, uids)


def fetch(imap: imaplib.IMAP4_SSL, uid: bytes) -> bytes | None:
    """The whole email. BODY.PEEK = read it without marking it as read."""
    status, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
    raw = next((part[1] for part in data or [] if isinstance(part, tuple)), None)
    return raw if status == "OK" else None


def check() -> CheckResult:
    cfg = settings()
    name = label()
    try:
        imap = open_label(name)
    except GmailError as e:
        return red(str(e), METHOD)
    try:
        ids = ids_since(imap, cfg["days_back"])
        known = db.known_emails(list(ids.values()))
        new = [(uid, gid) for uid, gid in ids.items() if gid not in known][-cfg["max_emails_per_check"]:]

        jobs: list[Job] = []
        read = []
        resolver = Resolver()
        for uid, gid in new:
            raw = fetch(imap, uid)
            if raw is None:
                continue
            try:
                found_jobs = jobs_from_email(raw, resolver)
            except Exception:
                log.exception("Could not read job-alert email %s", gid)
                found_jobs = []
            jobs.extend(found_jobs)
            read.append(gid)
    except (imaplib.IMAP4.error, OSError) as e:
        return red(f"Reading Gmail failed ({type(e).__name__}: {str(e)[:120]})", METHOD)
    finally:
        _logout(imap)

    if not read:
        return CheckResult("green", f"No new emails in the label \"{name}\" "
                           f"({len(ids)} from the last {cfg['days_back']} days already read)", METHOD, NAME)
    result = found(jobs, METHOD, NAME, f" in {len(read)} new email{'' if len(read) == 1 else 's'}")
    result.read_emails = read
    return result


def _gmail_ids(imap: imaplib.IMAP4_SSL, uids: list[bytes]) -> dict[bytes, str]:
    """UID (number inside this label) -> Gmail's own message id, which never changes."""
    if not uids:
        return {}
    status, data = imap.uid("fetch", b",".join(uids), "(X-GM-MSGID)")
    ids = {}
    for line in data or []:
        text = (line[0] if isinstance(line, tuple) else line or b"").decode(errors="ignore")
        uid, gid = re.search(r"UID (\d+)", text), re.search(r"X-GM-MSGID (\d+)", text)
        if uid and gid:
            ids[uid[1].encode()] = gid[1]
    return ids


def jobs_from_email(raw: bytes, resolve=None) -> list[Job]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=("html",))
    if body is None:
        return []
    jobs = alerts.extract(body.get_content(), resolve)
    sender = str(msg.get("From", ""))
    log.info("Job-alert email from %s: %d job(s)", sender[:60], len(jobs))
    return jobs


class Resolver:
    """Follows a tracking link (e.g. click.stepstone.de/...) hop by hop until it reaches a
    job link, without opening the job page itself. Only used for links that look like a
    job title; never for unsubscribe or settings links (see alerts.could_be_job_link)."""

    def __init__(self):
        self.cache: dict[str, str | None] = {}
        self.lookups = 0

    def __call__(self, href: str) -> str | None:
        if href in self.cache:
            return self.cache[href]
        if self.lookups >= MAX_REDIRECT_LOOKUPS:
            return None
        self.lookups += 1
        result = None
        url = href
        try:
            with httpx.Client(follow_redirects=False, timeout=15, headers={"User-Agent": USER_AGENT}) as client:
                for _ in range(6):
                    _polite_wait(url, 1)
                    with client.stream("GET", url) as r:  # headers only, the page is not downloaded
                        location = r.headers.get("location")
                    if not location:
                        break
                    url = urljoin(url, location)
                    result = alerts.job_url(url)
                    if result:
                        break
        except httpx.HTTPError:
            pass
        self.cache[href] = result
        return result
