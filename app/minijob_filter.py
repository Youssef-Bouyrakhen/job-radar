"""Free filter for the Minijob section (no AI). Pure text rules, so they are easy to test.

An ad passes when it is a Minijob, is 100% home office, has no phone work (strong words), no clearly
too high MONTHLY pay and no scam signs. Hourly pay, ranges and "bis zu" are never compared.
"""
import re
from dataclasses import dataclass

I = re.I

# A Minijob (the Arbeitsagentur also sends its own flag, see minijob.py)
MINIJOB = re.compile(r"mini-?\s?job|geringf(?:ü|ue)gig|(?:450|520|538|556|603)\s*(?:€|euro|eur)\b|520[- ]?(?:€|euro)-?basis", I)

# 100% home office / remote
FULL_REMOTE = re.compile(
    r"100\s*(?:%|prozent)\s*[-–]?\s*(?:home[\s-]?office|remote|mobil|ortsunabh\w+|von\s+zu\s?hause)|"
    r"(?<![\wäöüß])(?:komplett|vollst(?:ä|ae)ndig|voll|ausschlie(?:ß|ss)lich|rein|nur|ganz)e?s?\s+"
    r"(?:(?:im|von|aus\s+dem)\s+(?:home[\s-]?office|remote|zu\s?hause)|home[\s-]?office|remote)|"
    r"(?:fully|100\s*%|completely)\s+remote|remote[\s-]?(?:job|minijob|only|first)|"
    r"home[\s-]?office[\s-]?(?:job|stelle|minijob)|heimarbeit|"
    r"(?:arbeit|arbeiten|t(?:ä|ae)tig)\s+(?:komplett\s+|ausschlie(?:ß|ss)lich\s+)?(?:von\s+zu\s?hause|von\s+daheim|remote)|"
    r"(?:ohne|kein(?:e)?)\s+(?:b(?:ü|ue)ro|pr(?:ä|ae)senz)|bundesweit\s+remote|deutschlandweit\s+remote|"
    r"(?:von\s+)?(?:\w+\s+)?(?:zu\s?hause|daheim)\s+aus\s+arbeiten", I)

# Not 100%: hybrid, only some days, "Homeoffice möglich", "nach Vereinbarung"
NOT_FULL = re.compile(
    r"hybrid|teilweise\s+(?:im\s+)?(?:home|remote|mobil)|teils\s+(?:im\s+)?(?:home|remote)|zum\s+teil\s+(?:im\s+)?(?:home|remote|mobil)|"
    r"(?:zwei|drei|1|2|3|4|ein|zwei)\s*(?:bis\s+\w+\s+)?tage?\s+(?:pro\s+woche\s+)?(?:im\s+)?(?:home|b(?:ü|ue)ro|vor\s+ort)|"
    r"(?:gelegentlich|nach\s+bedarf)\s+(?:im\s+)?(?:home|b(?:ü|ue)ro|vor\s+ort)|"
    r"sowohl\s+im\s+b(?:ü|ue)ro\s+als\s+auch|b(?:ü|ue)ro\s+(?:und|oder|bzw\.?)\s+(?:im\s+)?home[\s-]?office|"
    r"home[\s-]?office\s+(?:und|oder)\s+(?:im\s+)?b(?:ü|ue)ro|nach\s+(?:r(?:ü|ue)cksprache|absprache)\s+(?:auch\s+|ggf\.?\s+)?(?:remote|home)|"
    r"pr(?:ä|ae)senz(?:pflicht|tage|termin)|vor\s+ort\s+(?:im|in\s+der)\s+(?:b(?:ü|ue)ro|filiale|gesch(?:ä|ae)ft)", I)

# Only a maybe: "Homeoffice möglich / Möglichkeit". With a clear full-remote phrase in the same ad ("vollständig von
# zu Hause") the ad goes on to the AI, which decides. Without one it is dropped.
SOFT_MAYBE = re.compile(
    r"(?:home[\s-]?office|remote|mobiles?\s+arbeiten)\s+(?:ist\s+|ab\s+und\s+zu\s+|auch\s+|teilweise\s+)?(?:m(?:ö|oe)glich|nach\s+vereinbarung|je\s+nach)", I)

