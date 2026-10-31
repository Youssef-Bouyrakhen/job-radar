"""Initiativ email drafts: every morning a few Gmail DRAFTS for the companies on the Initiativ list.

Job Radar never sends them. You read each draft in Gmail, change it if you like, and send it yourself.

1. No AI, no tokens: 3 fixed templates (plain text), {{company}} is replaced by the company's name.
     Template 1      IT services/Software, Cybersecurity, Cloud/Data centers/Networks
     Template 3      Banks/Finance, Insurance
     Template Other  Other (and companies not sorted yet)
   {{company}} is the name up to its legal form: "Comfine GmbH Andreas Topp" -> "Comfine GmbH".
   Templates listed in config paused_templates (now Template 3) are left out: their companies get nothing.
   Each template has its own CV, attached as a PDF. Templates and CVs are personal: they are uploaded in the
   dashboard and kept in data/initiativ/ (never in git): template1.txt, cv_template1.pdf, template3.txt, ...
2. Each day: up to per_day companies that never got one, best fit first. The mix follows what is left in each
   group (about 60% Template 1, 6% Template 3, 34% Other with 339 companies), Template 3 at least
   min_template3 a day while any are left, so the small group lasts as long as the big ones.
3. The address: a jobs / Bewerbung address on its careers page, else the Impressum email (region.py), opened
   politely, no AI. None found: skipped (config no_email: draft makes a draft with an empty "To" instead).
4. Gmail: the same app password as the job-alert emails (IMAP, free). Job Radar only ADDS drafts to your Drafts
   folder and only LOOKS into Drafts and Sent; it never sends, deletes or changes an email.
5. Sent or not: the email you send from a draft stays in the draft's Gmail thread. Every hour (and before each
   day's drafts) Job Radar looks for that thread: in Sent = sent (initiativ_sent_date on the company), still in
   Drafts = waiting, in neither = you deleted the draft (that company gets no new one).
"""
import imaplib
import json
import logging
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parsedate_to_datetime

from . import careers, clock, db, gmail, region
from .config import CONFIG, ROOT
from .discover import company_key
from .fetcher import fetch_polite

log = logging.getLogger("jobradar")

FOLDER = ROOT / "data" / "initiativ"
# key -> (name, categories); "other" also takes companies whose category isn't sorted yet
TEMPLATES = {"1": ("Template 1", ("software", "security", "cloud")),
             "3": ("Template 3", ("finance", "insurance")),
             "other": ("Template Other", ("other",))}
MAX_CV_BYTES = 5_000_000
# The longest forms first, so "GmbH & Co. KG" is kept whole and not cut after "GmbH"
LEGAL_FORM = re.compile(r"\b(?:(?:GmbH|AG|UG|SE)\s*&\s*Co\.?\s*(?:KGaA|KG|OHG)|UG\s*\(haftungsbeschränkt\)"
                        r"|gGmbH|GmbH|KGaA|AG|UG|KG|SE|e\.\s?V\.)(?!\w)")
PENDING = ("draft",)

_lock = threading.Lock()
running = {"now": False}


def _local_day(at: str):
    return clock.german(datetime.fromisoformat(at)).date()


def settings() -> dict:
    return CONFIG["initiativ_emails"]


def paused(key: str) -> bool:
    return key in (settings().get("paused_templates") or [])


def template_of(category: str | None) -> str:
    return next((k for k, (_, cats) in TEMPLATES.items() if category in cats), "other")


def text_file(key: str):
    return FOLDER / f"template{'_other' if key == 'other' else key}.txt"


def cv_file(key: str):
    return FOLDER / f"cv_template{'_other' if key == 'other' else key}.pdf"


def template_text(key: str) -> str:
    f = text_file(key)
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def save_template(key: str, text: str) -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    text_file(key).write_text(text.strip().replace("\r\n", "\n") + "\n", encoding="utf-8")


def save_cv(key: str, data: bytes) -> None:
    if not data.startswith(b"%PDF"):
        raise ValueError("That is not a PDF file")
    if len(data) > MAX_CV_BYTES:
        raise ValueError(f"The PDF is bigger than {MAX_CV_BYTES // 1_000_000} MB")
    FOLDER.mkdir(parents=True, exist_ok=True)
    cv_file(key).write_bytes(data)


def ready(key: str) -> str:
    """"" if this template can be used, else what is missing."""
    missing = [what for what, ok in (("text", template_text(key)), ("CV", cv_file(key).exists())) if not ok]
    return " and ".join(missing)


# ---- your switches (dashboard) --------------------------------------------------------

