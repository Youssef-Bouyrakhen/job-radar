"""Region coverage: every business in a town that could hire an IT Werkstudent (Darmstadt, Weiterstadt,
Griesheim, Pfungstadt, Langen, Egelsbach, Dreieich, Neu-Isenburg, then after the airport Offenbach am Main,
Mörfelden-Walldorf, Kelsterbach, Rüsselsheim am Main, Mühltal, Roßdorf, Ober-Ramstadt, Seeheim-Jugenheim,
Erzhausen and Mainz: region.towns in config.yaml), and at Frankfurt Airport (see "The airport" below).
A town can have other spellings (names: "Offenbach") and places that only start like it (not_place: "Mainz-Kastel").

1. Collect (no AI, every night):
   a) OpenStreetMap: named offices of any kind, works, research institutes, universities, town halls and
      hospitals inside the town's boundary (all its districts). Not collected: shops, restaurants, cafés,
      doctors, pharmacies, craftsmen (none of them are offices), and small offices like lawyers or tax advisors.
   b) Arbeitsagentur: every employer with an ad in the town's postcodes, any job type (staffing agencies
      and private job agencies left out by the API itself).
   c) OffeneRegister.de: the company register as open data. Its only download is from 2019 (260 MB), so it is
      read once (for every town without an extract, in one pass) and a small extract per town is kept in data/. IT-sounding companies (Software, IT, Digital, ...) are
      added; the others only mark known companies as being in the town.
   Duplicates merge with the companies already known (same name or same website).
2. Rules: shops, restaurants, care services, craftsmen and staffing agencies the sources still bring are
   Skipped right away (the reason is saved), the rest goes through the normal pipeline: website (OSM tag,
   then a guess from the name, the AI last) -> careers page -> jobs -> fit score. max_checks_per_run a night
   for all towns together, in the order of region.towns (Darmstadt is finished before the next town starts).
3. Every company ends with a status: Watched (careers page, read every day at 19:00), Initiativ, No careers page
   or Skipped. Small IT companies without a careers page: the contact email from their Impressum, Initiativ.

The airport (region.towns "Frankfurt Airport", airport: true) is an area, not a town. It comes after the towns:
- OpenStreetMap: the Frankfurt district "Flughafen" (with Gateway Gardens) + the airport grounds (osm_areas),
  also the operators of hangars and other buildings ("Flugzeughalle 5" belongs to Lufthansa Technik AG).
- Arbeitsagentur: the ads in postcode 60549; the register: companies with an address in 60549 / 60546 / 60547.
- Fraport's public lists: the airlines A-Z list of frankfurt-airport.com (with each airline's website) and the
  companies of Gateway Gardens. Cargo City and The Squaire publish no list of their companies.
- Stronger rules: shops, duty-free, restaurants, parking, gates, check-in and service counters, car rentals,
  ground handling, cleaning and security staffing are Skipped (AIRPORT_SKIP).
- Duplicates: "Lufthansa Cargo" is not "Lufthansa" (no merging by a similar name); two names with the same
  website are one company (merge_same_website).
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


def is_airport(town: str | None) -> bool:
    return bool(town and towns().get(town, {}).get("airport"))


def exact_merge(key: str) -> bool:
    """Found at the airport: merged only with the same name or the same website, never a similar name
    ("Lufthansa Cargo" is not "Lufthansa")."""
    return is_airport(town_of_source(key))


def spellings(town: str) -> list[str]:
    """The town and its other spellings (region.towns: names): "Offenbach am Main", "Offenbach"."""
    return [town, *towns()[town].get("names", [])]


def in_town(plz: str | None, place: str | None, town: str) -> bool:
    t = towns()[town]
    if plz and str(plz).strip().isdigit():
        return int(plz) in t["postcodes"]
    if t.get("place"):  # the airport: "Frankfurt - Flughafen"
        return bool(place and re.search(t["place"], place, re.I))
    if place and t.get("not_place") and re.match(t["not_place"], place.strip(), re.I):
        return False  # "Mainz-Kastel" is Wiesbaden
    # "Darmstadt-Eberstadt" yes, "Roßdorf bei Darmstadt" no
    return bool(place and any(re.match(rf"{re.escape(n)}\b", place.strip(), re.I) for n in spellings(town)))


# Units of one big employer that the sources list one by one ("Institut für Geodäsie" has the TU's website,
# but "Studierendensekretariat TU Darmstadt" and "Bürger- und Ordnungsamt" need the name)
PARENTS = {
    "Darmstadt": [
        (re.compile(r"\btu darmstadt\b|technische universit[äa]t darmstadt", re.I), "Technische Universität Darmstadt"),
        (re.compile(r"^(?:h_da\b|hochschule darmstadt\b)", re.I), "Hochschule Darmstadt"),
        (re.compile(r"wissenschaftsstadt darmstadt|\bstadt darmstadt\b|^magistrat der stadt darmstadt", re.I),
         "Wissenschaftsstadt Darmstadt"),
    ],
    "Mainz": [
        (re.compile(r"universit[äa]tsmedizin mainz|universit[äa]tsklinik(?:um)? mainz", re.I), "Universitätsmedizin Mainz"),
        (re.compile(r"johannes[- ]gutenberg[- ]universit[äa]t|\bjgu\b|^universit[äa]t mainz\b", re.I),
         "Johannes Gutenberg-Universität Mainz"),
        (re.compile(r"^hochschule mainz\b", re.I), "Hochschule Mainz"),
    ],
}


def _hall(town: str) -> str:
    """The town's administration: "Stadt Langen", or "Gemeinde Egelsbach" (region.towns: hall)."""
    return towns()[town].get("hall") or f"Stadt {town}"


