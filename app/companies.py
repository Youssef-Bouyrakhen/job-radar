"""Company Discovery Engine, Phase A: collect companies and find their careers pages.

1. Collect employer names from
   a) the Arbeitsagentur API: ALL IT jobs (any type, full-time too) around Darmstadt and Frankfurt,
      plus keyword searches per category (companies.category_searches, IT jobs only),
   b) OpenStreetMap (Overpass API): IT / telecommunication / company offices, with their website tag,
   c) your own database: jobs, email alerts, suggestions and watched links,
   d) region coverage: every business in the towns (Darmstadt, Weiterstadt, Griesheim, Pfungstadt, Langen,
      Egelsbach, Dreieich, Neu-Isenburg) and at Frankfurt Airport that could hire an IT student (region.py).
2. Merge spelling variants ("ACME GmbH" = "Acme") and remember where each was found, how often.
3. For the next companies in line (IT employers first): find the website and careers page
   (see careers.py). Each run checks at most companies.max_companies_per_run, so a big list
   is worked through over several runs.
"""
import csv
import io
import json
import logging
import math
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx

from . import ai, arbeitsagentur, careers, categories, company_jobs, db, region, websearch
from .clock import GERMANY
from .config import CONFIG
from .discover import NOT_A_COMPANY, _host_key, _is_known, _same, _watched_keys, company_key
from .fetcher import BOT_HEADERS, Page, fetch_api, fetch_polite
from .filters import get_filter

log = logging.getLogger("jobradar")

# The four IT job fields ("Berufsfelder") of the Arbeitsagentur
BA_FIELDS = ("Informatik", "Softwareentwicklung und Programmierung",
             "IT-Netzwerktechnik, -Administration, -Organisation",
             "IT-Systemanalyse, -Anwendungsberatung und -Vertrieb")
BA_MAX_PAGES = 30  # 100 jobs per page; the API stops at about 10,000 results anyway
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_PAUSES = (30, 90)  # seconds to wait before the 2nd and 3rd try
OSM_OFFICES = ("it", "software", "telecommunication", "company")
BA_KEYWORD_PAGES = 5  # keyword searches are narrower: at most 500 ads each
IT_SOURCES = ("arbeitsagentur", "your_jobs", "osm_it", "osm_software", "osm_telecommunication", "bundesbank",
              *(f"ba_{cat}" for cat in categories.LABELS))

# "FERCHAU GmbH Niederlassung Darmstadt" is FERCHAU
BRANCH = re.compile(r"\s*(?:[-–,|/]\s*)?\b(?:zweigniederlassung|niederlassung|standort|filiale|branch office|"
                    r"gesch[äa]ftsstelle|c/o)\b.*$", re.I)
STAFFING = re.compile(r"personal(?:dienst|service|vermittlung|leasing|beratung|management)|zeitarbeit|"
                      r"arbeitnehmer[üu]berlassung|staffing|recruit|headhunt|\b(?:hays|randstad|adecco|manpower|"
                      r"ferchau|gulp|brunel|orizon|amadeus fire|dis ag|tempton|piening|jobactive|bindan|"
                      r"expertum|solcom|etengo|freelancermap|westhouse|computer futures|darwin recruitment)\b", re.I)


DOMAIN_NAME = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*)\.(?:de|com|net|org|eu|io|info|biz|ag|group)/?", re.I)
SOURCE_RANK = {"osm": 0, "arbeitsagentur": 1, "aa": 1, "your_jobs": 2}  # whose spelling of a name is cleanest


def _source_kind(key: str) -> str:
    """'osm_it' -> 'osm', 'region:Darmstadt:osm:engineer' -> 'osm', 'region:Darmstadt:aa' -> 'aa'."""
    return key.split(":")[2] if key.startswith("region:") else key.split("_")[0]


# "adesso Frankfurt" is adesso: an office's city at the end of the name is not part of it
CITY_SUFFIX = re.compile(r"(?:\s+|\s*[-–,|/]\s*)(?:frankfurt(?: am main| a\.? ?m\.?)?|darmstadt|eschborn|offenbach"
                         r"(?: am main)?|r[üu]sselsheim|mainz|wiesbaden|hanau|langen|egelsbach|dreieich|neu-isenburg|"
                         r"bad homburg|oberursel|weiterstadt|griesheim|bad vilbel|kelsterbach|rhein-main)$", re.I)


# ... except where the city IS part of the name: "Sparkasse Darmstadt" is not "Sparkasse"
REGIONAL = re.compile(r"sparkasse|volksbank|raiffeisen|stadt|kreis|gemeinde|universit|hochschule|klinik|"
                      r"krankenhaus|flughafen|messe|verkehr|kammer|\bihk\b|polizei|\bamt\b|landes|bistum|"
                      r"caritas|diakonie|evangelisch|katholisch|jobcenter|agentur f|zoo\b|theater|museum", re.I)


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", BRANCH.sub("", name or "")).strip(" -–,")
    short = CITY_SUFFIX.sub("", name).strip(" -–,")
    return short if len(company_key(short)) >= 3 and not REGIONAL.search(short) else name