# Phone work: STRONG words only, anywhere in the text. Plain "Telefon" / "telefonisch" is NOT one (often a contact line).
PHONE = re.compile(r"call[\s-]?cent(?:er|re)|hotline|telefonist|outbound|akquise|kundenanrufe|kunden[\s-]?anrufe|"
                   r"telefonmarketing|telefonverkauf|telesales|telefoninterview\w*|telefonumfrage\w*|"
                   r"telefon\w*[^.\n]{0,60}marktforschung|marktforschung[^.\n]{0,60}telefon", I)

# Commission-only pay (selling jobs). A negated one ("nicht nur Provision", "keine rein erfolgsabhängige ...") does not count.
COMMISSION = re.compile(r"(?<![\wäöüß])nur\s+(?:auf\s+)?provision\w*|provisionsbasi\w*|rein\s+erfolgsabh(?:ä|ae)ngig\w*|"
                        r"verg(?:ü|ue)tung\s+auf\s+provisionsbasis", I)
# Offered as a Minijob AND full time in the same ad ("Vollzeit/Teilzeit/Minijob"): kept only if the text clearly says Minijob,
# and the AI score is capped (MIXED_MAX_SCORE in minijob.py)
MIXED_TYPE = re.compile(r"vollzeit[^.\n]{0,40}mini-?\s?job|mini-?\s?job[^.\n]{0,40}vollzeit", I)

# A strong phone word that is negated does not count: "keine Akquise", "kein Call Center", "ohne Hotline",
# "keine Kaltakquise oder Kundenanrufe", "hotline-frei", "akquisefrei"
NEGATED_BEFORE = re.compile(r"(?<![\wäöüß])(?:kein\w*|ohne|nicht|weder|noch|frei\s+von|verzicht\w*\s+auf|no|without)\s+"
                            r"(?:[\wäöüß-]+\s+){0,2}[\wäöüß-]*$", I)
NEGATED_AFTER = re.compile(r"^[\wäöüß]{0,12}[-\s]*(?:frei|los|free)(?![\wäöüß])", I)


def phone_word(text: str) -> str | None:
    """The first strong phone word that is used positively (not negated), or None."""
    for m in PHONE.finditer(text):
        before = re.split(r"[.;:!?\n]", text[max(0, m.start() - 40):m.start()])[-1]
        if NEGATED_BEFORE.search(before) or NEGATED_AFTER.search(text[m.end():m.end() + 20]):
            continue
        return m[0]
    return None


# "gegebenenfalls Remote-Arbeit", "nach Absprache Homeoffice": a maybe is not 100%
CONDITIONAL_BEFORE = re.compile(r"(?:gegebenenfalls|ggf\.?|eventuell|m(?:ö|oe)glicherweise|bei\s+bedarf|"
                                r"nach\s+(?:r(?:ü|ue)cksprache|absprache|vereinbarung))[^\w]+(?:[\wäöüß-]+[^\w]+){0,3}$", I)


def full_remote(text: str) -> str | None:
    """The first clear "100% home office" phrase that is not negated ("kein Homeoffice-Job") or only a maybe."""
    for m in FULL_REMOTE.finditer(text):
        before = re.split(r"[.;:!?\n]", text[max(0, m.start() - 50):m.start()])[-1]
        if not (NEGATED_BEFORE.search(before) or CONDITIONAL_BEFORE.search(before)):
            return m[0]
    return None