def _parent(name: str, town: str) -> str:
    # Every town: "Magistrat der Stadt Weiterstadt Personalservice", "Stadt Pfungstadt: Kultur ...",
    # "Gemeindeverwaltung Egelsbach" are its administration
    spelled = "|".join(re.escape(n) for n in spellings(town))
    if re.match(rf"(?:der )?(?:magistrat der |stadtverwaltung |gemeindeverwaltung |gemeindevorstand der )?"
                rf"(?:(?:kreis|landeshaupt)?stadt|gemeinde) (?:{spelled})\b|(?:stadt|gemeinde)verwaltung\b", name, re.I):
        name = _hall(town)
    return next((parent for rule, parent in PARENTS.get(town, []) if rule.search(name)), name)


# "Fraport AG Zentrale", "SSP Deutschland GmbH - Hauptsitz": the company itself
HEAD_OFFICE = re.compile(r"\s*[-–,]?\s*\b(?:zentrale|hauptsitz|hauptverwaltung|headquarters?|head ?office|hq)$", re.I)


def _clean(name: str, town: str) -> str:
    from .companies import clean_name  # companies imports this module
    name = clean_name(_parent(name, town))
    # Only at the airport: the towns' companies merge by a similar name anyway
    return (HEAD_OFFICE.sub("", name).strip() or name) if is_airport(town) else name


# ---- 1. collecting ---------------------------------------------------------------------

# OpenStreetMap office types that don't hire IT students: small practices, clubs, parties, agents
SKIP_OSM = {"lawyer", "notary", "tax_advisor", "accountant", "estate_agent", "property_management", "travel_agent",
            "religion", "political_party", "coworking", "employment_agency", "financial_advisor", "insurance",
            "therapist", "physician", "healthcare", "dog_trainer", "tutoring", "association", "club", "foundation",
            "union", "charity", "architect", "landscape_architect", "surveyor", "guide", "moving_company",
            "private_investigator", "nachlassverwalter", "diplomatic", "forestry", "vacant", "no"}