def _from_domain(record: dict) -> dict:
    """Jobs from company sites sometimes have a web address as employer ("sva.de"):
    use "sva" as the name and the address as the website to try."""
    if m := DOMAIN_NAME.fullmatch(record["name"].strip()):
        return {**record, "name": m[1], "website": record.get("website") or record["name"].strip(),
                "hint_from": record.get("hint_from") or "name", "domain_name": True}
    return record


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = [(math.radians(x), math.radians(y)) for x, y in (a, b)]
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def distance_km(lat: float | None, lon: float | None) -> float | None:
    if lat is None or lon is None:
        return None
    return round(min(_km((lat, lon), tuple(p)) for p in CONFIG["companies"]["places"].values()), 1)


# ---- 1. collecting ---------------------------------------------------------------

def collect_arbeitsagentur() -> list[dict]:
    """One record per IT job ad (so a company with 12 ads counts 12 times)."""
    cfg, seen, records = CONFIG["companies"], set(), []
    for place in cfg["places"]:
        for field in BA_FIELDS:
            for page in range(1, BA_MAX_PAGES + 1):
                _progress["step"] = f"Arbeitsagentur: {field}, {place} (page {page})"
                params = {"berufsfeld": field, "wo": place, "umkreis": cfg["radius_km"], "size": 100, "page": page}
                offers = fetch_api(arbeitsagentur.API, params=params, headers=arbeitsagentur.HEADERS).get("ergebnisliste", [])
                for o in offers:
                    ref, firm = o.get("referenznummer"), clean_name(o.get("firma") or "")
                    if not firm or ref in seen:
                        continue
                    seen.add(ref)
                    records.append(_ad_record(o, firm, "arbeitsagentur"))
                if len(offers) < 100:
                    break
    log.info("Companies: %d IT job ads from the Arbeitsagentur", len(records))
    return records


def _ad_record(o: dict, firm: str, source: str) -> dict:
    loc = (o.get("stellenlokationen") or [{}])[0]
    title = " · ".join(filter(None, [o.get("stellenangebotsTitel"), o.get("hauptberuf")]))
    return {"name": firm, "source": source, "city": (loc.get("adresse") or {}).get("ort"),
            "lat": loc.get("breite"), "lon": loc.get("laenge"), "title": title[:150]}


def collect_category_searches() -> list[dict]:
    """Keyword searches per category ("Versicherung IT", "Pentest", ...): one record per IT job ad,
    source "ba_<category>". Ads that aren't IT jobs (a "SOC" search also finds security guards) are dropped."""
    cfg, it, records = CONFIG["companies"], get_filter().it, []
    for cat, words in cfg["category_searches"].items():
        seen = set()
        for word in words:
            for place in cfg["places"]:
                for page in range(1, BA_KEYWORD_PAGES + 1):
                    _progress["step"] = f"Arbeitsagentur: \"{word}\", {place}"
                    params = {"was": word, "wo": place, "umkreis": cfg["radius_km"], "size": 100, "page": page}
                    offers = fetch_api(arbeitsagentur.API, params=params, headers=arbeitsagentur.HEADERS).get("ergebnisliste", [])
                    for o in offers:
                        ref, firm = o.get("referenznummer"), clean_name(o.get("firma") or "")
                        job = f"{o.get('stellenangebotsTitel') or ''} | {o.get('hauptberuf') or ''}"
                        if firm and ref not in seen and it.find(job) and not categories.NOT_IT_JOB.search(job) \
                                and not NOT_A_COMPANY.search(firm):
                            seen.add(ref)
                            records.append(_ad_record(o, firm, f"ba_{cat}"))
                    if len(offers) < 100:
                        break
    log.info("Companies: %d IT job ads from the category searches", len(records))
    return records


BUNDESBANK_PAGE = "https://www.bundesbank.de/de/aufgaben/unbarer-zahlungsverkehr/serviceangebot/bankleitzahlen/download-bankleitzahlen-602592"
# "Aareal Bank GF BDS New Markets", "Aareal Bank ehem. Filiale Hamburg", "Commerzbank Clearing": all Aareal Bank / Commerzbank
BANK_EXTRA = re.compile(r"\s*\(.*$|\s+(?:GF|Gf\d*|GAA|ehem\.|Clearing|formerly|vormals|Ndl|ZNdl\.?|Zw|Bs|CC|INT|Service|Agentur|-\s|-alt)"
                        r"(?:\s.*)?$|,?\s+(?:Frankfurt|Germany) Branch$", re.I)