# Scam signs
SCAM = re.compile(
    r"vorkasse|vorab\s+(?:bezahl|zahl|überweis|ueberweis)|(?:geb(?:ü|ue)hr|kaution|startkapital|kosten)\s+(?:vorab|im\s+voraus)|"
    r"(?:ihr|dein|euer)\s*(?:e|en|em|er)?\s*(?:eigene[nmrs]?\s+|privat\w*\s+)?(?:bank)?konto\s+(?:zur\s+verf|nutzen|bereit|zu\s+nutzen)|"
    r"eigene[nmrs]?\s+(?:bank)?konto|konto\s+(?:f(?:ü|ue)r\s+(?:zahlungen|transaktionen)|zur\s+verf(?:ü|ue)gung)|"
    # your own account / your own money (v14.3): reselling schemes that post ads from YOUR eBay Kleinanzeigen account
    r"eigene[nmrs]?\s+[\w\s-]{0,30}?(?:konto|account)|(?:ebay[\s-]*)?kleinanzeigen[\s-]*(?:konto|account)|verkaufskonto|"
    r"\bkaution\b|\bstartkapital\b|(?:equipment|ausr(?:ü|ue)stung|material|ware)\s+selbst\s+(?:kaufen|bezahlen|besorgen|finanzieren)|"
    r"(?:finanzagent|zahlungsagent|geldw(?:ä|ae)sche)|zahlungen?\s+weiter(?:leiten|geben)|geld\s+weiter(?:leiten|geben)|"
    r"(?:paket|waren)\s*(?:annahme|weiterleit)|reshipping|"
    r"\b[1-9]\d{2,3}\s*(?:€|euro|eur)\s*(?:pro|am|je|/)\s*tag|t(?:ä|ae)glich\s+(?:bis\s+zu\s+)?\d{3,4}\s*(?:€|euro|eur)|"
    r"schnell\s+(?:reich|geld)|ohne\s+(?:jegliche\s+)?(?:arbeit|aufwand)\s+geld|passives?\s+einkommen\s+garantiert", I)

MONTH_WORDS = r"(?:monat(?:lich)?|monatsgehalt|monatsverdienst|im\s+monat|pro\s+monat|/\s*monat|mtl\.?|/\s*mon\.?)"
AMOUNT = r"(\d{1,2}(?:[.,]\d{3})|\d{3,5})(?:[.,]\d{2})?"
CUR = r"\s*(?:€|(?:euro|eur)\b)"
# "700 € pro Monat" / "700 Euro monatlich" and "monatlich 700 €" / "Verdienst: 700 € im Monat"
PAY_AFTER = re.compile(AMOUNT + CUR + r"\s*(?:brutto\s*|netto\s*)?" + MONTH_WORDS, I)
PAY_BEFORE = re.compile(MONTH_WORDS + r"\s*(?:von|:|ca\.?|circa|rund|ab)?\s*" + AMOUNT + CUR, I)
SKIP_BEFORE = re.compile(r"(?:bis\s+zu|bis|max\.?|maximal|höchstens|up\s+to|ab|mindestens|mind\.?|[-–—]|\d\s*(?:€|euro)?\s*(?:bis|-|–))\s*$", I)
HOURLY_NEAR = re.compile(r"stunde|std|/\s*h\b|hourly|pro\s+h\b", I)


@dataclass
class Verdict:
    passed: bool
    reason: str
    code: str = ""    # not_minijob / not_remote / phone / pay / scam ("" = passed)
    words: str = ""   # the words that decided it


def monthly_amounts(text: str) -> list[float]:
    """Clear MONTHLY euro amounts in the text. Not counted: hourly pay, "bis zu"/"ab"/ranges (the text before the number says so)."""
    found = []
    for rule in (PAY_AFTER, PAY_BEFORE):
        for m in rule.finditer(text):
            before = text[max(0, m.start() - 14):m.start(1)] if rule is PAY_AFTER else text[max(0, m.start() - 14):m.start()]
            around = text[max(0, m.start() - 25):m.end() + 25]
            if SKIP_BEFORE.search(before) or HOURLY_NEAR.search(around):
                continue
            raw = m.group(1).replace(".", "").replace(",", "")
            found.append(float(raw))
    return found


