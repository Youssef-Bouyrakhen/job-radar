"""Company categories (the industry method): Insurance, Banks/Finance, Cloud/Data centers/Networks,
Cybersecurity, IT services/Software, or Other.

1. Rules first (no AI): the name and website ("R+V Versicherung", "secunet.com"), then the job titles
   (Arbeitsagentur ads and the jobs read from its careers page).
2. The AI only for the companies the rules can't place: 80 per request, at most
   companies.max_category_ai_per_day requests a day, companies with a careers page first.
A category you pick yourself is never changed.
"""
import json
import logging
import re
from collections import Counter

from . import ai, clock, db
from .config import CONFIG
from .discover import company_key

log = logging.getLogger("jobradar")

LABELS = {"insurance": "Insurance", "finance": "Banks/Finance", "cloud": "Cloud/Data centers/Networks",
          "security": "Cybersecurity", "software": "IT services/Software", "other": "Other"}

# Not what the words below suggest: guards ("Kötter Security"), alarm systems, staffing agencies, power grids
NOT_IT = re.compile(r"sicherheitsdienst|bewachung|objektschutz|werkschutz|sicherheitskr[äa]ft|schutz- und|"
                    r"brandschutz|arbeitssicherheit|sicherheitstechnik|aviation security|\b(?:securitas|k[öo]tter|"
                    r"pond security|icts|wisag|dussmann|piepenbrock|fraport)\b|\bnetze?\b|stromnetz|energienetz|"
                    r"finanzamt|finanzverwaltung|finanzministerium", re.I)

# Jobs found by the "Security" / "Rechenzentrum" searches that aren't IT jobs (the job filter's IT words are broad)
NOT_IT_JOB = re.compile(r"sicherheitsmitarbeiter|sicherheitskraft|sicherheitsfachkraft|schutz-? und sicherheit|"
                        r"arbeitssicherheit|bewachung|objektschutz|werkschutz|interventionskraft|security guard|"
                        r"luftsicherheit|empfangs|pf[öo]rtner|sigeko|\bhse\b|geb[äa]udetechnik|reinigung", re.I)

# From the name and the website's domain: decisive, checked in this order.
# Plain "security" is not enough: "X Security GmbH" is often a guard company (its jobs or the AI decide).
NAME_RULES = [
    ("insurance", r"versicher|assekuranz|insurance|\brisk solutions\b|allianz|\bergo\b|\baxa\b|generali|\bhuk\b|"
                  r"signal iduna|barmenia|gothaer|\br\s?\+\s?v\b|\bruv\b|debeka|zurich|swiss re|munich re|hannover re|"
                  r"talanx|\bhdi\b|provinzial|n[üu]rnberger|alte leipziger|continentale|\bdevk\b|\blvm\b|"
                  r"helvetia|\bwwk\b|\bvhv\b|krankenkasse|\bbkk\b|\baok\b|\bikk\b|barmer|\bdak\b|techniker krankenkasse"),
    ("finance", r"\bbank|banken|bankhaus|sparkasse|volksbank|raiffeisen|\bvr\b|finanz|financ|(?<!human )capital\b|invest|"
                r"asset management|kapitalverwaltung|wertpapier|b[öo]rse|boerse|\bkfw\b|helaba|\bdeka\b|commerzbank|"
                r"bundesbank|\bezb\b|\becb\b|payment|zahlungs|kredit|credit|fonds|\bfunds?\b|clearing|fintech|\bleasing\b|"
                r"\bpay\b|paypal|\bing[- ]diba\b|\bconsorsbank|comdirect|\bdkb\b|\bn26\b|union investment|\bunzer\b|nexi|"
                r"\bconcardis\b|\bevertec\b"),
    ("security", r"cyber|it[- ]?secur|it-sicherheit|informationssicherheit|sicherheit in der informationstechnik|"
                 r"pentest|infosec|forensi|\bsecunet\b|\bgenua\b|\busd\b|sophos|kaspersky|\beset\b|fortinet|"
                 r"palo alto|crowdstrike|trend micro|telekom security|\bnspyre\b|\batos\b(?= security)"),
    ("cloud", r"rechenzentr|data ?cent(?:er|re)|datacenter|cloud|hosting|\bhoster\b|colocation|netzwerk|network|"
              r"telekom|telecom|telekommunikation|\btelco\b|glasfaser|fib(?:er|re)\b|internet ?service|equinix|"
              r"interxion|digital realty|de-cix|\bntt\b|vodafone|\b1\s?&\s?1\b|ionos|hetzner|\baws\b|amazon web services|"
              r"\bstrato\b|maincubes|e-shelter|northc|\bcolt\b|cyrusone|vantage data|\bdevops\b|\blinux\b|\bkgrz\b"),
    ("software", r"software|\bsoft\b|systemhaus|\bedv\b|\bit\b|informatik|informationstechn|computer|programmier|"
                 r"\bsap\b|\bdigital\b|webdesign|internetagentur|\bapps?\b|\bdev\b|\bdata\b|daten(?:verarbeitung|technik)"),
]
NAME_RULES = [(cat, re.compile(p, re.I)) for cat, p in NAME_RULES]