def collect_bundesbank() -> list[dict]:
    """Banks with their head office in companies.bank_postcodes (60000–65999), from the Bundesbank's official
    bank code file (Bankleitzahlendatei, free download, updated 4 times a year). "Merkmal 1" = the bank itself,
    not one of its branches."""
    low, high = CONFIG["companies"]["bank_postcodes"]
    page = fetch_polite(BUNDESBANK_PAGE)
    if not page.ok:
        raise httpx.TransportError(f"Bundesbank page: {page.reason}")
    link = re.search(r'href="([^"]*blz-aktuell-csv-data\.csv)"', page.html)
    if not link:
        raise httpx.TransportError("Bundesbank page: no link to the bank code file")
    r = httpx.get("https://www.bundesbank.de" + link[1], headers=BOT_HEADERS, timeout=60)
    r.raise_for_status()
    records = []
    for row in csv.DictReader(io.StringIO(r.content.decode("latin-1")), delimiter=";"):
        if row.get("Merkmal") != "1" or not (row.get("PLZ") or "").isdigit() or not low <= int(row["PLZ"]) <= high:
            continue
        if re.search(r"-alt-|\beh$", row["Bezeichnung"]):  # merged into another bank ("alt" = old)
            continue
        name = clean_name(BANK_EXTRA.sub("", row["Bezeichnung"]).strip())
        if len(company_key(name)) >= 2:
            records.append({"name": name, "source": "bundesbank", "city": row["Ort"]})
    log.info("Companies: %d bank head offices from the Bundesbank", len(records))
    return records


def overpass(query: str, label: str, progress: dict) -> list[dict]:
    """The elements an Overpass query finds. Overpass is a free service and often busy (429 / 504):
    try 3 times with a pause."""
    for attempt, pause in enumerate(OVERPASS_PAUSES + (None,)):
        progress["step"] = f"Collecting companies from {label}…" + (f" (try {attempt + 1})" if attempt else "")
        try:
            r = httpx.post(OVERPASS, data={"data": query}, headers={"User-Agent": BOT_HEADERS["User-Agent"]}, timeout=200)
            r.raise_for_status()
            # A busy server can also answer 200 with an error note and no offices: that's not "no offices"
            if not r.json().get("elements") and "error" in str(r.json().get("remark", "")).lower():
                raise httpx.TransportError(f"Overpass: {r.json()['remark'][:120]}")
            return r.json().get("elements", [])
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            if pause is None:
                raise
            log.info("Companies: %s busy (%s), trying again in %d s", label, e, pause)
            time.sleep(pause)


def collect_openstreetmap() -> list[dict]:
    cfg = CONFIG["companies"]
    radius = int(cfg["radius_km"] * 1000)
    around = "".join(f'nwr["office"~"^({"|".join(OSM_OFFICES)})$"]["name"](around:{radius},{lat},{lon});'
                     for lat, lon in cfg["places"].values())
    query = f"[out:json][timeout:120];({around});out center tags;"
    records = []
    for e in overpass(query, "OpenStreetMap", _progress):
        tags = e.get("tags", {})
        center = e.get("center") or e
        website = tags.get("website") or tags.get("contact:website") or tags.get("url")
        records.append({"name": clean_name(tags.get("name", "")), "source": f"osm_{tags.get('office')}",
                        "website": website, "hint_from": "osm" if website else None,
                        "city": tags.get("addr:city"), "lat": center.get("lat"), "lon": center.get("lon")})
    log.info("Companies: %d offices from OpenStreetMap", len(records))
    return records


def collect_your_data() -> list[dict]:
    names = [clean_name(n) for n in db.all_employer_names()]
    return [{"name": n, "source": "your_jobs"} for n in names if len(n) >= 2 and not NOT_A_COMPANY.search(n)]


# ---- 2. merging ------------------------------------------------------------------