def is_on() -> bool:
    return db.get_setting("initiativ_drafts_on") == "1"


def per_day() -> int:
    return int(db.get_setting("initiativ_per_day") or settings()["per_day"])


def sender_name() -> str:
    """The name in "From": yours from the dashboard, else the last line of Template 1 (the signature)."""
    lines = template_text("1").splitlines()
    return db.get_setting("initiativ_sender_name") or (lines[-1].strip() if lines else "")


def set_switches(on: bool | None = None, count: int | None = None, name: str | None = None) -> None:
    if on is not None:
        db.set_setting("initiativ_drafts_on", "1" if on else "0")
    if count is not None:
        db.set_setting("initiativ_per_day", str(max(1, min(int(count), 60))))
    if name is not None:
        db.set_setting("initiativ_sender_name", name.strip()[:80])


# ---- which companies today ------------------------------------------------------------

def candidates() -> dict[str, list[dict]]:
    """Initiativ companies that never got a draft, per template, best fit first. Not the ones you already
    applied to (a job of theirs is marked Applied) or sent an Initiativ email to."""
    done = {d["company_id"] for d in db.initiativ_drafts() if not d["test"]}
    with db.connect() as conn:
        applied = {company_key(r[0]) for r in conn.execute("SELECT DISTINCT company FROM jobs WHERE status = 'applied' "
                                                           "AND company IS NOT NULL")}
    groups = {k: [] for k in TEMPLATES}
    for c in db.company_rows():
        if c["status"] == "initiativ" and c["id"] not in done and not c["initiativ_sent_date"] \
                and c["name_key"] not in applied:
            groups[template_of(c["category"])].append(c)
    for rows in groups.values():
        rows.sort(key=lambda c: (-(c["fit_score"] or 0), c["name"].lower()))
    return groups


def quotas(left: dict[str, int], n: int, min_t3: int) -> Counter:
    """How many of each template today: in proportion to what is left, Template 3 at least min_t3."""
    q, total = Counter(), sum(left.values())
    if not total:
        return q
    q["3"] = min(left.get("3", 0), min_t3, n)
    while sum(q.values()) < n and any(q[k] < left[k] for k in left):
        k = sum(q.values()) + 1  # the next one goes to the group furthest behind its share
        q[max((g for g in left if q[g] < left[g]), key=lambda g: left[g] / total * k - q[g])] += 1
    return q


# ---- the address ------------------------------------------------------------------------

def find_email(c: dict) -> tuple[str | None, str]:
    """(address, where it was found). A jobs / Bewerbung address on its own website first."""
    saved = (c["contact_email"] or "").strip().lower()
    if saved and region.GOOD_INBOX.search(saved):
        return saved, "Impressum (saved)"
    site = careers._site(c["website"] or c["career_url"] or "")
    if c["career_url"]:
        page = fetch_polite(c["career_url"])
        if page.ok:
            good = [e for e in region.emails_in(page.html)
                    if region.GOOD_INBOX.search(e) and (e.split("@")[1] == site or e.split("@")[1].endswith("." + site))]
            if good:
                return good[0], "careers page"
    if saved:
        return saved, "Impressum (saved)"
    if c["website"]:
        try:
            email, _ = region.find_contact(c["website"])
        except Exception as e:
            log.info("Initiativ: website of %s could not be read (%s)", c["name"], e)
            email = None
        if email:
            return email, "Impressum"
    return None, ""


def short_name(name: str) -> str:
    """The name up to and with its legal form: "Comfine GmbH Andreas Topp" -> "Comfine GmbH". No legal form: as is."""
    name = " ".join(name.split())
    m = LEGAL_FORM.search(name)
    return name[:m.end()] if m and m.start() else name


def fill(key: str, c: dict) -> str:
    return template_text(key).replace("{{company}}", short_name(c["name"]))


def pick(n: int, look_up: int, need_cv: bool = True) -> tuple[list[dict], list[dict]]:
    """Today's companies with their address: (picked, no address found). Templates that aren't ready
    (text or CV missing; for the preview only the text) and paused ones are left out."""
    groups = {k: rows for k, rows in candidates().items()
              if not paused(k) and (not ready(k) if need_cv else template_text(k))}
    want = quotas({k: len(rows) for k, rows in groups.items()}, n, settings()["min_template3"])
    picked, no_email, tries = [], [], 0
    draft_anyway = settings()["no_email"] == "draft"
    queue = {k: iter(rows) for k, rows in groups.items()}

    def take(key: str) -> bool:
        nonlocal tries
        for c in queue[key]:
            if tries >= look_up:
                return False
            tries += 1
            email, where = find_email(c)
            if email or draft_anyway:
                picked.append({"company": c, "template": key, "to": email, "from": where})
                return True
            no_email.append({"company": c, "template": key})
        return False

    for key in TEMPLATES:
        for _ in range(want.get(key, 0)):
            if not take(key):
                break
    # A group ran out of addresses: the others fill its places
    while len(picked) < n and tries < look_up and any(take(k) for k in sorted(groups, key=lambda k: -len(groups[k]))):
        pass
    return picked[:n], no_email


