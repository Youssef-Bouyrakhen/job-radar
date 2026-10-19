"""Shared pieces for all job extractors."""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup


@dataclass
class Job:
    title: str
    url: str
    company: str = ""
    location: str = ""
    description: str = ""
    employment: str = ""  # e.g. "Teilzeit", "Working student", "16-20 h/week" when the source says it
    posted: str = ""      # when the job was posted, if the source says it: "2026-10-14" or a full ISO timestamp


def clean(text: str | None, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def html_to_text(html: str | None, limit: int = 5000) -> str:
    if not html:
        return ""
    return clean(BeautifulSoup(html, "html.parser").get_text(" "), limit)


def to_iso(value) -> str:
    """Turn the many date formats job sites use into ISO text ("" if unknown).

    Day-only dates stay day-only ("2026-10-14"); times get a timezone.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):  # Unix timestamp, in seconds or milliseconds
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")
    text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        m = re.search(r"\d{4}-\d{2}-\d{2}", text) or re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if not m:
            return relative_date(text)
        if m.lastindex == 3:  # German "14.10.2026"
            return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
        return m[0]
    if dt.tzinfo is None:
        dt = dt.astimezone()  # no timezone given: assume local (German) time
    return dt.isoformat(timespec="seconds")


_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "einem": 1, "einer": 1, "ein": 1, "eine": 1}


def relative_date(text: str) -> str:
    """'vor 3 Tagen', 'Posted 2 Days Ago', 'heute', 'yesterday', ... -> ISO date ("" if unknown)."""
    t = (text or "").lower()
    today = date.today()
    if re.search(r"heute|today|just posted|gerade|minute|stunde|hour", t):
        m = re.search(r"(\d+)\s*(?:stunde|hour|std)", t)
        if m:
            return (datetime.now(timezone.utc) - timedelta(hours=int(m[1]))).isoformat(timespec="seconds")
        return today.isoformat()
    if re.search(r"gestern|yesterday", t):
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+|an?|one|eine[mr]?|ein)\+?\s*(tag|day|woche|week|monat|month)", t)
    if not m:
        return ""
    n = _NUMBER_WORDS.get(m[1]) or int(m[1])
    days = n * {"tag": 1, "day": 1, "woche": 7, "week": 7, "monat": 30, "month": 30}[m[2]]
    return (today - timedelta(days=days)).isoformat()