def merge(records: list[dict], collected: set[str]) -> list[dict]:
    """Group the records by company. Known companies keep their id; counts of the sources
    collected this time are replaced, the others are kept."""
    groups: dict[str, dict] = {}
    by_prefix: dict[str, list[str]] = {}
    by_site: dict[str, str] = {}

    def add_group(key: str, group: dict) -> None:
        groups[key] = group
        by_prefix.setdefault(key[:5], []).append(key)

    for row in db.company_rows():
        old = json.loads(row["sources"] or "{}")
        add_group(row["name_key"], {**row, "names": {row["name"]: ((True, 9), 0)}, "places": [], "cities": Counter(),
                                    "titles": Counter(),
                                    "counts": Counter({s: n for s, n in old.items() if s not in collected})})
        if row["website"]:
            by_site[careers._site(row["website"])] = row["name_key"]

    def find(key: str, website: str | None, exact: bool = False) -> str | None:
        if key in groups:
            return key
        site = careers._site(careers.normalize_url(website) or "") if website else ""
        if site and site in by_site:
            return by_site[site]
        if exact:  # the airport: "Lufthansa Cargo" is not "Lufthansa" (region.exact_merge)
            return None
        return next((k for k in by_prefix.get(key[:5], []) if _same(k, key)), None)

    for r in map(_from_domain, records):
        key = company_key(r["name"])
        if len(key) < 2:
            continue
        found = find(key, r.get("website"), region.exact_merge(r["source"]))
        if not found:
            add_group(key, {"id": None, "name_key": key, "names": {}, "places": [], "cities": Counter(), "titles": Counter(),
                            "counts": Counter(), "website": None, "website_from": None})
            found = key
        g = groups[found]
        # Spelling preference: a real name over a web address, OSM over Arbeitsagentur over your jobs
        rank = (bool(r.get("domain_name")), SOURCE_RANK.get(_source_kind(r["source"]), 2))
        best, count = g["names"].get(r["name"], (rank, 0))
        g["names"][r["name"]] = (min(best, rank), count + 1)
        g["counts"][r["source"]] += 1
        if r.get("city"):
            g["cities"][r["city"]] += 1
        if r.get("title"):
            g["titles"][r["title"]] += 1
        if r.get("lat") is not None and r.get("lon") is not None:
            g["places"].append((r["lat"], r["lon"]))
        if r.get("website") and (not g.get("hint_new") or r.get("hint_from") == "osm" and g["hint_from_new"] != "osm"):
            g["hint_new"], g["hint_from_new"] = r["website"], r.get("hint_from")
            by_site.setdefault(careers._site(careers.normalize_url(r["website"]) or ""), found)

    rows = []
    for key, g in groups.items():
        if not g["counts"]:
            continue
        # Preferred source first, then the most common spelling, then the shorter one
        name = min(g["names"].items(), key=lambda kv: (kv[1][0], -kv[1][1], len(kv[0])))[0]
        dists = [d for d in (distance_km(*p) for p in g["places"]) if d is not None]
        ads = g["counts"].get("arbeitsagentur", 0)
        size = "staffing agency" if STAFFING.search(name) else "big" if ads >= 15 else "mid" if ads >= 4 else None
        rows.append({
            "id": g["id"], "name": name, "name_key": key,
            "city": g["cities"].most_common(1)[0][0] if g["cities"] else g.get("city"),
            "distance_km": min(dists) if dists else g.get("distance_km"),
            "lat": g["places"][0][0] if g["places"] else g.get("lat"),
            "lon": g["places"][0][1] if g["places"] else g.get("lon"),
            "sources": json.dumps(dict(g["counts"]), sort_keys=True),
            "size_hint": size, "website_hint": g.get("hint_new"), "hint_from": g.get("hint_from_new"),
            # None keeps the titles from earlier runs
            "ad_titles": json.dumps([t for t, _ in g["titles"].most_common(8)], ensure_ascii=False) if g["titles"] else None,
        })
    return rows


# ---- 3. checking websites and careers pages -------------------------------------------

WEBSITE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT", "properties": {"id": {"type": "INTEGER"}, "website": {"type": "STRING"}},
              "required": ["id", "website"]},
}
WEBSITE_PROMPT = """These are employers with an office in or near Darmstadt / Frankfurt am Main, Germany.
For each one (by its number) give its official main website (homepage), e.g. "https://www.example.de".
Only give a website you actually know belongs to this company. If you are not sure, give "".
Never build a domain from the name. Every website is loaded and checked, wrong ones are thrown away.

{companies}
"""


def _priority(c: dict) -> tuple:
    """Companies from your lists and look-alikes first, then IT employers (Arbeitsagentur IT ads,
    your jobs, IT offices), staffing agencies last."""
    sources = json.loads(c["sources"] or "{}")
    yours = any(s.startswith("list:") or s == "lookalike" for s in sources)
    return (c["size_hint"] == "staffing agency", not yours, not any(sources.get(s) for s in IT_SOURCES),
            -sum(sources.values()), c["distance_km"] if c["distance_km"] is not None else 999)


def companies_to_check(limit: int, ids: set[int] | None = None) -> list[dict]:
    """Companies whose website and careers page were never looked for (each is checked once)."""
    rows = [c for c in db.company_rows() if not c["last_checked"] and c["status"] not in ("skipped", "initiativ")
            and (ids is None or c["id"] in ids)]
    return sorted(rows, key=_priority)[:limit]


def ask_ai_for_websites(companies: list[dict]) -> dict[int, str]:
    """{company id: website the AI suggested}. Empty if the AI is off or its limits are used up."""
    if not ai.enabled() or not companies:
        return {}
    found, size = {}, CONFIG["companies"]["ai_batch_size"]
    for i in range(0, len(companies), size):
        batch = companies[i:i + size]
        _progress["step"] = f"Asking the AI for websites ({i + len(batch)} of {len(companies)})"
        lines = "\n".join(f"{n}. {c['name']}" + (f" ({c['city']})" if c["city"] else "") for n, c in enumerate(batch, 1))
        company_jobs._pace()  # the free AI tiers also count requests per minute
        try:
            answer = ai.ask_json(WEBSITE_PROMPT.format(companies=lines), WEBSITE_SCHEMA, temperature=0.1, priority="jobs")
        except (ai.RateLimited, ai.Overloaded) as e:
            log.warning("Companies: AI website lookup stopped (%s)", type(e).__name__)
            _progress["warnings"].append(ai.stop_note(e, "some companies were checked without a website guess"))
            break
        for c in batch:
            c["ai_asked"] = True  # the AI answered for it (maybe "don't know"): no need to wait for the next night
        for a in answer if isinstance(answer, list) else []:
            n = a.get("id")
            if isinstance(n, int) and 1 <= n <= len(batch) and a.get("website"):
                found[batch[n - 1]["id"]] = a["website"]
    return found


def _trim(page: Page) -> Page:
    page.html = page.html[:400_000]  # 150 homepages in memory must fit on a small server
    return page