# ---- Gmail -------------------------------------------------------------------------------

class Mailbox:
    """Your Gmail over IMAP (app password): adds drafts, looks into Drafts and Sent. `with Mailbox() as m:`"""

    def __enter__(self):
        address, password = gmail.credentials()
        if not (address and password):
            raise gmail.GmailError("Not set up: GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing in .env (see README)")
        self.address = address
        try:
            self.imap = imaplib.IMAP4_SSL(gmail.IMAP_HOST, timeout=30)
            self.imap.login(address, password)
        except imaplib.IMAP4.error as e:
            raise gmail.GmailError("Gmail refused the login: check GMAIL_APP_PASSWORD in .env") from e
        except OSError as e:
            raise gmail.GmailError(f"Could not reach Gmail ({type(e).__name__})") from e
        self.folders = self._special_folders()
        missing = [f for f in ("Drafts", "Sent") if f not in self.folders]
        if missing:
            gmail._logout(self.imap)
            raise gmail.GmailError(f"Gmail's {' and '.join(missing)} folder not found over IMAP "
                                   "(Gmail settings > Labels: tick \"Show in IMAP\" for it)")
        return self

    def __exit__(self, *_):
        gmail._logout(self.imap)

    def _special_folders(self) -> dict[str, str]:
        """{"Drafts": '"[Gmail]/Entw&APw-rfe"', "Sent": ...}: found by their flag, so a German Gmail works too."""
        status, lines = self.imap.list()
        found = {}
        for line in lines or []:
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)', (line or b"").decode(errors="ignore"))
            if not m:
                continue
            name = m["name"] if m["name"].startswith('"') else f'"{m["name"]}"'
            for flag in ("Drafts", "Sent"):
                if "\\" + flag in m["flags"].split():
                    found[flag] = name
        return found

    def add_draft(self, msg: EmailMessage) -> tuple[str, str]:
        """Puts the email into Drafts. Returns Gmail's (message id, thread id)."""
        folder = self.folders["Drafts"]
        status, data = self.imap.append(folder, r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != "OK":
            raise gmail.GmailError(f"Gmail did not take the draft ({(data or [b''])[0][:120]!r})")
        uid = re.search(rb"APPENDUID \d+ (\d+)", b" ".join(d for d in data if isinstance(d, bytes)))
        self.imap.select(folder, readonly=True)
        if uid:
            uids = [uid[1]]
        else:  # no UIDPLUS answer: find it by its Message-ID
            status, found = self.imap.uid("search", None, "HEADER", "Message-ID", msg["Message-ID"])
            uids = found[0].split()[-1:] if status == "OK" and found and found[0] else []
        if not uids:
            return "", ""
        status, data = self.imap.uid("fetch", uids[0], "(X-GM-MSGID X-GM-THRID)")
        text = b" ".join(d[0] if isinstance(d, tuple) else d or b"" for d in data or []).decode(errors="ignore")
        msgid, thrid = re.search(r"X-GM-MSGID (\d+)", text), re.search(r"X-GM-THRID (\d+)", text)
        return (msgid[1] if msgid else ""), (thrid[1] if thrid else "")

    def in_thread(self, thrid: str) -> str | None:
        """When the newest email of this thread in the folder arrived (ISO, UTC), None = not in it.
        The folder must be open (look())."""
        status, data = self.imap.uid("search", None, "X-GM-THRID", thrid)
        uids = data[0].split() if status == "OK" and data and data[0] else []
        return self._date(uids[-1]) if uids else None

    def sent_to(self, address: str, subject: str) -> str | None:
        """In Sent, open: an email to this address with this subject (also when you wrote a new one)."""
        query = f'to:{address} subject:"{subject}"'.replace('\\', '').replace('"', '\\"')
        status, data = self.imap.uid("search", None, "X-GM-RAW", f'"{query}"')
        uids = data[0].split() if status == "OK" and data and data[0] else []
        return self._date(uids[-1]) if uids else None

    def look(self, flag: str) -> None:
        status, _ = self.imap.select(self.folders[flag], readonly=True)
        if status != "OK":
            raise gmail.GmailError(f"Gmail's {flag} folder could not be opened")

    def _date(self, uid: bytes) -> str:
        status, data = self.imap.uid("fetch", uid, "(INTERNALDATE)")
        text = b" ".join(d[0] if isinstance(d, tuple) else d or b"" for d in data or []).decode(errors="ignore")
        m = re.search(r'INTERNALDATE "([^"]+)"', text)
        if not m:
            return db.now()
        at = datetime.strptime(m[1], "%d-%b-%Y %H:%M:%S %z")
        return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def message(p: dict, from_address: str, test: bool = False) -> EmailMessage:
    key, c = p["template"], p["company"]
    name = sender_name()
    msg = EmailMessage()
    msg["From"] = formataddr((name, from_address)) if name else from_address
    if test:  # to yourself, so nothing can reach the company by mistake
        msg["To"] = from_address
        msg["Subject"] = f"[TEST {c['name']} -> {p['to'] or 'no address'}] {settings()['subject']}"
    else:
        if p["to"]:
            msg["To"] = p["to"]
        msg["Subject"] = settings()["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1])
    msg.set_content(fill(key, c) + "\n")
    msg.add_attachment(cv_file(key).read_bytes(), maintype="application", subtype="pdf",
                       filename=f"Lebenslauf {name}.pdf".replace("  ", " ") if name else "Lebenslauf.pdf")
    return msg


def make_drafts(n: int, test: bool = False, why: str = "by hand") -> dict:
    """Makes up to n drafts now. test: to your own address, subject "[TEST ...]", the companies stay on the
    list. Returns what happened (also kept for the dashboard and the Timeline)."""
    if not _lock.acquire(blocking=False):
        return {"error": "Initiativ drafts are being made already"}
    running["now"] = True
    started = db.now()
    result = {"at": started, "why": why, "test": test, "made": [], "no_email": 0, "failed": [], "not_ready": []}
    try:
        result["checked"] = check_sent()
        result["not_ready"] = [f"{TEMPLATES[k][0]}: {ready(k)} missing" for k in TEMPLATES if ready(k) and not paused(k)]
        today = sum(1 for d in db.initiativ_drafts() if not d["test"] and d["state"] != "no_email"
                    and _local_day(d["created_at"]) == clock.today())
        n = n if test or why == "by hand" else max(0, n - today)  # the 07:00 run after a run by hand: only the rest
        if not n:
            return result
        picked, no_email = pick(n, settings()["max_lookups_per_run"])
        if not test:
            for x in no_email:
                db.save_initiativ_draft({"company_id": x["company"]["id"], "template": x["template"], "state": "no_email",
                                         "created_at": db.now(), "note": "No address on its careers page or Impressum"})
        result["no_email"] = len(no_email)
        if picked:
            with Mailbox() as box:
                for p in picked:
                    c = p["company"]
                    try:
                        msgid, thrid = box.add_draft(message(p, box.address, test))
                        state, note = "draft", None
                    except (gmail.GmailError, imaplib.IMAP4.error, OSError) as e:
                        log.warning("Initiativ draft for %s failed: %s", c["name"], e)
                        msgid = thrid = None
                        state, note = "failed", str(e)[:200]
                        result["failed"].append(c["name"])
                    if state == "draft" or not test:
                        db.save_initiativ_draft({"company_id": -c["id"] if test else c["id"], "template": p["template"],
                                                 "to_email": p["to"], "email_from": p["from"], "state": state,
                                                 "test": int(test), "gmail_msgid": msgid, "gmail_thrid": thrid,
                                                 "created_at": db.now(), "note": note})
                    if state == "draft":
                        result["made"].append({"name": c["name"], "template": p["template"], "to": p["to"]})
        log.info("Initiativ drafts (%s%s): %d made, %d without an address, %d failed", why, ", test" if test else "",
                 len(result["made"]), result["no_email"], len(result["failed"]))
    except gmail.GmailError as e:
        result["error"] = str(e)
        log.warning("Initiativ drafts: %s", e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log.exception("Initiativ drafts failed")
    finally:
        result["finished_at"] = db.now()
        db.set_setting("initiativ_last_run", json.dumps(result))
        running["now"] = False
        _lock.release()
    return result


def check_sent() -> dict:
    """Looks in Gmail which drafts you sent (or deleted). Returns {"sent": n, "deleted": n} or {"error": why}."""
    pending = [d for d in db.initiativ_drafts() if d["state"] in PENDING and d["gmail_thrid"]]
    found = {"sent": 0, "deleted": 0}
    if not pending:
        return found
    try:
        with Mailbox() as box:
            box.look("Sent")
            sent = {}
            for d in pending:
                at = box.in_thread(d["gmail_thrid"]) or (d["to_email"] and not d["test"]
                                                                 and box.sent_to(d["to_email"], settings()["subject"]))
                if at:
                    sent[d["company_id"]] = at
            box.look("Drafts")
            for d in pending:
                now = db.now()
                if d["company_id"] in sent:
                    db.update_initiativ_draft(d["company_id"], {"state": "sent", "sent_at": sent[d["company_id"]],
                                                                "checked_at": now})
                    found["sent"] += 1
                elif box.in_thread(d["gmail_thrid"]):
                    db.update_initiativ_draft(d["company_id"], {"checked_at": now})
                else:
                    db.update_initiativ_draft(d["company_id"], {"state": "deleted", "checked_at": now,
                                                                "note": "You deleted the draft in Gmail without sending it"})
                    found["deleted"] += 1
    except (gmail.GmailError, imaplib.IMAP4.error, OSError) as e:
        log.warning("Initiativ: could not look for sent drafts (%s)", e)
        return {"error": str(e)[:200]}
    if found["sent"] or found["deleted"]:
        log.info("Initiativ: %d draft(s) sent, %d deleted in Gmail", found["sent"], found["deleted"])
    return found


# ---- the scheduler and the dashboard ------------------------------------------------------

def scheduled() -> None:
    """Every day at initiativ_emails.hour (schedule.py): today's drafts, if you turned it on."""
    if not is_on():
        log.info("Initiativ drafts: off (turn them on in Companies > Initiativ)")
        check_sent()
        return
    make_drafts(per_day(), why="daily")


def scheduled_check() -> None:
    if not running["now"]:
        check_sent()


def last_run() -> dict:
    return json.loads(db.get_setting("initiativ_last_run") or "{}")


def preview(n: int) -> list[dict]:
    """What the next n drafts would be, with their full text. Opens careers pages / Impressums to find the
    addresses, nothing goes to Gmail and nothing is saved."""
    picked, no_email = pick(n, settings()["max_lookups_per_run"], need_cv=False)
    out = [{"name": p["company"]["name"], "category": p["company"]["category"], "template": p["template"],
            "to": p["to"], "found_in": p["from"], "subject": settings()["subject"], "body": fill(p["template"], p["company"]),
            "cv": cv_file(p["template"]).name if cv_file(p["template"]).exists() else None} for p in picked]
    return out + [{"name": x["company"]["name"], "template": x["template"], "to": None, "skipped": "no address found"}
                  for x in no_email]


def by_company() -> dict[int, dict]:
    """For the Companies list: {company id: {"state", "to", "at"}} (no test drafts)."""
    return {d["company_id"]: {"state": d["state"], "to": d["to_email"], "at": d["sent_at"] or d["created_at"]}
            for d in db.initiativ_drafts() if not d["test"]}


def status() -> dict:
    drafts = db.initiativ_drafts()
    real = [d for d in drafts if not d["test"]]
    today = clock.today()
    groups = candidates()
    names = {c["id"]: c["name"] for c in db.company_rows()}
    with db.connect() as conn:
        sent_total = conn.execute("SELECT COUNT(*) FROM companies WHERE initiativ_sent_date IS NOT NULL").fetchone()[0]
    return {
        "on": is_on(), "per_day": per_day(), "hour": settings()["hour"], "subject": settings()["subject"],
        "sender_name": sender_name(), "running": running["now"],
        "ready_today": sum(1 for d in real if d["state"] == "draft" and _local_day(d["created_at"]) == today),
        "waiting": sum(1 for d in real if d["state"] == "draft"),
        "sent": sent_total,
        "deleted": sum(1 for d in real if d["state"] == "deleted"),
        "no_email": sum(1 for d in real if d["state"] == "no_email"),
        "templates": [{"key": k, "name": name, "categories": cats, "left": len(groups[k]), "text": template_text(k),
                       "cv": cv_file(k).stat().st_size if cv_file(k).exists() else None, "missing": ready(k),
                       "paused": paused(k)}
                      for k, (name, cats) in TEMPLATES.items()],
        "today_mix": dict(quotas({k: len(v) for k, v in groups.items() if not ready(k) and not paused(k)}, per_day(),
                                 settings()["min_template3"])),
        "last": last_run(),
        "recent": [{**d, "name": names.get(abs(d["company_id"]), "?")} for d in drafts[:40] if d["state"] != "no_email"],
    }