# Student fraternities are often mapped as offices ("B! Frisia", "Landsmannschaft Cheruskia")
FRATERNITY = re.compile(r"^[A-Z]{1,3}! |landsmannschaft|burschenschaft|wingolf|\bcorps\b|turnerschaft|verbindung", re.I)
OSM_AMENITIES = ("university", "college", "research_institute", "townhall", "hospital")
OSM_QUERY = """[out:json][timeout:180];
{area}->.a;
(nwr["office"]["name"](area.a);
 nwr["man_made"="works"]["name"](area.a);
 nwr["industrial"]["name"](area.a);
 nwr["amenity"~"^(""" + "|".join(OSM_AMENITIES) + """)$"]["name"](area.a);
 nwr["research"]["name"](area.a);{extra});
out center tags;"""
# The airport's hangars, warehouses and office buildings are named "Flugzeughalle 5" or "Gebäude 534":
# only their operator ("Lufthansa Technik AG") is a company
AIRPORT_OSM = """
 nwr["operator"]["building"](area.a);
 nwr["operator"]["aeroway"="hangar"](area.a);"""


def _osm_area(town: str) -> str:
    t = towns()[town]
    if t.get("osm_areas"):  # OpenStreetMap relations: area id = 3600000000 + relation id
        return "(" + "".join(f"area({3600000000 + r});" for r in t["osm_areas"]) + ")"
    return f'area["de:amtlicher_gemeindeschluessel"="{t["ags"]}"]["boundary"="administrative"]'


def _osm_kind(tags: dict) -> str:
    if tags.get("office"):
        return re.sub(r"[^a-z0-9_]+", "_", tags["office"].lower())[:30]
    if tags.get("man_made") == "works" or tags.get("industrial"):
        return "works"
    if tags.get("amenity") in OSM_AMENITIES or tags.get("research") or not tags.get("operator"):
        return tags.get("amenity") or "research"
    return "operator"  # a building at the airport (also a fire station or car park: its operator is the company)


def collect_osm(town: str, progress: dict) -> list[dict]:
    from .companies import overpass  # companies imports this module
    query = OSM_QUERY.format(area=_osm_area(town), extra=AIRPORT_OSM if is_airport(town) else "")
    elements = overpass(query, f"OpenStreetMap ({town})", progress)
    records = []
    for e in elements:
        tags = e.get("tags", {})
        kind = _osm_kind(tags)
        name = (tags.get("operator") if kind == "operator" else tags.get("name", "")).strip()
        if kind in SKIP_OSM or FRATERNITY.search(name):
            continue
        if kind == "townhall":  # every town's is just "Rathaus": they must not merge into one company
            name = _hall(town)
        center = e.get("center") or e
        # A building's website is the building's ("The Squaire"), not its operator's
        website = None if kind == "operator" else tags.get("website") or tags.get("contact:website") or tags.get("url")
        records.append({"name": _clean(name, town), "source": source(town, f"osm:{kind}"),
                        "website": website, "hint_from": "osm" if website else None,
                        "city": tags.get("addr:city") or towns()[town].get("city", town),
                        "lat": center.get("lat"), "lon": center.get("lon")})
    log.info("Region %s: %d places from OpenStreetMap (%d left out: lawyers, clubs, ...)",
             town, len(records), len(elements) - len(records))
    return records


AA_MAX_PAGES = 40  # 100 ads per page; Darmstadt has about 1,500


def collect_aa(town: str, progress: dict) -> list[dict]:
    """One record per ad of any kind (so a company with 12 ads counts 12 times)."""
    from .companies import _ad_record
    records, seen = [], set()
    for page in range(1, AA_MAX_PAGES + 1):
        progress["step"] = f"Arbeitsagentur: all employers in {town} (page {page})"
        params = {"wo": towns()[town].get("aa_where", town), "umkreis": 0, "size": 100, "page": page, "zeitarbeit": "false", "pav": "false"}
        offers = fetch_api(arbeitsagentur.API, params=params, headers=arbeitsagentur.HEADERS).get("ergebnisliste", [])
        for o in offers:
            ref, firm = o.get("referenznummer"), _clean(o.get("firma") or "", town)
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


def _register_town(office: str, plz: str | None, names: list[str]) -> str | None:
    """Which of these towns a register entry belongs to. The airport's companies are registered in Frankfurt
    am Main: only their postcode counts."""
    for town in names:
        if is_airport(town):
            if plz and int(plz) in towns()[town]["postcodes"]:
                return town
        elif office in spellings(town) and (not plz or in_town(plz, None, town)):  # Griesheim near Darmstadt, not another one
            return town
    return None


