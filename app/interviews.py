"""Interviews: your interview dates, read from your emails (nothing is typed by hand).

What it does, every hour (and when you press "Read Gmail now" in the Interviews popup):
1. Finds interview threads in the "Bewerbungen" label (same safe Gmail reader as the application tracker:
   read-only, BODY.PEEK, nothing is moved, labelled, answered or deleted): a search for interview words
   (German + English) plus the threads the tracker already sorted as interview.
2. Reads each thread whole, oldest to newest (your own replies are in it too). Quoted replies and signatures are cut,
   calendar invites (.ics) and Teams / Zoom links are kept. The LAST agreed time wins.
3. The free AI (the same rotation as everywhere, purpose "other") returns strict JSON for each thread.
   A thread is read again only when a new email arrives in it.
4. A check without AI (_verify): the date, the time and the weekday the AI named must really stand in the emails.
   If not, confidence is set to "low" and the dashboard shows "check the email".
Status: confirmed / waiting. "past" is worked out when shown (the time is over).
"""
import hashlib
import html
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

from . import ai, applications, clock, db, gmail
from .config import CONFIG

log = logging.getLogger("jobradar")

KINDS = ("teams", "video", "phone", "in_person", "unknown")
KIND_LABELS = {"teams": "Teams", "video": "Video call", "phone": "Phone", "in_person": "In person", "unknown": "Type unknown"}
CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
# Words for the Gmail search (ASCII only: Gmail's search ignores umlauts, "Gespraech" is also tried)
SEARCH_WORDS = ["interview", "vorstellungsgespraech", "vorstellungsgesprach", "gesprach", "gespraech", "kennenlernen",
                "termin", "einladung", "teams", "videocall", "terminbestatigung", "terminbestaetigung",
                "alternativtermin", "invitation", "meeting", "zoom", "calendly"]
DEFAULT_LENGTH = timedelta(hours=1)  # an interview without an end time counts as over after this long
MAX_PROMPT_CHARS = 9000
MAX_PER_MESSAGE = 1400

_lock = threading.Lock()
_last: dict = {}
_misses: dict[str, int] = {}


def settings() -> dict:
    return CONFIG.get("interviews") or {}


def enabled() -> bool:
    return bool(settings().get("enabled", True)) and gmail.configured()


def last_result() -> dict:
    return dict(_last)


# ---- 1. reading an email, short -------------------------------------------------------------

MEETING_LINK = re.compile(r"https?://(?:[\w-]+\.)?(?:teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet|zoom\.us/[jw]|"
                          r"meet\.google\.com|[\w-]+\.webex\.com|calendly\.com|whereby\.com)/[^\s<>\")\]]+", re.I)
QUOTE_START = re.compile(r"^\s*(am .{5,140} schrieb|on .{5,140} wrote|-{2,}\s*(original|urspr|weitergeleitet|forwarded)|"
                         r"von:\s|from:\s|gesendet:|sent:|_{5,}|\*{0,2}von:)", re.I)
SIGNATURE = re.compile(r"^\s*(--\s*$|mit (freundlichen|besten|herzlichen) gr|freundliche gr|viele gr|beste gr|liebe gr|"
                       r"herzliche gr|best regards|kind regards|regards|many thanks|sent from|gesendet von|"
                       r"holen sie sich outlook)", re.I)


def _html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(style|script|head).*?</\1>", " ", content)
    content = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", content)
    return re.sub(r"<[^>]+>", " ", content)


def _short(text: str) -> str:
    """The new part of an email: no quoted older emails, no signature, no links."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        if QUOTE_START.match(line) and any(x.strip() for x in lines):
            break
        if SIGNATURE.match(line) and any(x.strip() for x in lines):
            break
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"https?://\S+", " ", out)
    out = re.sub(r"[ \t ]+", " ", out)
    out = re.sub(r"\n\s*\n+", "\n", out)
    return out.strip()


def _tz(name: str):
    if re.search(r"w\. europe|central europe|romance|mitteleurop|berlin", name, re.I):
        return clock.GERMANY
    try:
        return ZoneInfo(name)
    except Exception:
        return clock.GERMANY


def _ics_time(prop: str, value: str):
    """DTSTART;TZID=Europe/Berlin:20261113T130000 -> German local time. Whole-day values give None."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)", value.strip())
    if not m:
        return None
    y, mo, d, h, mi = (int(m[i]) for i in range(1, 6))
    try:
        if m[7]:
            return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).astimezone(clock.GERMANY)
        tz = re.search(r"TZID=([^;:]+)", prop)
        return datetime(y, mo, d, h, mi, tzinfo=_tz(tz[1].strip('"')) if tz else clock.GERMANY).astimezone(clock.GERMANY)
    except ValueError:
        return None