def check(title: str, description: str, *, flag_minijob: bool = False, flag_remote_100: bool = False,
          flag_not_full: bool = False, max_monthly: float = 650) -> Verdict:
    """flag_*: what the source itself says (Arbeitsagentur: Minijob flag, home office percent)."""
    text = f"{title}\n{description}"
    if scam := SCAM.search(text):
        return Verdict(False, f"Scam sign ('{scam[0].strip()}')", "scam", scam[0].strip())
    if word := phone_word(text):
        return Verdict(False, f"Phone work ('{word}')", "phone", word)
    for m in COMMISSION.finditer(text):
        before = re.split(r"[.;:!?\n]", text[max(0, m.start() - 40):m.start()])[-1]
        if not NEGATED_BEFORE.search(before):
            return Verdict(False, f"Commission only ('{m[0]}')", "commission", m[0])
    if re.search(r"werkstudent", title, I):
        return Verdict(False, "Not a Minijob (Werkstudent in the title)", "not_minijob", "Werkstudent")
    if not (flag_minijob or MINIJOB.search(text)):
        return Verdict(False, "Not a Minijob", "not_minijob")
    hybrid = NOT_FULL.search(text)
    if flag_not_full or hybrid:
        words = hybrid[0] if hybrid else "the ad's own home office percent is below 100"
        return Verdict(False, "Not 100% home office (hybrid / office days)", "not_remote", words)
    full = flag_remote_100 or full_remote(text)
    if not full:
        maybe = SOFT_MAYBE.search(text)
        return Verdict(False, "Home office not clearly 100%", "not_remote",
                       f"only '{maybe[0]}'" if maybe else "no clear 100% home office phrase")
    amounts = [a for a in monthly_amounts(text) if a > max_monthly]
    if amounts:
        return Verdict(False, f"Pay above Minijob limit ({amounts[0]:g} EUR per month)", "pay", f"{amounts[0]:g} EUR monthly")
    return Verdict(True, "Minijob + 100% home office")


# ---- score caps (v14.3): the ad is kept, but its AI score may not go above the cap (see minijob.py) ----------------

# Commission / brokerage jobs: the pay depends on sales or placements. A negated "keine Provision" does not count.
BROKER = re.compile(r"vermittler\w*|variable[rnms]?\s+verg(?:ü|ue)tung|provision(?:s\w*|en)?(?![\wäöüß])|vertriebspartner\w*", I)
# Words in front of "/ Homeoffice" that are NOT a place ("Minijob / Homeoffice", "Teilzeit, Remote")
NOT_A_PLACE = {"minijob", "mini-job", "teilzeit", "vollzeit", "tz", "vz", "büro", "buero", "flexibel", "flexible", "mobil",
               "mobiles", "arbeiten", "komplett", "vollständig", "deutschland", "deutschlandweit", "bundesweit", "germany",
               "mwd", "remote", "homeoffice", "home-office", "home", "office", "minijobber", "aushilfe", "nebenjob"}
HYBRID_PAIR = re.compile(r"([\wäöüß.-]{3,})\s*[/|·,–]\s*(home[\s-]?office|remote)|"
                         r"(home[\s-]?office|remote)\s*[/|·,–]\s*([\wäöüß.-]{3,})", I)
# A place that already says it is remote (the alert email's location line)
REMOTE_PLACE = re.compile(r"home[\s-]?office|remote|von\s+zu\s?hause|mobil\w*\s+arbeit|deutschland|bundesweit", I)


def broker_word(text: str) -> str | None:
    """The first commission / brokerage word that is used positively, or None."""
    for m in BROKER.finditer(text):
        before = re.split(r"[.;:!?\n]", text[max(0, m.start() - 40):m.start()])[-1]
        if not NEGATED_BEFORE.search(before):
            return m[0]
    return None


def alert_not_full_remote(title: str, location: str | None) -> bool:
    """Alert-email ad whose place is a real city and whose title does not say 100% remote, or a title like
    "München / Homeoffice" (= hybrid): True. A location that says Home Office / Remote keeps the ad."""
    if full_remote(title):
        return False
    for m in HYBRID_PAIR.finditer(title):
        word = m.group(1) or m.group(4)
        if word[0].isupper() and word.lower() not in NOT_A_PLACE:
            return True
    loc = (location or "").strip()
    return bool(loc) and not REMOTE_PLACE.search(loc)


def check_unverified(title: str) -> Verdict:
    """Ads from alert emails: only the title is known, so Minijob + home office cannot be checked here. Only
    scam and phone words in the title drop the ad; everything else is passed on, flagged "unverified"."""
    if scam := SCAM.search(title):
        return Verdict(False, f"Scam sign ('{scam[0].strip()}')", "scam", scam[0].strip())
    if word := phone_word(title):
        return Verdict(False, f"Phone work ('{word}')", "phone", word)
    return Verdict(True, "From an alert email: Minijob and home office not checked in the ad")