def _download_register(names: list[str], progress: dict) -> None:
    """Stream the whole register once (about 5 million lines, 2-5 minutes) and keep these towns' active
    companies. Nothing big is saved: only one extract per town (a few hundred KB)."""
    if not robots_allows(REGISTER_URL, BOT_NAME):
        raise httpx.TransportError("OffeneRegister's robots.txt asks automated visitors not to download it")
    found, lines, buffer = {t: [] for t in names}, 0, b""
    # The file may write umlauts as JSON escapes (a backslash, then u00fc for ü): both forms
    wanted = {form for t in names for w in (map(str, towns()[t]["postcodes"]) if is_airport(t) else spellings(t))
              for form in (w.encode(), json.dumps(w)[1:-1].encode())}
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
                if not any(w in line for w in wanted):
                    continue
                d = json.loads(line)
                attrs = d.get("all_attributes") or {}
                if d.get("current_status") != "currently registered" or attrs.get("_registerArt") not in REGISTER_TYPES:
                    continue
                office = re.sub(r"\s*\(.*\)$", "", attrs.get("registered_office") or "")  # "Langen (Hessen)"
                address = d.get("registered_address") or ""
                plz = re.search(r"\b(\d{5})\b", address)
                if not (town := _register_town(office, plz[1] if plz else None, names)):
                    continue
                found[town].append({"name": d["name"], "address": address.rstrip(".")})
            progress["step"] = f"OffeneRegister: reading the company register once ({lines / 1_000_000:.1f} of about 5.3 million lines)…"
    for town, entries in found.items():
        _register_file(town).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    log.info("OffeneRegister read once (%d lines): %s", lines, ", ".join(f"{len(e)} active companies in {t}"
                                                                         for t, e in found.items()))


_register_failed: Exception | None = None  # this run's download failed: the other towns don't try again


def _register_entries(town: str, progress: dict) -> list[dict]:
    """This town's register companies. The first town without an extract reads the big file for every
    town that has none yet, so it is read only once (and only tried once per run)."""
    global _register_failed
    if not _register_file(town).exists():
        if _register_failed:
            raise _register_failed
        try:
            _download_register([t for t in towns() if not _register_file(t).exists()], progress)
        except Exception as e:
            _register_failed = e
            raise
    return json.loads(_register_file(town).read_text(encoding="utf-8"))


def collect_register(town: str, progress: dict, known: list[dict]) -> list[dict]:
    """IT-sounding register companies as new records; the others only if they are known already (from the
    other sources or earlier runs): then they get the town too."""
    entries = _register_entries(town, progress)
    keys = {company_key(r["name"]) for r in known} | {c["name_key"] for c in db.company_rows()}
    by_prefix: dict[str, list[str]] = {}
    for k in keys:
        by_prefix.setdefault(k[:5], []).append(k)
    records = []
    for e in entries:
        name = _clean(e["name"], town)
        key = company_key(name)
        if len(key) < 2 or REGISTER_SHELL.search(name):
            continue
        is_known = key in keys or any(_same(key, k) for k in by_prefix.get(key[:5], []))
        if REGISTER_IT.search(name) or is_known:
            records.append({"name": name, "source": source(town, "register"), "city": towns()[town].get("city", town)})
    log.info("Region %s: %d of %d register companies used (IT names, or known already)", town, len(records), len(entries))
    return records


FRAPORT_AIRLINES = ("https://www.frankfurt-airport.com/de/fluege-und-airlines/airlines-a-z/_jcr_content/root/"
                    "content/airlinefilter.model.json")  # the data of frankfurt-airport.com's "Airlines A-Z" page
# Links on an airline's page that are never the airline's own website
NOT_AIRLINE_SITE = re.compile(r"frankfurt-airport|fraport|facebook|instagram|youtube|linkedin|twitter|x\.com|tiktok|"
                              r"google|apple\.com|getyourguide|booking\.com|survey|xing", re.I)