def check_company(c: dict, ai_site: str | None, home: Page | None, watched: set[str]) -> dict:
    """Website + careers page for one company. Returns the fields to save."""
    notes, website_from = [], c["hint_from"] or "osm"
    if home is None and ai_site:
        home, note = careers.check_website(ai_site, c["name"])
        website_from = "ai"
        if note:
            notes.append(f"AI website: {note}")
    found = careers.find_career(home, c["name"])  # no website: only the job systems and the web search
    if home is None and not found.career_url:
        return {"career_state": "red", "status": "watched" if _is_known(c["name_key"], watched) else "no_career_page",
                "notes": " · ".join(notes) or "No website found", "website": None, "website_from": None,
                "career_url": None, "ats_type": None, "feed_url": None, "career_step": None,
                "career_how": "No website found" + (f"; {found.note}" if found.note else "")}
    known = _is_known(c["name_key"], watched) or bool(found.career_url and _is_known(_host_key(found.career_url), watched))
    status = "watched" if known else "new" if found.career_url else "no_career_page"
    website, website_from = (home.url, website_from) if home else (found.website, "search" if found.website else None)
    return {"website": website, "website_from": website_from, "career_url": found.career_url,
            "ats_type": found.ats_type, "feed_url": found.feed_url, "career_state": found.state,
            "status": status, "notes": " · ".join(filter(None, notes + [found.note])),
            "career_step": found.step, "career_how": found.note}


# ---- once: every "No careers page" company again, with the 6 steps ----------------------

FINDER_RULE = "1"  # bump when the careers-page finder gets better: "No careers page" companies are checked again
REJECTED = re.compile(r"(?:AI|OSM|Known) website: ([\w.-]+\.[a-z]{2,}) loaded, but its name doesn't match")


def finder_rule() -> str:
    """The web search counts too: when you add TINYFISH_API_KEY later, they are checked once more with it."""
    return FINDER_RULE + ("+search" if websearch.search_enabled() else "")


def recheck_due() -> bool:
    return db.get_setting("finder_rule") != finder_rule()


def recheck_no_career_pages(progress: dict) -> Counter:
    """Every "No careers page" company once more (not the ones you set yourself).
    Returns how many were found per step (1-6), "none" and "failed"."""
    started, used_before = time.monotonic(), Counter(websearch.usage) + Counter(careers.usage)
    todo = [c for c in db.company_rows() if c["status"] == "no_career_page" and c["status_by"] != "you"]
    progress.update(step="Looking again for the careers pages of \"No careers page\" companies…", done=0, total=len(todo))
    watched, stats = _watched_keys(), Counter()

    def one(c: dict) -> None:
        try:
            home, how, why = None, c["hint_from"] or c["website_from"] or "osm", ""
            if c["website"]:
                home, why = careers.check_website(c["website"], c["name"])  # c24bank.com turned out to be a parked domain
            if home is None and (m := REJECTED.search(c["notes"] or "")):  # rejected by the old, stricter name check
                home, why = careers.check_website(m[1], c["name"])
            if home:
                c["hint_from"] = c["website_from"] or how
            fields = check_company(c, None, home, watched)
            if why and not fields["career_url"]:  # e.g. "durr.com: The site answered with an error (HTTP 502)"
                fields["notes"] = fields["career_how"] = f"Website {why}; " + fields["career_how"].removeprefix("No website found; ")
            db.update_company(c["id"], fields)
            with _lock:
                stats[fields.get("career_step") or "none"] += 1
                progress["careers"] += bool(fields.get("career_url"))
                progress["feeds"] += bool(fields.get("feed_url"))
            if fields.get("career_url"):
                log.info("Companies: %s: careers page found again (step %s, %s)", c["name"], fields["career_step"], fields["career_how"])
        except Exception as e:  # one bad company never stops the others
            log.warning("Companies: looking again for the careers page of %s failed (%s)", c["name"], e)
            with _lock:
                stats["failed"] += 1
        with _lock:
            progress["done"] += 1
            done, found = progress["done"], sum(n for k, n in stats.items() if isinstance(k, int))
        if done % 20 == 0 or done == len(todo):  # so you can watch it in the log
            log.info("Companies: re-check %d of %d done, %d careers pages found so far (%.0f min)",
                     done, len(todo), found, (time.monotonic() - started) / 60)

    with ThreadPoolExecutor(CONFIG["companies"]["workers"]) as pool:
        list(pool.map(one, todo))
    db.set_setting("finder_rule", finder_rule())
    used = Counter(websearch.usage) + Counter(careers.usage) - used_before
    log.info("Companies: %d \"No careers page\" companies checked again in %.0f s: %s found (%s), %d still none, "
             "%d failed; used %d web searches, %d Jina pages, %d browser pages, 0 AI requests",
             len(todo), time.monotonic() - started, sum(n for k, n in stats.items() if isinstance(k, int)),
             ", ".join(f"step {k}: {stats[k]}" for k in sorted(k for k in stats if isinstance(k, int))) or "-",
             stats["none"], stats["failed"], used["searches"], used["jina"], used["browser"])
    progress["notes"].append(f"{sum(n for k, n in stats.items() if isinstance(k, int))} of {len(todo)} \"No careers "
                             f"page\" companies have a careers page now")
    return stats


