"""Recognising the same job twice, even when two sources write it slightly differently.

Example: "Werkstudent Innovation, Data Science & Automation (all genders)" at
"H World International Germany" and "Werkstudent Innovation, Data Science & Automation
(all genders), H World International" at "H World International - Team Center" are one job.
"""
import re
from collections import Counter
from difflib import SequenceMatcher

from .extractors.generic import GENDER

LEGAL_FORMS = re.compile(r"\b(gmbh|ag|se|kg|kgaa|co|mbh|ug|ev|e\.v|inc|ltd|llc|plc|group|gruppe|holding)\b")
FILLER = {"germany", "deutschland", "europe", "de", "the", "der", "die", "das", "und", "and", "fur", "für"}
WORD = re.compile(r"[a-z0-9äöüß]+")


def _words(text: str) -> list[str]:
    return WORD.findall((text or "").lower())


def company_words(company: str) -> set[str]:
    return set(_words(LEGAL_FORMS.sub(" ", (company or "").lower()))) - FILLER


def title_words(title: str, ignore: set[str]) -> list[str]:
    """Title words without gender markers, company names and place names."""
    return [w for w in _words(GENDER.sub(" ", title or "")) if w not in ignore and w not in FILLER]


def dedup_key(title: str, company: str) -> str:
    """Exact key: simplified "title|company"."""
    return f"{' '.join(title_words(title, set()))}|{' '.join(sorted(company_words(company)))}"


def similar_company(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return a == b
    if a <= b or b <= a:
        return True
    return len(a & b) / len(a | b) >= 0.5


def prepared(job: dict) -> tuple[set[str], set[str], list[str]]:
    """What same_job compares, worked out once per job: its company words, place words and title words
    (before the company and place words are left out)."""
    return company_words(job["company"]), set(_words(job.get("location"))), title_words(job["title"], set())


def ratio_at_least(a: str, b: str, limit: float) -> bool:
    """SequenceMatcher(None, a, b).ratio() >= limit, with its two quick upper bounds first (the lengths, then
    the letters both have, the same sums as difflib's real_quick_ratio / quick_ratio): below the limit there,
    the slow ratio can't reach it."""
    total = len(a) + len(b)
    if 2.0 * min(len(a), len(b)) / total < limit:
        return False
    if 2.0 * sum((Counter(a) & Counter(b)).values()) / total < limit:
        return False
    return SequenceMatcher(None, a, b).ratio() >= limit


def same_prepared(a: tuple, b: tuple) -> bool:
    """same_job for two prepared() jobs."""
    (ca, la, wa), (cb, lb, wb) = a, b
    if not similar_company(ca, cb):
        return False
    # Ignore company words in titles, and place names both jobs share
    # ("... - Frankfurt" vs "..."), but keep different cities apart.
    ignore = ca | cb | (la & lb)
    ta, tb = [w for w in wa if w not in ignore], [w for w in wb if w not in ignore]
    if not ta or not tb:
        return False
    if set(ta) == set(tb):
        return True
    return ratio_at_least(" ".join(ta), " ".join(tb), 0.93)


def same_job(a: dict, b: dict) -> bool:
    """a, b: dicts with title, company, location."""
    return same_prepared(prepared(a), prepared(b))


def find_duplicate(job: dict, known: list[dict]) -> dict | None:
    for other in known:
        if same_job(job, other):
            return other
    return None


def find_duplicates(jobs: list[dict]) -> list[tuple[dict, dict]]:
    """(original, copy) for every job that is the same as an earlier one, in order: the same result as
    find_duplicate against all jobs kept so far, but fast. Two jobs can only be the same when their
    companies share a word (similar_company; or both have none), so only those are compared."""
    kept, by_word, found = [], {}, []  # kept: (job, prepared); by_word: company word ("" = none) -> positions in kept
    for job in jobs:
        p = prepared(job)
        words = p[0] or {""}
        candidates = sorted({i for w in words for i in by_word.get(w, ())})
        original = next((kept[i][0] for i in candidates if same_prepared(p, kept[i][1])), None)
        if original:
            found.append((original, job))
            continue
        for w in words:
            by_word.setdefault(w, []).append(len(kept))
        kept.append((job, p))
    return found
