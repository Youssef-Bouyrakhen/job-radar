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

NUM = r"(\d{1,3}(?:[.,]5)?)"
HOURS = re.compile(
    rf"(?:(bis zu|max\.?|maximal|höchstens|up to|mind\.?|mindestens|min\.|at least|\bab)\s*)?{NUM}"
    rf"(?:\s*(?:-|–|—|bis|to|und|and)\s*{NUM})?"
    r"\s*(wochenstunden|monatsstunden|stunden|std\b\.?|hours|hrs|h\b)",
    re.I,
)
# The time unit next to the number. "m/w/d" is not "per week".
WEEK = re.compile(r"wochenstunden|woche|week|wöchentl|weekly|(?<![a-z])/\s*w(?:k|oche)?\b(?!\s*/)|pro\s+w|p\.\s?w", re.I)
MONTH = re.compile(r"monatsstunden|monat|month|/\s*mon?\b\.?(?!\s*/)|im\s+mon", re.I)
# Hours without a unit, but clearly weekly working hours ("Arbeitszeit: 40 Stunden", "Werkstudent (m/w/d) 20 Stunden")
WORK = re.compile(r"arbeitszeit|working (?:time|hours)|arbeiten|work|werk\s?-?student|teilzeit|part[- ]time|"
                  r"hilfskraft|\bhiwi|student(?:ische[rn]?)?\s+assistan", re.I)
WEEKS_PER_MONTH = 4.33
# Student assistant (HiWi) jobs at universities and research institutes are paid by the hour per MONTH
HIWI = re.compile(r"hilfskraft|\bhiwi|student(?:ische[rn]?)?\s+assistan|research assistant|studentische\s+mitarbeit", re.I)
ACADEMIC = re.compile(r"universit|hochschule|\bh[-_]da\b|fraunhofer|max[- ]planck|\bmpi\b|helmholtz|leibniz|\bgsi\b|"
                      r"\bdlr\b|forschung|institut|research|\btu\s|tu-darmstadt|\buni[- ]|akademie|\.edu\b", re.I)


def is_academic(title: str, employer: str) -> bool:
    """A HiWi / Hilfskraft / Student Assistant job at a university or research institute."""
    return bool(HIWI.search(title or "")) and bool(ACADEMIC.search(employer or ""))


@dataclass
class Hours:
    text: str          # e.g. "15–20 h/week", "40 h/month ≈ 9 h/week"
    low: float         # per week
    high: float        # per week: the most this job can mean
    per_month: bool = False


def _nearest_unit(text: str, start: int, end: int) -> str | None:
    """"week" / "month": the unit word closest to the number ("40 Std./Monat", "monthly working time are 40 hours")."""
    best = None
    for unit, rule in (("week", WEEK), ("month", MONTH)):
        for m in rule.finditer(text, max(0, start - 60), min(len(text), end + 30)):
            gap = start - m.end() if m.end() <= start else m.start() - end if m.start() >= end else 0
            if best is None or gap < best[0]:
                best = (gap, unit)
    return best[1] if best else None


def detect_hours(text: str, academic: bool = False, assume_week: bool = False) -> Hours | None:
    """Working hours in an ad. Per month ("pro Monat", "h/Monat", "monthly") is turned into per week (/ 4.33).
    academic: a HiWi job at a university / research institute: a number above 20 without a unit is per month.
    assume_week: a number without a unit is per week (a job title: "Junior Backend Developer 20h")."""
    text = text or ""
    for m in HOURS.finditer(text):
        limit, first, second, unit = m.groups()
        low = float(first.replace(",", "."))
        high = float(second.replace(",", ".")) if second else low
        period = "week" if unit.lower() == "wochenstunden" else "month" if unit.lower() == "monatsstunden" \
            else _nearest_unit(text, m.start(), m.end())
        if period is None:
            work = WORK.search(text[max(0, m.start() - 60):m.end() + 30])
            if academic and high > 20:
                period = "month"  # "Student Assistant ... 40 hours": 40 a month
            elif work or assume_week:
                period = "week"
            else:
                continue  # "24 Stunden Service" is not about working hours
        if not (1 <= low <= high <= (48 if period == "week" else 200)):
            continue
        at_least = bool(limit) and limit.lower().startswith(("mind", "min", "at", "ab"))
        up_to = bool(limit) and not at_least
        low = 0 if up_to else low
        fmt = lambda x: f"{x:g}"
        span = lambda a, b: f"{fmt(a)}–{fmt(b)}" if a and a != b else fmt(b)
        word = "up to " if up_to else "at least " if at_least else ""
        if period == "week":
            return Hours(f"{word}{span(low, high)} h/week", low, high)
        week = lambda x: max(1, round(x / WEEKS_PER_MONTH))
        return Hours(f"{word}{span(low, high)} h/month ≈ {span(week(low) if low else 0, week(high))} h/week",
                     low / WEEKS_PER_MONTH, high / WEEKS_PER_MONTH, per_month=True)
    return None


def max_per_week(hours: str | None) -> float | None:
    """The most hours a week in a saved hours text: "15–20 h/week" -> 20, "40 h/month ≈ 9 h/week" -> 9."""
    nums = re.findall(r"\d+(?:[.,]\d+)?", (hours or "").split("≈")[-1])
    return max(float(n.replace(",", ".")) for n in nums) if nums else None


def hours_of(title: str, employment: str, description: str, employer: str = "") -> Hours | None:
    """The hours of one job, from its title, job type and description. employer: company, source and
    link (is it a university / research institute?)."""
    academic = is_academic(title, f"{employer} | {title} | {(description or '')[:600]}")
    return detect_hours(f"{title} | {employment} | {description}", academic) \
        or detect_hours(title, academic, assume_week=True)


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

    def check(self, title: str, employment: str = "", description: str = "", employer: str = "") -> FilterResult:
        """employer: company, source and link of the job (a HiWi job at a university counts hours per month)."""
        head = f"{title} | {employment}"         # title + employment type: strong signals
        everything = f"{head} | {description}"
        hours = hours_of(title, employment, description, employer)
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
