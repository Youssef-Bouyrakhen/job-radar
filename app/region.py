"""Region coverage: every business in a town that could hire an IT Werkstudent (now: Darmstadt).

1. Collect (no AI, every night):
   a) OpenStreetMap: named offices of any kind, works, research institutes, universities, town halls and
      hospitals inside the town's boundary (all its districts). Not collected: shops, restaurants, cafés,
      doctors, pharmacies, craftsmen (none of them are offices), and small offices like lawyers or tax advisors.
   b) Arbeitsagentur: every employer with an ad in the town's postcodes, any job type (staffing agencies
      and private job agencies left out by the API itself).
   c) OffeneRegister.de: the company register as open data. Its only download is from 2019 (260 MB), so it is
      read once and a small extract is kept in data/. IT-sounding companies (Software, IT, Digital, ...) are
      added; the others only mark known companies as being in the town.
   Duplicates merge with the companies already known (same name or same website).
2. Rules: shops, restaurants, care services, craftsmen and staffing agencies the sources still bring are
   Skipped right away (the reason is saved), the rest goes through the normal pipeline: website (OSM tag,
   then a guess from the name, the AI last) -> careers page -> jobs -> fit score. A few per night.
3. Every company ends with a status: Watched (careers page, read every week), Initiativ, No careers page
   or Skipped. Small IT companies without a careers page: the contact email from their Impressum, Initiativ.
"""
import bz2
import html as htmllib
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx

from . import arbeitsagentur, careers, db
from .config import CONFIG
from .discover import ANCHOR, GENERIC_SLUGS, _same, _words, company_key
from .fetcher import BOT_HEADERS, BOT_NAME, Page, fetch_api, fetch_polite, robots_allows

log = logging.getLogger("jobradar")

FINAL = ("watched", "initiativ", "no_career_page", "skipped")


def towns() -> dict:
    return CONFIG["region"]["towns"]


def source(town: str, kind: str) -> str:
    """'region:Darmstadt:aa', 'region:Darmstadt:osm:engineer', 'region:Darmstadt:register'."""
    return f"region:{town}:{kind}"


def town_of_source(key: str) -> str | None:
    return key.split(":")[1] if key.startswith("region:") else None


def in_town(plz: str | None, place: str | None, town: str) -> bool:
    t = towns()[town]
    if plz and str(plz).strip().isdigit():
        return int(plz) in t["postcodes"]
    # "Darmstadt-Eberstadt" yes, "Roßdorf bei Darmstadt" no
    return bool(place and re.match(rf"{re.escape(town)}\b", place.strip(), re.I))


# Units of one big employer that the sources list one by one ("Institut für Geodäsie" has the TU's website,
# but "Studierendensekretariat TU Darmstadt" and "Bürger- und Ordnungsamt" need the name)
PARENTS = {
    "Darmstadt": [
        (re.compile(r"\btu darmstadt\b|technische universit[äa]t darmstadt", re.I), "Technische Universität Darmstadt"),
        (re.compile(r"^(?:h_da\b|hochschule darmstadt\b)", re.I), "Hochschule Darmstadt"),
        (re.compile(r"wissenschaftsstadt darmstadt|\bstadt darmstadt\b|^magistrat der stadt darmstadt", re.I),
         "Wissenschaftsstadt Darmstadt"),
    ],
}


def _parent(name: str, town: str) -> str:
    return next((parent for rule, parent in PARENTS.get(town, []) if rule.search(name)), name)


# ---- 1. collecting ---------------------------------------------------------------------

# OpenStreetMap office types that don't hire IT students: small practices, clubs, parties, agents
SKIP_OSM = {"lawyer", "notary", "tax_advisor", "accountant", "estate_agent", "property_management", "travel_agent",
            "religion", "political_party", "coworking", "employment_agency", "financial_advisor", "insurance",
            "therapist", "physician", "healthcare", "dog_trainer", "tutoring", "association", "club", "foundation",
            "union", "charity", "architect", "landscape_architect", "surveyor", "guide", "moving_company",
            "private_investigator", "nachlassverwalter", "diplomatic", "forestry", "vacant", "no"}