# ---- re-checking careers pages found with older, looser rules ----------------------------

CAREER_RULE = "2"  # bump when the careers-page rule gets stricter: pages found before are checked again
FORGET = {"career_url": None, "ats_type": None, "feed_url": None, "career_state": None, "last_checked": None,
          "jobs_read_at": None, "jobs_method": None, "jobs_note": None, "jobs_total": None, "jobs_near": None,
          "it_jobs": None, "student_jobs": None, "good_jobs": None, "good_note": None, "fresh_jobs": None,
          "it_titles": None}


def recheck_career_pages() -> int:
    """Once per rule change: careers pages without an applicant system must pass the stricter rule
    (careers.career_page_problem). The ones that don't are looked for again in this run.
    Pages that don't load right now are left alone."""
    if db.get_setting("career_rule") == CAREER_RULE:
        return 0
    todo = [c for c in db.company_rows() if c["career_url"] and not c["feed_url"]]
    _progress.update(step="Re-checking careers pages with the stricter rule…", done=0, total=len(todo))

    def one(c: dict) -> bool:
        if careers.is_editorial(c["career_url"]):
            problem = "is a blog / magazine / news page"
        else:
            loaded = fetch_polite(c["career_url"])
            page = careers.follow_script_redirect(loaded)
            if not loaded.ok or not page.ok and "parked" not in page.reason:
                problem = None  # doesn't load right now: leave it alone
            else:
                problem = page.reason if not page.ok else careers.career_page_problem(page, c["website"])
                if not problem and page.url != loaded.url:  # the real page behind a script redirect
                    db.update_company(c["id"], {"career_url": page.url})
        with _lock:
            _progress["done"] += 1
        if not problem:
            return False
        log.info("Companies: %s: careers page %s %s, looking again", c["name"], c["career_url"], problem)
        # The homepage is known: no AI needed to find it again
        db.update_company(c["id"], {**FORGET, "status": "new", "website_hint": c["website"], "hint_from": c["website_from"] or "osm",
                                    "notes": f"Old careers page {c['career_url']} {problem}"[:300]})
        return True

    with ThreadPoolExecutor(CONFIG["companies"]["workers"]) as pool:
        wrong = sum(pool.map(one, todo))
    db.set_setting("career_rule", CAREER_RULE)
    log.info("Companies: re-checked %d careers pages, %d were not careers pages", len(todo), wrong)
    if wrong:
        _progress["notes"].append(f"{wrong} careers pages from earlier runs weren't real careers pages "
                                  f"(e.g. magazine articles): looked for again")
    return wrong


# ---- the run -----------------------------------------------------------------------

NIGHT_MARGIN_MINUTES = 15  # stop starting new companies this long before night_end_hour (the running ones finish)
COUNTERS = {"done": 0, "total": 0, "collected": 0, "new": 0, "websites": 0, "careers": 0, "feeds": 0,
            "jobs_read": 0, "new_jobs": 0, "auto_added": 0, "initiativ": 0}
# kind: "run" (the full discovery run), "import" (a list you gave it) or "lookalikes"
_progress = {"running": False, "kind": "run", "step": "", **COUNTERS, "warnings": [], "notes": [], "error": "",
             "finished_at": None}
_lock = threading.Lock()


def progress() -> dict:
    return {**_progress, "warnings": list(_progress["warnings"]), "notes": list(_progress["notes"])}


def start(kind: str = "run", text: str = "", label: str = "", night_limit: bool = True,
          stop_at: float | None = None) -> bool:
    """Start a run in the background. False if one is already running (only one at a time).
    kind "import": the companies in `text` (names or a link), marked with `label`;
    kind "lookalikes": companies similar to the ones whose jobs you saved or applied to;
    kind "recheck": look again for the careers pages of the "No careers page" companies;
    kind "read": read the jobs of every company never read (company_jobs.read_unread);
    kind "auto": the queue only, no collecting (started by auto_resume.py).
    night_limit False: no stop before the morning, even if started at night.
    stop_at: stop starting new companies at this time (time.time()) instead."""
    with _lock:
        if _progress["running"]:
            return False
        _progress.update(running=True, kind=kind, step="Starting…", **COUNTERS, warnings=[], notes=[], error="",
                         finished_at=None)
    threading.Thread(target=_run, args=(kind, text, label, night_limit, stop_at), daemon=True, name="companies").start()
    return True


def scheduled_run() -> None:
    """The 03:00 night run. A run still going (auto-resume, or one you started) is waited for, at most
    an hour, instead of skipping the night."""
    for _ in range(60):
        if start():
            return
        time.sleep(60)
    log.warning("Companies: the night run didn't start, another run was still going after an hour")


def night_deadline() -> float | None:
    """A run that starts at night stops starting new companies a bit before the morning
    (companies.night_end_hour, German time): what is left waits for the next night.
    A run started during the day has no deadline."""
    end = CONFIG["companies"].get("night_end_hour")
    now = datetime.now(GERMANY)
    if end is None or now.hour >= end:
        return None
    return (now.replace(hour=end, minute=0, second=0, microsecond=0) - timedelta(minutes=NIGHT_MARGIN_MINUTES)).timestamp()