AIRLINE_WORDS = {"air", "airlines", "airline", "airways", "royal", "international", "express", "city"}
# The companies of Gateway Gardens (Fraport's business quarter at the airport), from its leaflet "Site overview,
# as of October 2024" (gateway-gardens.de). It is a PDF with a site map: the names are kept here, not read
# from the PDF every night. Its hotels, restaurants, shops and medical practices are left out.
GATEWAY_GARDENS = ("Gate Gourmet", "AGENDIS", "Air France", "Getinge", "HOYA Surgical Optics", "Mundipharma", "Siemens",
                   "Keyence", "Air Liquide", "Schaeffler", "Lufthansa", "emeis Deutschland", "Bechtle",
                   "Schiff-Martini & Cie.", "Aramark", "Goldbeck", "ZVEI", "msg systems", "Windstar Medical",
                   "House of Logistics & Mobility (HOLM)", "Die Autobahn GmbH", "KION Group", "Deutsche Bahn", "Eisai")


def _airline_sites_file():
    return db.DB_PATH.parent / "fraport-airlines.json"


def _airline_site(name: str, link: str) -> str | None:
    """The airline's own website, from its page on frankfurt-airport.com: the first link named like it."""
    page = fetch_polite(link)
    if not page.ok:
        return None
    words = [w for w in _words(name) if w not in AIRLINE_WORDS and len(w) >= 3] or [company_key(name)]
    for href, _ in ANCHOR.findall(page.html):
        host = urlparse(href.strip()).hostname or ""
        if href.startswith("http") and not NOT_AIRLINE_SITE.search(host) \
                and any(w in host.replace("-", "").replace(".", "") for w in words):
            return f"https://{host}/"
    return None


def collect_fraport(town: str, progress: dict) -> list[dict]:
    """Fraport's public lists: every airline at the airport (with its website: read once per airline and
    kept in data/fraport-airlines.json) and the companies of Gateway Gardens."""
    if not robots_allows(FRAPORT_AIRLINES, BOT_NAME):
        raise httpx.TransportError("frankfurt-airport.com's robots.txt asks automated visitors not to read the airline list")
    airlines = fetch_api(FRAPORT_AIRLINES).get("results") or []
    if not airlines:
        raise httpx.TransportError("the airline list of frankfurt-airport.com was empty")
    path = _airline_sites_file()
    sites = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    city = towns()[town].get("city", town)
    records = []
    for n, a in enumerate(airlines, 1):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        if name not in sites and a.get("link", "").startswith("https://www.frankfurt-airport.com/"):
            progress["step"] = f"Fraport: the website of {name} ({n} of {len(airlines)} airlines)"
            sites[name] = _airline_site(name, a["link"])
        records.append({"name": _clean(name, town), "source": source(town, "fraport"), "website": sites.get(name),
                        "hint_from": "fraport" if sites.get(name) else None, "city": city})
    path.write_text(json.dumps(sites, ensure_ascii=False, indent=1), encoding="utf-8")
    records += [{"name": _clean(name, town), "source": source(town, "gateway"), "city": city} for name in GATEWAY_GARDENS]
    log.info("Region %s: %d airlines from Fraport's list (%d with a website), %d Gateway Gardens companies", town,
             len(airlines), sum(bool(sites.get(a.get("name"))) for a in airlines), len(GATEWAY_GARDENS))
    return records