# Student fraternities are often mapped as offices ("B! Frisia", "Landsmannschaft Cheruskia")
FRATERNITY = re.compile(r"^[A-Z]{1,3}! |landsmannschaft|burschenschaft|wingolf|\bcorps\b|turnerschaft|verbindung", re.I)
OSM_QUERY = """[out:json][timeout:180];
area["de:amtlicher_gemeindeschluessel"="{ags}"]["boundary"="administrative"]->.a;
(nwr["office"]["name"](area.a);
 nwr["man_made"="works"]["name"](area.a);
 nwr["industrial"]["name"](area.a);
 nwr["amenity"~"^(university|college|research_institute|townhall|hospital)$"]["name"](area.a);
 nwr["research"]["name"](area.a););
out center tags;"""


def _osm_kind(tags: dict) -> str:
    if tags.get("office"):
        return re.sub(r"[^a-z0-9_]+", "_", tags["office"].lower())[:30]
    if tags.get("man_made") == "works" or tags.get("industrial"):
        return "works"
    return tags.get("amenity") or "research"


def collect_osm(town: str, progress: dict) -> list[dict]:
    from .companies import clean_name, overpass  # companies imports this module
    elements = overpass(OSM_QUERY.format(ags=towns()[town]["ags"]), f"OpenStreetMap ({town})", progress)
    records = []
    for e in elements:
        tags = e.get("tags", {})
        kind, name = _osm_kind(tags), tags.get("name", "").strip()
        if kind in SKIP_OSM or FRATERNITY.search(name):
            continue
        center = e.get("center") or e
        website = tags.get("website") or tags.get("contact:website") or tags.get("url")
        records.append({"name": clean_name(_parent(name, town)), "source": source(town, f"osm:{kind}"),
                        "website": website, "hint_from": "osm" if website else None,
                        "city": tags.get("addr:city") or town, "lat": center.get("lat"), "lon": center.get("lon")})
    log.info("Region %s: %d places from OpenStreetMap (%d left out: lawyers, clubs, ...)",
             town, len(records), len(elements) - len(records))
    return records


AA_MAX_PAGES = 40  # 100 ads per page; Darmstadt has about 1,500


def collect_aa(town: str, progress: dict) -> list[dict]:
    """One record per ad of any kind (so a company with 12 ads counts 12 times)."""
    from .companies import _ad_record, clean_name
    records, seen = [], set()
    for page in range(1, AA_MAX_PAGES + 1):
        progress["step"] = f"Arbeitsagentur: all employers in {town} (page {page})"
        params = {"wo": town, "umkreis": 0, "size": 100, "page": page, "zeitarbeit": "false", "pav": "false"}
        offers = fetch_api(arbeitsagentur.API, params=params, headers=arbeitsagentur.HEADERS).get("ergebnisliste", [])
        for o in offers:
            ref, firm = o.get("referenznummer"), clean_name(_parent(o.get("firma") or "", town))
            address = ((o.get("stellenlokationen") or [{}])[0].get("adresse") or {})
            if not firm or ref in seen or not in_town(address.get("plz"), address.get("ort"), town):
                continue
            seen.add(ref)
            records.append(_ad_record(o, firm, source(town, "aa")))
        if len(offers) < 100:
            break
    log.info("Region %s: %d job ads from the Arbeitsagentur", town, len(records))
    return records


REGISTER_URL = "https://daten.offeneregister.de/de_companies_ocdata.jsonl.bz2"
REGISTER_TYPES = ("HRB", "HRA", "GnR")  # companies and cooperatives (not clubs "VR", not partnerships "PR")
# IT-sounding names: these are added as new companies
REGISTER_IT = re.compile(r"software|\bit\b|\bit[-&]|informatik|informationstechn|digital|\bdata|\bdaten(?!schutz)|cloud|"
                         r"cyber|\bweb|internet|online|computer|network|\bnetz(?:werk)?|\bapps?\b|\bai\b|\bki\b|"
                         r"analytics|\blabs?\b|interactive|\.io\b|systemhaus|\bedv\b|programm|technolog|systems\b|"
                         r"\bsecurity\b|\bdevops\b|\bsaas\b|\bgames?\b|\bmedia\b", re.I)