def _calendar_invites(msg) -> list[str]:
    """Calendar invites (.ics) in the email, as one short line each (a Terminbestaetigung often is one)."""
    out = []
    for part in msg.walk():
        if part.get_content_type() not in ("text/calendar", "application/ics"):
            continue
        try:
            raw = part.get_content()
        except Exception:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        raw = re.sub(r"\r?\n[ \t]", "", raw)  # folded lines
        props = {}
        for line in raw.splitlines():
            m = re.match(r"([A-Z-]+)((?:;[^:]+)*):(.*)", line)
            if m and m[1] not in props:
                props[m[1]] = (m[2], m[3].strip())
        start = _ics_time(*props["DTSTART"]) if "DTSTART" in props else None
        end = _ics_time(*props["DTEND"]) if "DTEND" in props else None
        if not start:
            continue
        parts = ["Calendar invite"]
        if props.get("METHOD", ("", ""))[1] == "CANCEL" or props.get("STATUS", ("", ""))[1] == "CANCELLED":
            parts.append("CANCELLED")
        parts.append(f"start {start:%d.%m.%Y %H:%M} German time")
        if end:
            parts.append(f"end {end:%d.%m.%Y %H:%M}")
        for key, label in (("SUMMARY", "title"), ("LOCATION", "location")):
            if props.get(key, ("", ""))[1]:
                parts.append(f"{label}: {props[key][1][:120]}")
        out.append("[" + " | ".join(parts) + "]")
    return out


def read_message(raw: bytes) -> dict:
    msg = message_from_bytes(raw, policy=policy.default)
    try:
        sent = parsedate_to_datetime(str(msg["Date"])).astimezone(clock.GERMANY)
    except (TypeError, ValueError):
        sent = clock.now()
    body = msg.get_body(preferencelist=("plain", "html"))
    text, full = "", ""
    if body is not None:
        try:
            content = body.get_content()
        except Exception:
            content = ""
        if body.get_content_type() == "text/html":
            content = _html_to_text(content)
        full = html.unescape(content)
        text = _short(full)
    links = list(dict.fromkeys(MEETING_LINK.findall(full)))[:2]
    invites = _calendar_invites(msg)
    sender = str(msg.get("From", ""))
    return {"sent": sent, "sender": sender, "mine": parseaddr(sender)[1].lower() == applications._me(),
            "subject": re.sub(r"\s+", " ", str(msg.get("Subject", "")))[:200], "text": text[:MAX_PER_MESSAGE],
            "links": links, "invites": invites}


def _message_block(i: int, m: dict) -> str:
    who = "ME (the student)" if m["mine"] else f"COMPANY ({applications.sender_name(m['sender']) or m['sender']})"
    extra = "".join(f"\n{inv}" for inv in m["invites"]) + "".join(f"\n[meeting link: {link[:110]}]" for link in m["links"])
    return f"### Email {i} | {m['sent']:%a %d.%m.%Y %H:%M} | {who} | subject: {m['subject']}\n{m['text']}{extra}"


def thread_text(messages: list[dict]) -> str:
    """The whole thread for the AI, oldest first, within the size limit (the newest emails matter most)."""
    blocks = [_message_block(i, m) for i, m in enumerate(messages, 1)]
    while len(blocks) > 2 and sum(len(b) for b in blocks) > MAX_PROMPT_CHARS // 2:
        del blocks[1 if len(blocks) > 3 else 0]  # drop from the middle, keep the first and the newest
    return "\n\n".join(blocks)[:MAX_PROMPT_CHARS // 2 + 800]


# ---- 2. the AI -----------------------------------------------------------------------------

AI_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "is_interview": {"type": "BOOLEAN"},
            "cancelled": {"type": "BOOLEAN"},
            "company": {"type": "STRING"},
            "job_title": {"type": "STRING"},
            "start": {"type": "STRING"},
            "end": {"type": "STRING"},
            "type": {"type": "STRING", "enum": list(KINDS)},
            "location_or_link": {"type": "STRING"},
            "status": {"type": "STRING", "enum": ["confirmed", "waiting"]},
            "proof": {"type": "STRING"},
            "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
            "note": {"type": "STRING"},
        },
        "required": ["id", "is_interview", "cancelled", "company", "job_title", "start", "end", "type",
                     "location_or_link", "status", "proof", "confidence", "note"],
    },
}