def collect(records: list[dict], progress: dict) -> tuple[list[dict], set[str]]:
    """All towns, all their sources. Returns (records, the source keys that were collected this time).
    A source that fails keeps its companies from earlier runs and is tried again the next night."""
    global _register_failed
    found, collected, _register_failed = [], set(), None
    old_keys = {k for c in db.company_rows() for k in json.loads(c["sources"] or "{}")}
    for town in towns():
        steps = [("OpenStreetMap", lambda: collect_osm(town, progress)),
                 ("the Arbeitsagentur", lambda: collect_aa(town, progress))]
        if is_airport(town):
            steps.append(("Fraport's lists", lambda: collect_fraport(town, progress)))
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
        # Found in several towns (a chain): the first in region.towns order
        town = next((t for t in towns() if any(town_of_source(k) == t for k in sources)), None) \
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
# Frankfurt Airport: businesses that are only there for the passengers or only do the work on the ground
AIRPORT_SKIP = (
    ("a shop or duty-free", re.compile(
        r"duty[- ]?free|travel retail|heinemann|airport retail|\bshops?\b|\bstores?\b|boutique|souvenir|"
        r"wechselstube|currency exchange|reisebank|global blue|tax[- ]?free|\bmarkt\b", re.I)),
    ("a restaurant, café, bar or caterer", re.compile(
        r"catering|gourmet|sky ?chefs|aramark|\bssp\b|mcdonald|burger king|starbucks|kuffler|gastronomie|"
        r"\bbars?\b|lounge|\bfood|\bthe move\b|casino", re.I)),
    ("a hotel", re.compile(
        r"hotel|radisson|hilton|marriott|sheraton|hyatt|holiday inn|steigenberger|\bmoxy\b|meininger|\bibis\b|"
        r"novotel|mercure|b.?mine|staycity|hampton|best western|park inn|ipartment|smartments", re.I)),
    ("parking", re.compile(r"\bpark(?:en|haus|platz)\b|parking|apcoa|valet|park ?(?:&|and|\+|n) ?fly", re.I)),
    ("a gate, check-in or service counter", re.compile(
        r"\bgates? [a-z]?\d|check-?in|schalter|\bcounter\b|(?<!technisches )service ?cent(?:er|re)\b|ticket", re.I)),
    ("a car rental or transfer counter", re.compile(
        r"autovermietung|mietwagen|car rental|rent[- ]?a[- ]?car|\bsixt\b|europcar|hertz|\bavis\b|enterprise rent|"
        r"\bbudget\b|buchbinder|airport transfer|limousin|chauffeur", re.I)),
    ("ground handling only", re.compile(
        r"ground ?(?:services|handling)|bodenverkehrsdienst|\bhandling\b|swissport|wisag|aviapartner|menzies|"
        r"\bdnata\b|flugzeugabfertigung", re.I)),
    ("a cleaning company", re.compile(r"cleaning|reinigung|kl[üu]h\b|piepenbrock|dussmann|facility services", re.I)),
    ("a security staffing company", re.compile(
        r"securitas|frasec|\bi-sec\b|\bicts\b|k[öo]tter|sicherheitsdienst|luftsicherheit|aviation security|"
        r"airport security|security services|sicherheitsgesellschaft|wachschutz|bewachung", re.I)),
    ("not a company that hires (travel agency, leisure, shell company or empty office)", re.compile(
        r"reiseb[üu]ro|flugsimulator|vorrats|^(?:empty|vacant|leer|n/?a)$", re.I)),
)
# Airport ads for jobs that are never office / IT jobs
AIRPORT_JOB = re.compile(
    r"flugbegleit|cabin crew|flight attendant|vorfeld|\bramp\b|bodenverkehrsdienst|gep[äa]ck|luftsicherheit|"
    r"passagier|fluggast|check-?in|passenger|abfertig|enteis|de-?icing|sicherheitskontroll|kontrollkraft|"
    r"\bsecurity\b|frachtumschlag|umschlag|flugzeugreinig|stapler|ladearbeit|lagerist", re.I)


def is_staffing(c: dict) -> bool:
    from .companies import STAFFING
    return c["size_hint"] == "staffing agency" or bool(STAFFING.search(c["name"]))


