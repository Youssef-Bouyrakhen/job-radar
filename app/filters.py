"""Layer 1 filter (free): keywords + part-time rule + working hours detection.

The keyword lists live in config.yaml under "filter:". Keyword syntax:
  Software     -> the whole word "Software" (also "IT-Software", not "Softwareentwickler")
  Software*    -> words that START with it: "Software", "Softwareentwickler"
  *entwickler  -> words that END with it: "Entwickler", "Softwareentwickler"
  *informatik* -> words that CONTAIN it: "Informatik", "Wirtschaftsinformatiker"
Short words in CAPITALS (like IT, KI, AI, SAP) must match in capitals, so "IT" never matches "mit".
"""
import re
from dataclasses import dataclass

from .config import CONFIG

LETTER = r"[a-zA-Z0-9äöüÄÖÜß]"


def _pattern(keyword: str) -> str:
    kw = keyword.strip()
    start = f"{LETTER}*" if kw.startswith("*") else f"(?<!{LETTER})"
    end = f"{LETTER}*" if kw.endswith("*") else f"(?!{LETTER})"
    body = re.escape(kw.strip("*")).replace(r"\ ", r"[\s\-]+")  # "Working Student" = "Working-Student"
    body = body.replace(r"\*", f"{LETTER}*")  # a * inside: "Studentische* Hilfskraft" = "Studentischer Hilfskraft"
    case = "" if (kw.strip("*").isupper() and len(kw.strip("*")) <= 4) else "(?i:"
    return f"{case}{start}{body}{end}{')' if case else ''}"


class Keywords:
    def __init__(self, words: list[str] | None):
        self.words = [w for w in (words or []) if w and w.strip("* ")]
        self.regex = re.compile("|".join(f"({_pattern(w)})" for w in self.words)) if self.words else None

    def find(self, text: str) -> str | None:
        """The first keyword found in the text (as written in the text), or None."""
        if not self.regex or not text:
            return None
        m = self.regex.search(text)
        return m[0] if m else None

    def find_all(self, text: str) -> set[str]:
        if not self.regex or not text:
            return set()
        return {m[0].lower() for m in self.regex.finditer(text)}


# ---- working hours --------------------------------------------------------------

NUM = r"(\d{1,2}(?:[.,]5)?)"
HOURS = re.compile(
    rf"(?:(bis zu|max\.?|maximal|höchstens|up to)\s*)?{NUM}"
    rf"(?:\s*(?:-|–|—|bis|to|und|and)\s*{NUM})?"
    r"\s*(wochenstunden|stunden|std\b\.?|hours|hrs|h\b)",
    re.I,
)
WEEK_CONTEXT = re.compile(r"woche|week|wöchentl|weekly|/\s*w\b|pro\s+w|p\.\s?w|arbeitszeit|working time", re.I)


@dataclass
class Hours:
    text: str          # e.g. "15–20 h/week"
    low: float
    high: float


def detect_hours(text: str) -> Hours | None:
    for m in HOURS.finditer(text or ""):
        upto, first, second, unit = m.groups()
        around = text[max(0, m.start() - 40):m.end() + 30]
        if "wochenstunden" not in unit.lower() and not WEEK_CONTEXT.search(around):
            continue  # "24 Stunden Service" is not about working hours
        low = float(first.replace(",", "."))
        high = float(second.replace(",", ".")) if second else low
        if not (1 <= low <= high <= 48):
            continue
        fmt = lambda x: f"{x:g}"
        if upto:
            return Hours(f"up to {fmt(high)} h/week", 0, high)
        label = f"{fmt(low)}–{fmt(high)} h/week" if high != low else f"{fmt(high)} h/week"
        return Hours(label, low, high)
    return None


# ---- the filter ----------------------------------------------------------------

@dataclass
class FilterResult:
    passed: bool
    reason: str
    hours: str = ""


class JobFilter:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.student = Keywords(cfg.get("student_keywords"))
        self.it = Keywords(cfg.get("it_keywords"))
        self.fulltime = Keywords(cfg.get("fulltime_keywords"))
        self.never = Keywords(cfg.get("exclude_keywords"))
        self.unless_student = Keywords(cfg.get("exclude_unless_student_keywords"))
        self.max_hours = cfg.get("max_hours_per_week", 20)
        self.min_it_words_in_description = cfg.get("min_it_words_in_description", 2)

    def check(self, title: str, employment: str = "", description: str = "") -> FilterResult:
        head = f"{title} | {employment}"         # title + employment type: strong signals
        everything = f"{head} | {description}"
        hours = detect_hours(everything)
        hours_text = hours.text if hours else ""
        result = lambda ok, reason: FilterResult(ok, reason, hours_text)

        if not self.enabled:
            return result(True, "Filter is turned off")

        if kw := self.never.find(title):
            return result(False, f"Not a student job ('{kw}')")

        student_in_head = self.student.find(head)
        if (kw := self.unless_student.find(title)) and not student_in_head:
            return result(False, f"Internship / thesis, not Werkstudent ('{kw}')")

        student_anywhere = student_in_head or self.student.find(description)
        part_time_hours = hours and hours.high <= self.max_hours
        full_time_hours = hours and hours.low >= 30
        fulltime_kw = self.fulltime.find(everything)
        if (fulltime_kw or full_time_hours) and not student_anywhere and not part_time_hours:
            return result(False, f"Full-time ('{fulltime_kw or hours_text}')")
        if hours and hours.low > self.max_hours and not student_in_head:
            return result(False, f"Too many hours ({hours_text})")

        if self.student.words and not (student_anywhere or part_time_hours):
            return result(False, "Not a Werkstudent / part-time job")

        if self.it.words:
            it_kw = self.it.find(head)
            if not it_kw:
                found = self.it.find_all(description)
                if len(found) < self.min_it_words_in_description:
                    return result(False, "Not IT-related")
                it_kw = ", ".join(sorted(found)[:3])
            return result(True, f"{student_anywhere or hours_text} + {it_kw}")
        return result(True, student_anywhere or hours_text)


def get_filter() -> JobFilter:
    return JobFilter(CONFIG.get("filter") or {})