AI_PROMPT = """You read email threads between a student (ME) and companies about Werkstudent jobs. For each thread find the
job interview (also a phone call, Teams / video call or "Kennenlernen") and its date. Emails are German or English, oldest first.
Today is {today} (Germany). Times are German time (Europe/Berlin).

For each thread return one object:
- id: the thread id, unchanged.
- is_interview: true only if a CONCRETE date and time for an interview was proposed, offered or agreed in the emails.
  false for: only "we will get back to you", rejections, application confirmations, newsletters, no date given.
- cancelled: true if the interview was cancelled / called off and no new time was agreed.
- company, job_title: as written in the emails ("" if unknown).
- start: "YYYY-MM-DD HH:MM" (24 hours). The LAST agreed time wins: a later email beats an earlier one. Example: the
  company proposed Thu 12.11. 09:00, then offered an alternative Fri 13.11. 13:00, and ME accepted: start = 13.11. 13:00.
  If a date has no year, use the next such date after the email it stands in. If nothing is agreed yet, use the company's
  newest proposal; if ME proposed several times and none is chosen, use the earliest one and say so in note. "" if none.
- end: "YYYY-MM-DD HH:MM" only if an end time or a length ("30 Minuten", "bis 14 Uhr") is stated, else "".
- type: teams (Microsoft Teams), video (Zoom, Google Meet, other video call), phone, in_person, unknown.
- location_or_link: the meeting link, address or phone number from the emails ("" if none). Never invent one.
- status:
  "confirmed" = the company proposed or agreed a time AND ME replied that it fits, OR the company sent a
  "Terminbestaetigung" / calendar invite / "hiermit bestaetigen wir".
  "waiting" = ME sent time suggestions, or the company asked ME to confirm, and nobody has confirmed yet.
- proof: copy word for word the short piece of an email that holds the date and the time (max 120 characters), "" if none.
- confidence: "high" only if date and time are stated clearly in the emails; "low" if you had to guess anything.
- note: max 12 words, e.g. which slots were proposed ("" if nothing to add).
Use ONLY what is written in the emails.

THREADS
{threads}
"""


def ask(batch: list[dict]) -> dict[str, dict]:
    """thread_id -> the AI's answer. Raises ai.RateLimited / Overloaded / BudgetSaved / BrokenAnswer."""
    today = clock.now()
    threads = "\n\n".join(f"=== THREAD id={t['thread_id']} ===\n{thread_text(t['messages'])}" for t in batch)
    answer = ai.ask_json(AI_PROMPT.format(today=f"{today:%A %d.%m.%Y}", threads=threads), AI_SCHEMA,
                         temperature=0.1, purpose="other")
    if isinstance(answer, dict):
        answer = answer.get("threads") or [answer]
    return {str(a.get("id")): a for a in answer if isinstance(a, dict)}


# ---- 3. the check without AI -----------------------------------------------------------------

MONTHS = {"januar": 1, "jänner": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "märz": 3,
          "maerz": 3, "mär": 3, "mar": 3, "march": 3, "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "jun": 6,
          "june": 6, "juli": 7, "jul": 7, "july": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
          "oktober": 10, "okt": 10, "oct": 10, "october": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12,
          "dec": 12, "december": 12}
MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
WEEKDAYS = {0: ("montag", "mo", "monday", "mon"), 1: ("dienstag", "di", "tuesday", "tue"),
            2: ("mittwoch", "mi", "wednesday", "wed"), 3: ("donnerstag", "do", "thursday", "thu"),
            4: ("freitag", "fr", "friday", "fri"), 5: ("samstag", "sa", "saturday", "sat"),
            6: ("sonntag", "so", "sunday", "sun")}