def skip_reason(c: dict) -> str | None:
    """Why this company is skipped without checking its website, or None."""
    from .companies import IT_SOURCES, REGIONAL
    from .filters import get_filter
    name, sources, airport = c["name"], json.loads(c["sources"] or "{}"), is_airport(c["town"])
    if any(c["name_key"].startswith(company_key(n)) for n in CONFIG["region"].get("never_skip") or []):
        return None  # you said: check it normally
    if is_staffing(c):
        return "a staffing agency"
    if airport:
        for label, rule in AIRPORT_SKIP:
            if m := rule.search(name):
                return f"{label} at the airport (\"{m[0]}\" in its name)"
    # "Flughafen" in a name keeps a town's company ("Flughafen Restaurant" is still a restaurant at the airport).
    # Hiring in IT on LinkedIn keeps it too ("REWE digital" is no supermarket for us)
    hires_it = any(k.startswith("linkedin:") for k in sources)
    if (m := NOT_TARGET.search(name)) and (airport or not REGIONAL.search(name)) and not hires_it:
        return f"a shop, restaurant, care or craft business (\"{m[0]}\" in its name)"
    titles = json.loads(c["ad_titles"] or "[]")
    only_ads = all(k.endswith(":aa") or k == "your_jobs" for k in sources) and not any(sources.get(s) for s in IT_SOURCES)
    ads = sum(n for k, n in sources.items() if k.endswith(":aa"))
    not_office = lambda t: NOT_TARGET_JOB.search(t) or airport and AIRPORT_JOB.search(t)
    if only_ads and titles and ads <= SMALL_AD_COUNT and all(not_office(t) for t in titles) \
            and not any(get_filter().it.find(t) for t in titles):
        return f"only ads for non-office jobs, e.g. \"{titles[0][:60]}\""
    return None


SKIP_NOTE = "Skipped by rule: "


def triage() -> int:
    """The rules for town companies that weren't checked yet. A company skipped by an older rule that
    doesn't apply any more goes back in line. Never touches a status you chose. At the airport also companies
    checked before (found elsewhere first) that are New or No careers page: the airport's stronger rules
    count for them too (Watched and Initiativ stay). Returns how many skipped."""
    skipped = 0
    for c in db.company_rows():
        if not c["town"] or c["status_by"] == "you":
            continue
        if c["last_checked"] and not (is_airport(c["town"]) and c["status"] in ("new", "no_career_page") and not c["link_id"]):
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


def _unchecked(c: dict) -> bool:
    return bool(c["town"]) and not c["last_checked"] and c["status"] not in ("skipped", "initiativ")


def to_check(exclude: set[int]) -> list[dict]:
    """Town companies whose website and careers page were never looked for, max_checks_per_run a night
    for all towns together: one town is finished before the next one (region.towns order) starts."""
    from .companies import _priority
    order = {t: n for n, t in enumerate(towns())}
    rows = [c for c in db.company_rows() if _unchecked(c) and c["id"] not in exclude]
    return sorted(rows, key=lambda c: (order.get(c["town"], len(order)), _priority(c)))[:CONFIG["region"]["max_checks_per_run"]]


# ---- 3. the website: a guess from the name before the AI ---------------------------------

def _mentions_town(page: Page, town: str) -> bool:
    text = careers._text_head(page.html, len(page.html))
    # The airport's companies name their city: "Frankfurt am Main", "Frankfurt Airport"
    places = [towns()[town].get("city", town).split()[0]] if is_airport(town) else spellings(town)
    return any(re.search(rf"\b{re.escape(p)}\b", text, re.I) for p in places) or \
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
            if not careers.valid_host(f"www.{host}.{tld}"):  # a very long name: no such domain can exist
                continue
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


IMPRESSUM = careers.IMPRESSUM
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


def skip_staffing() -> int:
    """Staffing agencies stay Skipped, also with a careers page: their jobs are for clients. Also undoes
    v9.9, which made some of them Watched. Never a status you chose or an auto-added watched link."""
    skipped = 0
    for c in db.company_rows():
        if c["status_by"] == "you" or c["link_id"] or not c["career_url"] or not is_staffing(c):
            continue
        if c["status"] == "new" or c["status"] == "watched" and (c["status_note"] or "").startswith("Careers page read"):
            db.update_company(c["id"], {"status": "skipped", "status_note": SKIP_NOTE + "a staffing agency"})
            skipped += 1
    if skipped:
        log.info("Region: %d staffing agencies with a careers page skipped", skipped)
    return skipped


SAME_SITE = "Same website as "


