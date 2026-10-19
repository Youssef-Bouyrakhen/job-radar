"""Recognising the same job twice, even when two sources write it slightly differently.

Example: "Werkstudent Innovation, Data Science & Automation (all genders)" at
"H World International Germany" and "Werkstudent Innovation, Data Science & Automation
(all genders), H World International" at "H World International - Team Center" are one job.
"""
import re
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


def same_job(a: dict, b: dict) -> bool:
    """a, b: dicts with title, company, location."""
    ca, cb = company_words(a["company"]), company_words(b["company"])
    if not similar_company(ca, cb):
        return False
    # Ignore company words in titles, and place names both jobs share
    # ("... - Frankfurt" vs "..."), but keep different cities apart.
    ignore = ca | cb | (set(_words(a.get("location"))) & set(_words(b.get("location"))))
    ta, tb = title_words(a["title"], ignore), title_words(b["title"], ignore)
    if not ta or not tb:
        return False
    if set(ta) == set(tb):
        return True
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.93


def find_duplicate(job: dict, known: list[dict]) -> dict | None:
    for other in known:
        if same_job(job, other):
            return other
    return None