# In job titles: what the company's business is (insurance / finance words) ...
INDUSTRY_TITLES = {
    "insurance": re.compile(r"versicherung|aktuar|actuar|insurance|underwrit|schadenregulier|schadensachbearbeit", re.I),
    "finance": re.compile(r"\bbank|banking|kredit|credit|zahlungsverkehr|payments?\b|wertpapier|trading|treasury|"
                          r"fonds|asset manag|anlageberat|portfolio manag|b[öo]rse|finanzdienstleist", re.I),
}
# ... and, at an IT company, which kind of IT it does
IT_TITLES = {
    "security": re.compile(r"security|sicherheit|pentest|penetration|\bsoc\b|siem|forensi|\bciso\b|\bisms\b|"
                           r"red team|threat|vulnerab|incident response|offensive", re.I),
    "cloud": re.compile(r"cloud|devops|linux|netzwerk|network|rechenzentr|data ?cent|hosting|systemadministr|sysadmin|"
                        r"infrastru|kubernetes|platform engineer|\bsre\b|site reliability|server|vmware|storage|"
                        r"telekommunikation|\bnoc\b|systemintegration|fachinformatiker.{0,5}systemint", re.I),
}
IT_WORDS = re.compile(r"\bit\b|informatik|software|entwickl|develop|programm|devops|cloud|netzwerk|network|"
                      r"system|security|sicherheit|data|daten|sap\b|admin|web|frontend|backend|consultant", re.I)


def _titles(c: dict, history: dict[str, list[str]]) -> list[str]:
    titles = json.loads(c.get("ad_titles") or "[]") + json.loads(c.get("it_titles") or "[]")
    return titles + history.get(c["name_key"], [])


def by_rules(c: dict, titles: list[str]) -> tuple[str, str] | None:
    """(category, how: name / jobs / osm) if the name, website or job titles make it clear; None = ask the AI."""
    sources = json.loads(c.get("sources") or "{}")
    site = re.sub(r"^https?://(www\.)?|/.*$", "", c.get("website") or c.get("website_hint") or "")
    name = f"{c['name']} {site.replace('-', ' ').replace('.', ' ')}"
    if NOT_IT.search(c["name"]) or c.get("size_hint") == "staffing agency":
        return "other", "name"
    if sources.get("bundesbank"):  # the Bundesbank's list of banks
        return "finance", "name"
    for cat, rule in NAME_RULES:
        if rule.search(name):
            return cat, "name"
    if titles:
        industry = Counter(cat for t in titles for cat, rule in INDUSTRY_TITLES.items() if rule.search(t))
        if industry:
            cat, n = industry.most_common(1)[0]
            if n >= max(2, len(titles) / 3):
                return cat, "jobs"
    # An IT company: an IT office on OpenStreetMap, or most of the jobs on its careers page are IT jobs.
    # Its IT job titles tell which kind (most are security jobs: Cybersecurity, ...), else IT services/Software.
    osm_it = sources.get("osm_it") or sources.get("osm_software") \
        or any(k.endswith((":osm:it", ":osm:software")) for k in sources)  # region:Darmstadt:osm:it
    if osm_it or (c.get("jobs_near") or 0) >= 3 and (c.get("it_jobs") or 0) >= 0.6 * c["jobs_near"]:
        it = [t for t in titles if IT_WORDS.search(t)]
        kinds = Counter(cat for t in it for cat, rule in IT_TITLES.items() if rule.search(t))
        cat, n = kinds.most_common(1)[0] if kinds else ("software", 0)
        if n and n >= 0.5 * len(it):
            return cat, "jobs"
        return "software", "jobs" if it else "osm"
    return None


def apply_rules() -> int:
    """Rules for every company that has no category from you or the AI yet (cheap: no AI, no web).
    Returns how many still need the AI."""
    history: dict[str, list[str]] = {}
    from .companies import clean_name  # companies imports this module
    for company, title in db.employer_titles():
        history.setdefault(company_key(clean_name(company)), []).append(title)
    unclear = 0
    for c in db.company_rows():
        if c["category_from"] in ("you", "ai"):
            continue
        found = by_rules(c, _titles(c, history))
        if found and found != (c["category"], c["category_from"]):
            db.update_company(c["id"], {"category": found[0], "category_from": found[1]})
        elif not found:
            unclear += 1
            if c["category"]:  # a rule placed it before, not any more (e.g. its name changed)
                db.update_company(c["id"], {"category": None, "category_from": None})
    return unclear


SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"id": {"type": "INTEGER"}, "category": {"type": "STRING", "enum": list(LABELS)}},
              "required": ["id", "category"]},
}
PROMPT = """Sort these employers (in or near Darmstadt / Frankfurt am Main, Germany) by their MAIN business:
- insurance: insurance companies, health insurance funds, and their IT subsidiaries
- finance: banks, savings banks, payment, stock exchange, asset management, fintech, and their IT subsidiaries
- cloud: data centers, hosting, cloud providers, telecom / internet providers, network integrators
- security: companies that SELL IT security / cybersecurity services or products (not guards or alarm systems)
- software: software makers, IT consultancies, IT service providers, system houses, web / app agencies
- other: everything else (industry, retail, car rental, health, public sector, research, staffing agencies, ...)
Decide by what the company sells or does, NOT by the jobs. The job titles came from keyword searches
(e.g. for security or cloud jobs), so many companies show such jobs: a car rental hiring a security
engineer is "other", an IT consultancy hiring security consultants is "software", a bank hiring
developers is "finance". Use the job titles only for companies you don't know.
If you don't know a company and its name and jobs don't tell, answer "other".
Answer for each company by its number.

{companies}
"""
BATCH = 80


def _budget_left() -> int:
    day, used = (db.get_setting("category_ai") or ":0").split(":")
    used = int(used) if day == clock.today().isoformat() else 0
    return max(0, CONFIG["companies"]["max_category_ai_per_day"] - used)


def _spend() -> None:
    day, used = (db.get_setting("category_ai") or ":0").split(":")
    used = int(used) if day == clock.today().isoformat() else 0
    db.set_setting("category_ai", f"{clock.today().isoformat()}:{used + 1}")


def ask_ai(progress: dict, first: set[int] | None = None) -> int:
    """The AI sorts the companies the rules couldn't, within today's AI budget.
    first: these companies before the others (an import, look-alikes)."""
    if not ai.enabled():
        return 0
    from .company_jobs import _pace
    # Also town companies with a website but no careers page: an IT one gets the Impressum check (region.py)
    todo = sorted((c for c in db.company_rows() if not c["category"] and c["status"] != "skipped"
                   and (c["career_url"] or c["town"] and c["website"])),
                  key=lambda c: (c["id"] not in (first or ()), not c["career_url"], not c["website"],
                                 -sum(json.loads(c["sources"] or "{}").values())))
    sorted_ = 0
    while todo and _budget_left():
        batch, todo = todo[:BATCH], todo[BATCH:]
        progress["step"] = f"The AI sorts companies into categories ({len(todo)} more after these)…"
        lines = "\n".join(
            f"{n}. {c['name']}" + (f" ({re.sub(r'^https?://(www.)?|/$', '', c['website'])})" if c["website"] else "")
            + (f": jobs: {'; '.join(json.loads(c['ad_titles'] or c['it_titles'] or '[]')[:3])}"
               if json.loads(c["ad_titles"] or c["it_titles"] or "[]") else "")
            for n, c in enumerate(batch, 1))
        _pace()
        try:
            answer = ai.ask_json(PROMPT.format(companies=lines), SCHEMA, temperature=0.1, purpose="categories")
        except (ai.RateLimited, ai.Overloaded) as e:
            log.info("Categories: AI stopped (%s)", type(e).__name__)
            break
        except ai.BrokenAnswer:
            continue  # these companies are asked again next time
        _spend()
        for a in answer if isinstance(answer, list) else []:
            n = a.get("id")
            if isinstance(n, int) and 1 <= n <= len(batch) and a.get("category") in LABELS:
                db.update_company(batch[n - 1]["id"], {"category": a["category"], "category_from": "ai"})
                sorted_ += 1
    log.info("Categories: the AI sorted %d companies, %d still to do", sorted_, len(todo))
    return sorted_


def run(progress: dict, first: set[int] | None = None) -> None:
    progress["step"] = "Sorting companies into categories…"
    unclear = apply_rules()
    if unclear:
        ask_ai(progress, first)
    left = sum(1 for c in db.company_rows() if not c["category"] and c["status"] != "skipped")
    if left:
        progress["notes"].append(f"{left} companies are not sorted into a category yet: "
                                 f"the AI does {CONFIG['companies']['max_category_ai_per_day'] * BATCH} a day")