def merge_same_website() -> int:
    """Frankfurt Airport: two of its names with the same website (the same address, not only the same group
    domain) are one company ("SIEMENS AG" and "Siemens AG", "Condor" and "Condor Flugdienst GmbH").
    "Lufthansa Cargo" with its own website stays its own company. The one kept: a status you chose, a watched
    link, a careers page, the shortest name (the website's owner: "Condor", not "Condor Technik"), the most
    sources, the oldest. The others are Skipped with a note; a Watched or Initiativ one never is.
    Returns how many were merged."""
    groups: dict[str, list[dict]] = {}
    for c in db.company_rows():
        if c["website"] and is_airport(c["town"]):
            groups.setdefault(careers.host(c["website"]), []).append(c)
    merged = 0
    for site, group in groups.items():
        if len(group) < 2:
            continue
        live = [c for c in group if not (c["status_note"] or "").startswith(SAME_SITE)]
        keep = min(live or group, key=lambda c: (c["status_by"] != "you", not c["link_id"], not c["career_url"],
                                                 len(c["name_key"]), -sum(json.loads(c["sources"] or "{}").values()),
                                                 c["id"]))
        for c in group:
            if c is keep or c["status_by"] == "you" or c["link_id"] or c["status"] not in ("new", "no_career_page"):
                continue
            db.update_company(c["id"], {"status": "skipped",
                                        "status_note": f"{SAME_SITE}{keep['name']} ({site}): counted as one company"})
            merged += 1
    if merged:
        log.info("Region: %d airport companies have the same website as another one: merged", merged)
    return merged


def settle(progress: dict) -> dict:
    """Give every checked company with a careers page, and every checked town company, its final status.
    Returns {status: how many changed}."""
    from .company_jobs import size_of
    changed = {s: 0 for s in FINAL}
    changed["skipped"] += skip_staffing() + merge_same_website()
    contacts = []
    for c in db.company_rows():
        # Watched links, Initiativ and Skipped are final already; a status you chose is never changed
        if c["status_by"] == "you" or c["status"] not in ("new", "no_career_page") or not c["last_checked"]:
            continue
        sources = json.loads(c["sources"] or "{}")
        if c["career_url"]:  # any company, also outside the towns (the ranking made the Initiativ ones before)
            if c["status"] == "new" and c["jobs_read_at"]:  # read, no good job right now, not Initiativ
                note = "Careers page read every day at 19:00; its good jobs come to you" + \
                       ("" if c["jobs_method"] else " (its job list couldn't be read: open the careers page yourself)")
                db.update_company(c["id"], {"status": "watched", "status_note": note})
                changed["watched"] += 1
        elif not c["town"]:
            continue  # the rules below are for the town companies only
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
            try:
                db.update_company(c["id"], _contact(c))
            except Exception as e:  # one bad company never stops the others; tried again next run
                log.warning("Region: contact email of %s failed (%s)", c["name"], e)
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
    def row(town: str, mine: list[dict], queued_before: int) -> dict:
        count = {s: sum(c["status"] == s for c in mine) for s in FINAL}
        unchecked = sum(_unchecked(c) for c in mine)
        final = sum(count.values())
        return {
            "town": town, "companies": len(mine), "careers": sum(bool(c["career_url"]) for c in mine),
            "feeds": sum(bool(c["feed_url"]) for c in mine), "emails": sum(bool(c["contact_email"]) for c in mine),
            "checked": final, "percent": math.floor(100 * final / len(mine)) if mine else 0,
            **count, "waiting": len(mine) - final, "unchecked": unchecked,
            # the towns share the nightly checks, in order: a town waits for the ones before it
            "nights_left": math.ceil((queued_before + unchecked) / per_night) if unchecked else 0,
            "sources": stats.get(town, {}),
        }

    result, queued = [], 0
    for town in towns():
        result.append(row(town, [c for c in rows if c["town"] == town], queued))
        queued += result[-1]["unchecked"]
    total = row("Whole region", [c for c in rows if c["town"] in towns()], 0)
    total["sources"] = {}
    return {"towns": result, "total": total, "covered_percent": total["percent"],
            "per_night": per_night, "run_hour": CONFIG["companies"].get("daily_run_hour")}