# Shells without staff: management companies, holdings, solar parks, real estate
REGISTER_SHELL = re.compile(r"verwaltung|holding|beteiligung|verm[öo]gen|immobilien|grundst|grundbesitz|real estate|"
                            r"property|\bpv\b|solar|photovoltaik|windpark|liquidation|i\.\s?l\.|vorrats|projektgesellschaft|"
                            r"objekt|treuhand|\bunternehmergesellschaft\b$", re.I)


def _register_file(town: str):
    return db.DB_PATH.parent / f"offeneregister-{town}.json"


def _download_register(town: str, progress: dict) -> list[dict]:
    """Stream the whole register once (about 5 million lines, 2-5 minutes) and keep this town's active
    companies. Nothing big is saved: only the extract (a few hundred KB)."""
    if not robots_allows(REGISTER_URL, BOT_NAME):
        raise httpx.TransportError("OffeneRegister's robots.txt asks automated visitors not to download it")
    found, lines, buffer = [], 0, b""
    decompressor = bz2.BZ2Decompressor()
    with httpx.stream("GET", REGISTER_URL, headers=BOT_HEADERS, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            while chunk:  # the file is several bz2 parts in a row
                buffer += decompressor.decompress(chunk)
                chunk = b""
                if decompressor.eof:
                    chunk, decompressor = decompressor.unused_data, bz2.BZ2Decompressor()
            *complete, buffer = buffer.split(b"\n")
            for line in complete:
                lines += 1
                if town.encode() not in line:
                    continue
                d = json.loads(line)
                attrs = d.get("all_attributes") or {}
                if d.get("current_status") != "currently registered" or attrs.get("registered_office") != town \
                        or attrs.get("_registerArt") not in REGISTER_TYPES:
                    continue
                address = d.get("registered_address") or ""
                plz = re.search(r"\b(\d{5})\b", address)
                if plz and not in_town(plz[1], None, town):
                    continue
                found.append({"name": d["name"], "address": address.rstrip(".")})
            progress["step"] = f"OffeneRegister: reading the company register once ({lines / 1_000_000:.1f} of about 5.3 million lines)…"
    _register_file(town).write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    log.info("Region %s: OffeneRegister read once (%d lines), %d active companies in %s", town, lines, len(found), town)
    return found


def collect_register(town: str, progress: dict, known: list[dict]) -> list[dict]:
    """IT-sounding register companies as new records; the others only if they are known already (from the
    other sources or earlier runs): then they get the town too."""
    from .companies import clean_name
    path = _register_file(town)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _download_register(town, progress)
    keys = {company_key(r["name"]) for r in known} | {c["name_key"] for c in db.company_rows()}
    by_prefix: dict[str, list[str]] = {}
    for k in keys:
        by_prefix.setdefault(k[:5], []).append(k)
    records = []
    for e in entries:
        name = clean_name(_parent(e["name"], town))
        key = company_key(name)
        if len(key) < 2 or REGISTER_SHELL.search(name):
            continue
        is_known = key in keys or any(_same(key, k) for k in by_prefix.get(key[:5], []))
        if REGISTER_IT.search(name) or is_known:
            records.append({"name": name, "source": source(town, "register"), "city": town})
    log.info("Region %s: %d of %d register companies used (IT names, or known already)", town, len(records), len(entries))
    return records


def collect(records: list[dict], progress: dict) -> tuple[list[dict], set[str]]:
    """All towns, all three sources. Returns (records, the source keys that were collected this time).
    A source that fails keeps its companies from earlier runs and is tried again the next night."""
    found, collected = [], set()
    old_keys = {k for c in db.company_rows() for k in json.loads(c["sources"] or "{}")}
    for town in towns():
        steps = [("OpenStreetMap", lambda: collect_osm(town, progress)),
                 ("the Arbeitsagentur", lambda: collect_aa(town, progress))]
        if CONFIG["region"].get("offeneregister"):
            steps.append(("OffeneRegister", lambda: collect_register(town, progress, records + found)))
        for label, step in steps:
            progress["step"] = f"Collecting {town} companies from {label}…"
            try:
                got = step()
            except Exception as e:
                log.warning("Region %s: %s could not be read (%s)", town, label, e)
                progress["warnings"].append(f"{town}: {label} could not be read this time ({type(e).__name__}). "
                                            f"Its companies from earlier runs are kept; it is tried again next run.")
                continue
            found += got
            collected |= {r["source"] for r in got}
            if label == "OpenStreetMap":  # an office type with no places any more: its old counts go
                collected |= {k for k in old_keys if k.startswith(source(town, "osm:"))}
            stats = json.loads(db.get_setting("region_stats") or "{}")
            stats.setdefault(town, {})[label] = len(got)
            db.set_setting("region_stats", json.dumps(stats))
    return found, collected


def assign_towns() -> int:
    """companies.town: found by a region source, or its city is the town. Returns how many changed."""
    changed = 0
    for c in db.company_rows():
        sources = json.loads(c["sources"] or "{}")
        town = next((t for t in map(town_of_source, sources) if t in towns()), None) \
            or next((t for t in towns() if in_town(None, c["city"], t)), None)
        if town != c["town"]:
            db.update_company(c["id"], {"town": town})
            changed += 1
    return changed


# ---- 2. rules: who is skipped right away -------------------------------------------------

NOT_TARGET = re.compile(
    r"b[äa]ckerei|konditorei|metzgerei|fleischerei|restaurant|ristorante|trattoria|pizzeria|gastst[äa]tte|gasthaus|"
    r"gasthof|\bcaf[eé]\b|bistro|imbiss|kebap|d[öo]ner|brauhaus|catering|\bhotel|hostel|friseur|coiffeur|barber|"
    r"\bhair|kosmetik|nagelstudio|beauty|tattoo|massage|apotheke|arztpraxis|\bpraxis\b|zahnarzt|zahn[äa]rzt|"
    r"kieferorthop|physiotherap|ergotherap|logop[äa]d|heilpraktik|tierarzt|tierklinik|pflegedienst|pflegeheim|"
    r"seniorenheim|altenheim|seniorenzentrum|ambulante pflege|\bkita\b|kindertagesst|kindergarten|kinderkrippe|"
    r"tagesmutter|fahrschule|reinigung|hausmeister|umz[üu]ge|umzugs|\btaxi|kurier|\bblumen|florist|kiosk|"
    r"tankstelle|autohaus|autowerkstatt|kfz|reifen|lackiererei|malerbetrieb|\bmaler\b|dachdecker|zimmerei|"
    r"schreinerei|tischlerei|sanit[äa]r|heizung|klempner|^elektro\b|elektroinstallation|fliesen|gartenbau|"
    r"garten- und landschaft|galabau|schl[üu]sseldienst|bestattung|optiker|h[öo]rger[äa]te|supermarkt|\brewe\b|"
    r"\bedeka\b|\baldi\b|\blidl\b|\bpenny\b|\bnetto\b|kaufland|\bdm\b|rossmann|fitness|yoga|tanzschule|\bgym\b|"
    r"privathaushalt|^familie\b|naturstein|steinmetz|getr[äa]nke", re.I)
# Ads for jobs that are never office / IT jobs
NOT_TARGET_JOB = re.compile(
    r"\bk[öo]ch|k[üu]che|kellner|servicekraft|servicemitarbeiter|gastro|barista|sp[üu]l|verk[äa]ufer|kassier|"
    r"filial|einzelhandel|pflege|betreuungskraft|alltagsbegleit|erzieher|sozialassistent|heilerzieh|friseur|"
    r"kosmetik|reinigung|raumpfleg|hauswirtschaft|hausmeister|fahrer|kurier|zusteller|auslieferung|\blager|"
    r"kommissionier|produktionshelfer|helfer\b|aushilfe|b[äa]cker|konditor|metzger|fleischer|anlagenmechaniker|"
    r"monteur|installateur|\bmaler|lackierer|dachdecker|tischler|schreiner|maurer|zimmerer|fliesenleger|\bkfz|"
    r"g[äa]rtner|zahnmedizin|\bzfa\b|\bmfa\b|medizinische[rn]? fachangestellte|arzthelfer|physiotherap|"
    r"ergotherap|apothek|\bpta\b|sicherheitsmitarbeiter|empfang|rezeption|hotelfach|elektroniker", re.I)
SMALL_AD_COUNT = 9  # a company with this many ads or fewer, all for such jobs, is skipped


def skip_reason(c: dict) -> str | None:
    """Why this company is skipped without checking its website, or None."""
    from .companies import IT_SOURCES, REGIONAL, STAFFING
    from .filters import get_filter
    name, sources = c["name"], json.loads(c["sources"] or "{}")
    if c["size_hint"] == "staffing agency" or STAFFING.search(name):
        return "a staffing agency"
    if (m := NOT_TARGET.search(name)) and not REGIONAL.search(name):
        return f"a shop, restaurant, care or craft business (\"{m[0]}\" in its name)"
    titles = json.loads(c["ad_titles"] or "[]")
    only_ads = all(k.endswith(":aa") or k == "your_jobs" for k in sources) and not any(sources.get(s) for s in IT_SOURCES)
    ads = sum(n for k, n in sources.items() if k.endswith(":aa"))
    if only_ads and titles and ads <= SMALL_AD_COUNT and all(NOT_TARGET_JOB.search(t) for t in titles) \
            and not any(get_filter().it.find(t) for t in titles):
        return f"only ads for non-office jobs, e.g. \"{titles[0][:60]}\""
    return None


SKIP_NOTE = "Skipped by rule: "


def triage() -> int:
    """The rules for town companies that weren't checked yet. A company skipped by an older rule that
    doesn't apply any more goes back in line. Never touches a status you chose. Returns how many skipped."""
    skipped = 0
    for c in db.company_rows():
        if not c["town"] or c["status_by"] == "you" or c["last_checked"]:
            continue
        reason = skip_reason(c)
        if reason and c["status"] != "skipped":
            db.update_company(c["id"], {"status": "skipped", "status_note": SKIP_NOTE + reason})
            skipped += 1
        elif not reason and c["status"] == "skipped" and (c["status_note"] or "").startswith(SKIP_NOTE):
            db.update_company(c["id"], {"status": "new", "status_note": None})
    if skipped:
        log.info("Region: %d companies skipped by the rules (shops, restaurants, craftsmen, staffing agencies)", skipped)
    return skipped


def to_check(exclude: set[int]) -> list[dict]:
    """Town companies whose website and careers page were never looked for, a few per night."""
    from .companies import _priority
    rows = [c for c in db.company_rows() if c["town"] and not c["last_checked"] and c["id"] not in exclude
            and c["status"] not in ("skipped", "initiativ")]
    return sorted(rows, key=_priority)[:CONFIG["region"]["max_checks_per_run"]]


# ---- 3. the website: a guess from the name before the AI ---------------------------------

def _mentions_town(page: Page, town: str) -> bool:
    text = careers._text_head(page.html, len(page.html))
    return bool(re.search(rf"\b{re.escape(town)}\b", text, re.I)) or \
        any(str(p) in text for p in towns()[town]["postcodes"])


def guess_website(c: dict) -> Page | None:
    """acme-software.de for "ACME Software GmbH". It only counts if the page belongs to the company AND
    names the town or one of its postcodes (most homepages do, in the footer): mueller.de is not every Müller."""
    words = _words(c["name"])
    key = "".join(words)
    if len(key) < 4 or key in GENERIC_SLUGS:
        return None
    for host in dict.fromkeys([key, "-".join(words)]):
        for tld in ("de", "com"):
            page, _ = careers.check_website(f"https://www.{host}.{tld}/", c["name"])
            if page and _mentions_town(page, c["town"]):
                return page
    return None


# ---- 4. the final status -----------------------------------------------------------------

IT_CATEGORIES = ("software", "cloud", "security")
IT_OSM = ("osm_it", "osm_software", "osm_telecommunication")


def it_type(c: dict) -> bool:
    sources = json.loads(c["sources"] or "{}")
    return c["category"] in IT_CATEGORIES or any(sources.get(s) for s in IT_OSM) \
        or any(k.endswith((":osm:it", ":osm:software", ":osm:telecommunication")) for k in sources)


IMPRESSUM = re.compile(r"impressum|imprint|legal[- ]?notice|anbieterkennzeichnung", re.I)
CONTACT = re.compile(r"kontakt|contact", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
AT = re.compile(r"\s*[\[({]\s*(?:at|ät|@)\s*[\])}]\s*|\s+(?:at|ät)\s+", re.I)
DOT = re.compile(r"\s*[\[({]\s*(?:dot|punkt)\s*[\])}]\s*", re.I)
CLOUDFLARE = re.compile(r"data-cfemail=[\"']([0-9a-f]+)[\"']|/cdn-cgi/l/email-protection#([0-9a-f]+)", re.I)
NOT_AN_EMAIL = re.compile(r"\.(?:png|jpe?g|gif|svg|webp)$|example\.|domain\.|sentry|wixpress|noreply|no-reply|"
                          r"@(?:2x|3x)\b", re.I)
GOOD_INBOX = re.compile(r"^(?:bewerbung|jobs?|karriere|career|hr|personal|recruiting)", re.I)
WEAK_INBOX = re.compile(r"^(?:datenschutz|privacy|dsb|abuse|webmaster|presse|press|postmaster)", re.I)


def _cloudflare(hexcode: str) -> str:
    """Cloudflare hides addresses: the first byte is the key, every other byte XOR the key."""
    data = bytes.fromhex(hexcode)
    return "".join(chr(b ^ data[0]) for b in data[1:])


def emails_in(html: str) -> list[str]:
    found = [_cloudflare(a or b) for a, b in CLOUDFLARE.findall(html)]
    text = htmllib.unescape(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html))
    found += re.findall(r"mailto:([^\"'?>\s]+)", text, re.I)
    plain = DOT.sub(".", AT.sub("@", re.sub(r"<[^>]+>", " ", text)))
    found += EMAIL.findall(plain)
    return list(dict.fromkeys(e.strip(".").lower() for e in found if EMAIL.fullmatch(e.strip(".")) and not NOT_AN_EMAIL.search(e)))


def best_email(emails: list[str], website: str) -> str | None:
    site = careers._site(website)
    return min(emails, key=lambda e: (e.split("@")[1] != site and not e.split("@")[1].endswith("." + site),
                                      0 if GOOD_INBOX.search(e) else 2 if WEAK_INBOX.search(e) else 1), default=None)


def find_contact(website: str) -> tuple[str | None, str | None]:
    """(email, the page it was on) from the Impressum (by law it has one), else the contact page or homepage."""
    home = fetch_polite(website)
    if not home.ok:
        return None, None
    links = [urljoin(home.url, href.strip()) for href, text in ANCHOR.findall(home.html)]
    labels = [href + " " + re.sub(r"<[^>]+>", " ", text) for href, text in ANCHOR.findall(home.html)]
    own = lambda u: u.startswith("http") and careers._site(u) == careers._site(home.url)
    pages = [u for u, l in zip(links, labels) if own(u) and IMPRESSUM.search(l)][:2] \
        or [urljoin(home.url, "/impressum")]
    pages += [u for u, l in zip(links, labels) if own(u) and CONTACT.search(l)][:1]
    for url in dict.fromkeys(pages):
        page = fetch_polite(url)
        if page.ok and (email := best_email(emails_in(page.html), home.url)):
            return email, page.url
    email = best_email(emails_in(home.html), home.url)
    return email, home.url if email else None


def _contact(c: dict) -> dict:
    try:
        email, page = find_contact(c["website"])
    except Exception as e:
        log.info("Region: Impressum of %s could not be read (%s)", c["name"], e)
        email, page = None, None
    note = (f"Small IT company without a careers page: write to {email} (from {'the Impressum' if page and IMPRESSUM.search(page) else 'its website'})"
            if email else "Small IT company without a careers page. No email found in its Impressum: use the website's contact form")
    return {"contact_email": email, "contact_url": page, "contact_checked_at": db.now(),
            "status": "initiativ", "initiativ_at": db.now(), "status_note": note}


def settle(progress: dict) -> dict:
    """Give every checked town company its final status. Returns {status: how many changed}."""
    from .company_jobs import size_of
    changed = {s: 0 for s in FINAL}
    contacts = []
    for c in db.company_rows():
        # Watched links, Initiativ and Skipped are final already; a status you chose is never changed
        if not c["town"] or c["status_by"] == "you" or c["status"] not in ("new", "no_career_page") or not c["last_checked"]:
            continue
        sources = json.loads(c["sources"] or "{}")
        if c["career_url"]:
            if c["status"] == "new" and c["jobs_read_at"]:  # read, no good job right now, not Initiativ
                note = "Careers page read every week; its good jobs come to you" + \
                       ("" if c["jobs_method"] else " (its job list couldn't be read: open the careers page yourself)")
                db.update_company(c["id"], {"status": "watched", "status_note": note})
                changed["watched"] += 1
        elif not c["website"] and all(k.endswith(":register") for k in sources):
            db.update_company(c["id"], {"status": "skipped", "status_note": "Only in the company register (2019) and no "
                                        "website found: probably closed, or a company without staff"})
            changed["skipped"] += 1
        elif c["website"] and not c["contact_checked_at"] and it_type(c) and size_of(c) not in ("big", "staffing agency"):
            contacts.append(c)
        elif c["status"] == "new":
            db.update_company(c["id"], {"status": "no_career_page"})
            changed["no_career_page"] += 1
    contacts = contacts[:CONFIG["region"]["max_contacts_per_run"]]
    if contacts:
        progress.update(step="Looking for contact emails in the Impressum…", done=0, total=len(contacts))

        def one(c: dict) -> None:
            db.update_company(c["id"], _contact(c))
            progress["done"] += 1

        with ThreadPoolExecutor(CONFIG["companies"]["workers"]) as pool:
            list(pool.map(one, contacts))
        changed["initiativ"] += len(contacts)
    if any(changed.values()):
        log.info("Region: final statuses: %s", ", ".join(f"{n} {s}" for s, n in changed.items() if n))
    return changed


# ---- the Coverage page -------------------------------------------------------------------

def coverage() -> dict:
    rows = db.company_rows()
    per_night = CONFIG["region"]["max_checks_per_run"]
    stats = json.loads(db.get_setting("region_stats") or "{}")
    result = []
    for town in towns():
        mine = [c for c in rows if c["town"] == town]
        count = {s: sum(c["status"] == s for c in mine) for s in FINAL}
        unchecked = sum(not c["last_checked"] and c["status"] not in FINAL for c in mine)
        final = sum(count.values())
        result.append({
            "town": town, "companies": len(mine), "careers": sum(bool(c["career_url"]) for c in mine),
            "feeds": sum(bool(c["feed_url"]) for c in mine), "emails": sum(bool(c["contact_email"]) for c in mine),
            "checked": final, "percent": math.floor(100 * final / len(mine)) if mine else 0,
            **count, "waiting": len(mine) - final, "unchecked": unchecked,
            "nights_left": math.ceil(unchecked / per_night) if unchecked else 0,
            "sources": stats.get(town, {}),
        })
    total = sum(t["companies"] for t in result)
    return {"towns": result, "covered_percent": math.floor(100 * sum(t["checked"] for t in result) / total) if total else 0,
            "per_night": per_night, "run_hour": CONFIG["companies"].get("daily_run_hour")}