def too_late(progress: dict) -> bool:
    return bool(progress.get("stop_at")) and time.time() >= progress["stop_at"]


def _run(kind: str = "run", text: str = "", label: str = "", night_limit: bool = True,
         stop_at: float | None = None) -> None:
    from . import company_import  # it imports this module
    started = time.monotonic()
    _progress["stop_at"] = stop_at or (night_deadline() if night_limit else None)
    try:
        ids = None  # None = all companies that are due
        if kind == "recheck":  # once after an update: the "No careers page" companies, nothing else
            recheck_no_career_pages(_progress)
            return
        if kind == "auto":  # auto-resume: what is waiting in the queue, the same steps as the night run
            _step("the region rules", region.triage)
            _step("checking the companies", lambda: _check(None))
            categories.run(_progress, None)
            company_jobs.run(_progress, None, ai_pages=company_jobs.ai_pages_left())  # also the ones the 19:00 read skipped
            _step("the final region statuses", lambda: region.settle(_progress))
            return
        if kind == "read":  # once, by hand: the jobs of every company never read, then the statuses
            company_jobs.read_unread(_progress)
            _step("the final statuses", lambda: region.settle(_progress))
            return
        recheck_career_pages()
        company_jobs.reread_after_area_change()
        if kind == "import":
            ids = company_import.import_list(text, label, _progress)
        elif kind == "lookalikes":
            ids = company_import.find_lookalikes(_progress)
        else:
            if company_import.lookalikes_due() and db.liked_employers() and ai.enabled():
                try:  # once a week: a few look-alikes join the queue (checked first)
                    company_import.find_lookalikes(_progress)
                except company_import.ImportProblem as e:
                    log.info("Look-alikes skipped: %s", e)
            _collect()
        _step("the region rules", region.triage)  # shops, restaurants, craftsmen in the towns are skipped before any web request
        if ids is None or ids:
            _step("checking the companies", lambda: _check(ids))
        categories.run(_progress, ids)  # rules for all; the AI for unclear ones, these first (before the fit score)
        if ids is None or ids:
            company_jobs.run(_progress, ids)  # Phase B: read their jobs, rank, auto-add, Initiativ list
        _step("the final region statuses", lambda: region.settle(_progress))  # incl. Impressum emails
    except company_import.ImportProblem as e:
        _progress["error"] = str(e)
    except Exception as e:
        log.exception("Company discovery failed")
        _progress["error"] = f"Something went wrong: {e}"
    finally:
        _progress.update(running=False, step="", finished_at=db.now())
        log.info("Companies: run finished in %.0f s", time.monotonic() - started)


def _step(label: str, run) -> None:
    """A step that goes wrong is logged and shown, and the run goes on with the next step."""
    try:
        run()
    except Exception as e:
        log.exception("Companies: %s failed", label)
        _progress["warnings"].append(f"Something went wrong in {label} ({type(e).__name__}: {e}); the rest of the run went on")


def _collect() -> None:
    records, collected = [], set()
    for label, collect in (("Arbeitsagentur", collect_arbeitsagentur),
                           ("the Arbeitsagentur category searches", collect_category_searches),
                           ("the Bundesbank bank list", collect_bundesbank),
                           ("OpenStreetMap", collect_openstreetmap), ("your database", collect_your_data)):
        _progress["step"] = f"Collecting companies from {label}…"
        try:
            found = collect()
        except Exception as e:  # one source down: keep its old counts, use the others
            log.warning("Companies: %s could not be read (%s)", label, e)
            _progress["warnings"].append(f"{label} could not be read this time ({type(e).__name__}). "
                                         f"Its companies from earlier runs are kept; it is tried again next run.")
            continue
        records += found
        collected |= {r["source"] for r in found}
        if label == "OpenStreetMap":
            collected |= {f"osm_{o}" for o in OSM_OFFICES}
        if collect is collect_category_searches:  # a category with no ads this time: its old counts go
            collected |= {f"ba_{cat}" for cat in CONFIG["companies"]["category_searches"]}
    # Region coverage: every business in the towns (region.towns) that could hire an IT student
    found, region_collected = region.collect(records, _progress)
    records += found
    collected |= region_collected
    _progress["step"] = "Merging duplicates…"
    rows = merge(records, collected)
    _progress["new"] = db.save_companies(rows)
    _progress["collected"] = len(rows)
    region.assign_towns()
    log.info("Companies: %d companies (%d new) from %d records", len(rows), _progress["new"], len(records))


class CheckFailed(Exception):
    pass


RETRY_NOTE = "Check failed, tried again next night: "