def dates_in(text: str) -> set[tuple[int, int]]:
    """(month, day) of every date written in the text: 13.11. / 13.11.2026 / 13. November / November 13 / 2026-11-13 / 13/11."""
    found = set()
    t = text.lower()
    for d, mo in re.findall(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.(?:\d{2,4})?(?!\d)", t):
        found.add((int(mo), int(d)))
    for y, mo, d in re.findall(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)", t):
        found.add((int(mo), int(d)))
    for d, mo in re.findall(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\.?\s*(?:of\s+)?({MONTH_RX})\b", t):
        found.add((MONTHS[mo], int(d)))
    for mo, d in re.findall(rf"\b({MONTH_RX})\.?\s+(\d{{1,2}})(?!\d)", t):
        found.add((MONTHS[mo], int(d)))
    for a, b in re.findall(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", t):
        found.add((int(b), int(a)))
        found.add((int(a), int(b)))
    return {(mo, d) for mo, d in found if 1 <= mo <= 12 and 1 <= d <= 31}


def times_in(text: str) -> set[tuple[int, int]]:
    """(hour, minute) of every time written in the text: 13:00 / 13.00 / 13 Uhr / 1 pm / 1:30pm."""
    found = set()
    t = text.lower()
    for h, m in re.findall(r"(?<![\d.])(\d{1,2})[:.](\d{2})(?!\d)", t):
        found.add((int(h), int(m)))
    for h in re.findall(r"(?<![\d.:])(\d{1,2})\s*(?:uhr|h\b)", t):
        found.add((int(h), 0))
    for h, m, ap in re.findall(r"(?<![\d.:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t):
        hour = int(h) % 12 + (12 if ap == "pm" else 0)
        found.add((hour, int(m or 0)))
    return {(h, m) for h, m in found if 0 <= h <= 24 and 0 <= m < 60}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", text.lower())


def _parse_local(text: str) -> datetime | None:
    try:
        return datetime.strptime((text or "").strip()[:16].replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def verify(a: dict, messages: list[dict]) -> tuple[str, str]:
    """(confidence, warning) after a check without AI: is the date / time / weekday the AI named really in the emails?
    The AI's own confidence can only be lowered here, never raised."""
    start = _parse_local(a.get("start", ""))
    if not start:
        return "low", "The AI gave no usable date"
    texts = "\n".join(m["text"] + "\n" + "\n".join(m["invites"]) + "\n" + m["subject"] for m in messages)
    warnings = []
    level = CONFIDENCE.get(a.get("confidence"), 0)
    if (start.month, start.day) not in dates_in(texts):
        weekday_only = any(re.search(rf"\b{w}\b\.?", texts.lower()) for w in WEEKDAYS[start.weekday()])
        level = min(level, 1 if weekday_only else 0)
        warnings.append("The date is not written in the emails" if not weekday_only
                        else "Only the weekday is written in the emails, not the date")
    elif not any(m["invites"] for m in messages):
        # a weekday written right before the date must fit: "Freitag 13.11." and 13.11. has to be a Friday
        for m in messages:
            for wd, d, mo in re.findall(r"(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|monday|tuesday|"
                                        r"wednesday|thursday|friday|saturday|sunday)[,.]?\s+(?:den\s+|the\s+)?"
                                        r"(\d{1,2})\.\s?(\d{1,2})\.", m["text"].lower()):
                if (int(mo), int(d)) == (start.month, start.day) and wd not in WEEKDAYS[start.weekday()]:
                    level = 0
                    warnings.append("The weekday in the email does not fit the date")
    if (start.hour, start.minute) not in times_in(texts):
        level = min(level, 0)
        warnings.append("The time is not written in the emails")
    if start.date() < messages[0]["sent"].date() - timedelta(days=1):
        level = min(level, 0)
        warnings.append("The date is before the first email")
    if start.date() > messages[-1]["sent"].date() + timedelta(days=200):
        level = min(level, 0)
        warnings.append("The date is far in the future")
    proof = _norm(a.get("proof") or "")
    if level == 2 and (not proof or proof not in _norm(texts)):
        level = 1
        warnings.append("The quoted proof was not found word for word")
    return {0: "low", 1: "medium", 2: "high"}[level], "; ".join(dict.fromkeys(warnings))


def _confirmed_possible(messages: list[dict]) -> bool:
    """"confirmed" needs a reply from ME, a calendar invite, or the company saying it confirms."""
    if any(m["mine"] or m["invites"] for m in messages):
        return True
    return bool(re.search(r"terminbest[äa]tigung|best[äa]tig\w+ (hiermit|wir|ich)|hiermit best|is confirmed|confirmed",
                          " ".join(m["text"] for m in messages), re.I))


def build_row(thread_id: str, fingerprint: str, a: dict | None, messages: list[dict]) -> dict:
    """The database row for one thread, from the AI's answer (None: the emails do not look like an interview)."""
    row = {"thread_id": thread_id, "fingerprint": fingerprint, "state": "none", "subject": messages[-1]["subject"]}
    if not a or not a.get("is_interview") or a.get("cancelled"):
        return row
    start = _parse_local(a.get("start", ""))
    if not start:
        return row
    end = _parse_local(a.get("end", ""))
    if end and end <= start:
        end = None
    confidence, warning = verify(a, messages)
    kind = a.get("type") if a.get("type") in KINDS else "unknown"
    links = [link for m in messages for link in m["links"]]
    place = str(a.get("location_or_link") or "").strip()[:200] or (links[-1] if links else "")
    if kind == "unknown" and links:
        kind = "teams" if "teams." in links[-1] else "video"
    status = a.get("status") if a.get("status") in ("confirmed", "waiting") else "waiting"
    if status == "confirmed" and not _confirmed_possible(messages):
        status = "waiting"
    note = "; ".join(x for x in (str(a.get("note") or "").strip()[:120], warning) if x)
    to_utc = lambda d: d.replace(tzinfo=clock.GERMANY).astimezone(timezone.utc).isoformat(timespec="seconds")
    row.update(state="ok", company=str(a.get("company") or "").strip()[:120], job_title=str(a.get("job_title") or "").strip()[:160],
               start_at=to_utc(start), end_at=to_utc(end) if end else None, kind=kind, place=place, status=status,
               confidence=confidence, note=note)
    return row


# ---- 4. finding and reading the threads ----------------------------------------------------------

def _candidates(imap) -> dict[str, list[bytes]]:
    """thread id -> UIDs of the WHOLE thread (All Mail), for every thread that may hold an interview."""
    since = applications.start_date()
    label = str(applications.settings()["label"]).strip().replace(" ", "-")
    query = (f"-in:drafts -in:chats label:{label} after:{since:%Y/%m/%d} "
             f"{{{' '.join(SEARCH_WORDS)}}}")
    uids = gmail.search(imap, query)
    thread_ids = {t for _, t in gmail.ids_of(imap, uids).values() if t}
    thread_ids |= db.interview_threads_from_tracker()
    out = {}
    for tid in thread_ids:
        thread = gmail.thread_uids(imap, tid)
        if thread:
            out[tid] = thread
    return out


def _fingerprint(imap, uids: list[bytes]) -> str:
    ids = sorted(g for g, _ in gmail.ids_of(imap, uids).values())
    return hashlib.sha1(",".join(ids).encode()).hexdigest()[:16]


def _load(imap, thread_id: str, uids: list[bytes]) -> list[dict]:
    limit = int(settings().get("max_messages_per_thread") or 14)
    messages = []
    for uid in sorted(uids, key=int)[-limit:]:
        raw = gmail.fetch(imap, uid)
        if raw is not None:
            messages.append(read_message(raw))
    return sorted(messages, key=lambda m: m["sent"])


def _worth_asking(messages: list[dict]) -> bool:
    """No AI for a thread without any interview word or without any date / time."""
    text = " ".join(m["subject"] + " " + m["text"] for m in messages)
    if not (applications.INTERVIEW.search(text) or any(m["invites"] for m in messages)
            or re.search(r"\b(termin|gespr[äa]ch|call|meeting)\b", text, re.I)):
        return False
    return bool(dates_in(text) or any(m["invites"] for m in messages)
                or re.search(r"\b(montag|dienstag|mittwoch|donnerstag|freitag|monday|tuesday|wednesday|thursday|friday|morgen|tomorrow)\b", text, re.I))


def check() -> dict:
    """Reads the interview threads now. Returns counts; the result is also kept for the dashboard."""
    if not enabled():
        return {"ok": False, "note": "Off: no Gmail login in .env, or interviews: enabled: false"}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "note": "Already running"}
    try:
        result = _check()
    except gmail.GmailError as e:
        result = {"ok": False, "note": str(e)}
    except Exception as e:
        log.exception("Interviews: check failed")
        result = {"ok": False, "note": f"{type(e).__name__}: {str(e)[:150]}"}
    finally:
        _lock.release()
    _last.clear()
    _last.update(result, at=db.now())
    return result


def _check() -> dict:
    counts = {"ok": True, "threads": 0, "read": 0, "interviews": 0, "waiting_for_ai": 0, "note": ""}
    known = db.interview_fingerprints()
    todo = []
    imap = gmail.open_folder("All")  # read-only: EXAMINE, BODY.PEEK
    try:
        candidates = _candidates(imap)
        counts["threads"] = len(candidates)
        for tid, uids in candidates.items():
            fp = _fingerprint(imap, uids)
            if known.get(tid) == fp:
                continue  # no new email in this thread since it was read
            if len(todo) >= int(settings().get("max_threads_per_run") or 12):
                counts["waiting_for_ai"] += 1
                continue
            messages = _load(imap, tid, uids)
            if messages:
                todo.append({"thread_id": tid, "fingerprint": fp, "messages": messages})
    finally:
        gmail._logout(imap)
    counts["read"] = len(todo)
    asked = []
    for t in todo:
        if _worth_asking(t["messages"]):
            asked.append(t)
        else:
            db.save_interview(build_row(t["thread_id"], t["fingerprint"], None, t["messages"]))
    batches, current = [], []
    for t in asked:  # up to 3 threads in one AI request, within the size limit
        size = sum(len(thread_text(x["messages"])) for x in current + [t])
        if current and (len(current) >= 3 or size > MAX_PROMPT_CHARS):
            batches.append(current)
            current = []
        current.append(t)
    if current:
        batches.append(current)
    for n, batch in enumerate(batches):
        try:
            try:
                answers = ask(batch)
            except ai.BrokenAnswer:
                if len(batch) == 1:
                    raise
                answers = {}
                for t in batch:  # a broken answer for 3 threads: one by one
                    answers.update(ask([t]))
        except (ai.RateLimited, ai.Overloaded) as e:  # includes BudgetSaved: the rest waits for the next round
            counts["waiting_for_ai"] += sum(len(b) for b in batches[n:])
            counts["note"] = ai.stop_note(e, "the next round")
            break
        except Exception as e:
            log.warning("Interviews: AI did not answer (%s)", str(e)[:150])
            counts["waiting_for_ai"] += sum(len(b) for b in batches[n:])
            counts["note"] = "The AI did not give a usable answer, trying again next round"
            break
        for t in batch:
            a = answers.get(t["thread_id"])
            if a is None and _misses.get(t["thread_id"], 0) < 2:
                _misses[t["thread_id"]] = _misses.get(t["thread_id"], 0) + 1
                counts["waiting_for_ai"] += 1
                continue  # the AI skipped it: not saved, read again next round
            _misses.pop(t["thread_id"], None)
            db.save_interview(build_row(t["thread_id"], t["fingerprint"], a, t["messages"]))
    counts["interviews"] = len(db.interviews())
    if todo:
        log.info("Interviews: %d threads found, %d read, %d interviews, %d waiting for the AI",
                 counts["threads"], counts["read"], counts["interviews"], counts["waiting_for_ai"])
    return counts


def check_in_background() -> None:
    if enabled():
        threading.Thread(target=check, daemon=True, name="interviews").start()


def scheduled() -> None:
    check()


# ---- 5. what the dashboard gets -----------------------------------------------------------------

def _gmail_link(thread_id: str) -> str:
    try:
        return f"https://mail.google.com/mail/u/0/#all/{int(thread_id):x}"
    except ValueError:
        return ""


def overview() -> dict:
    now = datetime.now(timezone.utc)
    items = []
    for r in db.interviews():
        start = datetime.fromisoformat(r["start_at"])
        end = datetime.fromisoformat(r["end_at"]) if r["end_at"] else None
        over = (end or start + DEFAULT_LENGTH) < now
        s, e = clock.german(start), clock.german(end) if end else None
        items.append({"thread_id": r["thread_id"], "company": r["company"] or "Unknown company", "job_title": r["job_title"] or "",
                      "date": f"{s:%Y-%m-%d}", "start": f"{s:%H:%M}", "end": f"{e:%H:%M}" if e else None,
                      "end_date": f"{e:%Y-%m-%d}" if e else None, "kind": r["kind"], "kind_label": KIND_LABELS.get(r["kind"], "Type unknown"),
                      "place": r["place"] or "", "status": "past" if over else r["status"], "agreed": r["status"],
                      "confidence": r["confidence"], "note": r["note"] or "", "subject": r["subject"] or "",
                      "gmail_url": _gmail_link(r["thread_id"])})
    items.sort(key=lambda i: (i["date"], i["start"]))
    german_now = clock.now()
    return {"enabled": enabled(), "today": f"{german_now:%Y-%m-%d}", "now": f"{german_now:%H:%M}",
            "upcoming": sum(1 for i in items if i["status"] != "past"), "interviews": items, "last": last_result()}