def _save_failure(c: dict, e: Exception) -> None:
    """The first failure leaves the company unchecked, so the next night tries it again.
    A second failure in a row is saved as checked (red): it doesn't take a place in the line every night."""
    error = str(e) if isinstance(e, CheckFailed) else f"{type(e).__name__}: {e}"
    if (c["notes"] or "").startswith(RETRY_NOTE):
        log.warning("Companies: %s failed again, not tried any more (%s)", c["name"], error)
        db.save_company_check(c["id"], {"notes": f"Check failed twice: {error}"[:300], "career_state": "red"})
    else:
        log.warning("Companies: %s failed, tried again next night (%s)", c["name"], error)
        db.update_company(c["id"], {"notes": (RETRY_NOTE + error)[:300]})


def _check(ids: set[int] | None = None) -> None:
    cfg = CONFIG["companies"]
    todo = companies_to_check(cfg["max_companies_per_run"], ids)
    if ids is None:  # plus a few companies of the towns (one town after the other) each night, whatever their business
        todo += region.to_check({c["id"] for c in todo})
    _progress.update(total=len(todo), done=0)
    if not todo:
        return
    watched = _watched_keys()
    homes: dict[int, Page | None] = {}

    def check_osm(c: dict) -> None:
        try:
            homes[c["id"]], note = careers.check_website(c["website_hint"], c["name"])
        except Exception as e:  # one bad company never stops the run: it is checked again next night
            c["failed"], homes[c["id"]] = f"its website check failed ({type(e).__name__}: {e})", None
            return
        if homes[c["id"]]:
            _trim(homes[c["id"]])
        else:
            c["hint_note"] = f"{'OSM' if c['hint_from'] == 'osm' else 'Known'} website: {note}"

    def guess(c: dict) -> None:
        if too_late(_progress) or c.get("failed"):
            return
        try:
            home = region.guess_website(c)
        except Exception as e:
            c["failed"] = f"the website guess failed ({type(e).__name__}: {e})"
            return
        if home:
            homes[c["id"]], c["hint_from"] = _trim(home), "guess"

    _progress["step"] = "Checking the websites from OpenStreetMap…"
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        list(pool.map(check_osm, [c for c in todo if c["website_hint"]]))
    # Town companies: a guess from the name first (rule), the AI only for the rest
    _progress["step"] = "Guessing websites from the company names…"
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        list(pool.map(guess, [c for c in todo if c["town"] and not homes.get(c["id"])]))
    # Look-alikes came with the AI's website already: don't ask it twice
    need_ai = [c for c in todo if not homes.get(c["id"]) and c["hint_from"] != "ai" and not c.get("failed")]
    ask = not too_late(_progress)
    ai_sites = ask_ai_for_websites(need_ai) if ask else {}
    # The free AI ran out before it could look for their website: they wait for the next night
    # (not saved, so they stay unchecked) instead of ending as "No website found"
    waiting = {c["id"] for c in need_ai if not c.get("ai_asked")} if ai.enabled() and ask else set()
    postponed = Counter()

    def finish(c: dict) -> None:
        if c["id"] in waiting or too_late(_progress):
            with _lock:
                postponed["ai" if c["id"] in waiting else "time"] += 1
                _progress["done"] += 1
            return
        try:
            if c.get("failed"):
                raise CheckFailed(c["failed"])
            fields = check_company(c, ai_sites.get(c["id"]), homes.pop(c["id"], None), watched)
            if c.get("hint_note"):
                fields["notes"] = " · ".join(filter(None, [c["hint_note"], fields["notes"]]))
            db.save_company_check(c["id"], fields)
        except websearch.LimitReached:  # not saved: it stays unchecked and the next night goes on with it
            fields = {}
            with _lock:
                postponed["search"] += 1
        except Exception as e:  # one bad company never stops the run
            fields = {}
            try:
                _save_failure(c, e)
            except Exception:
                log.exception("Companies: could not save the failure of %s", c["name"])
        with _lock:
            _progress["done"] += 1
            _progress["websites"] += bool(fields.get("website"))
            _progress["careers"] += bool(fields.get("career_url"))
            _progress["feeds"] += bool(fields.get("feed_url"))
            _progress["step"] = "Finding careers pages…"

    _progress["step"] = "Finding careers pages…"
    with ThreadPoolExecutor(cfg["workers"]) as pool:
        list(pool.map(finish, todo))
    log.info("Companies: checked %d: %d websites, %d careers pages, %d job feeds",
             len(todo) - sum(postponed.values()), _progress["websites"], _progress["careers"], _progress["feeds"])
    companies = lambda n: f"{n} compan{'y' if n == 1 else 'ies'}"
    if postponed["ai"]:
        _progress["notes"].append(f"{companies(postponed['ai'])} need the AI to find their website, but its free "
                                  f"requests are used up (scoring jobs comes first): they wait for the next night")
    if postponed["search"]:
        _progress["notes"].append(f"{companies(postponed['search'])} need the web search (step 5), but today's free "
                                  f"searches are used up: they wait for the next night")
    if postponed["time"]:
        _progress["notes"].append(f"{companies(postponed['time'])} wait for the next night: the run stops before "
                                  f"{CONFIG['companies']['night_end_hour']}:00")
    if postponed:
        log.info("Companies: %d wait for the AI, %d for the web search, %d for time: checked next night",
                 postponed["ai"], postponed["search"], postponed["time"])
