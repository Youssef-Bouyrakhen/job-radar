"""Built-in source: the official job search API of the Bundesagentur für Arbeit.

Docs: https://github.com/bundesAPI/jobsuche-api (the API key below is the public one
the Jobbörse website itself uses, not a secret).
"""
import base64

from .config import CONFIG
from .extractors import Job, clean, to_iso
from .fetcher import fetch_api
from .sources import CheckResult, found, red

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAILS_API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails"
DETAIL_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
NAME = "Bundesagentur für Arbeit (Jobsuche)"
LINK_URL = "https://www.arbeitsagentur.de/jobsuche/"  # how this source appears in the links list


def check() -> CheckResult:
    cfg = CONFIG["arbeitsagentur"]
    jobs: dict[str, Job] = {}
    try:
        for search in cfg["searches"]:
            for place in cfg["locations"]:
                params = {"was": search, "wo": place, "umkreis": cfg["radius_km"], "size": 100, "page": 1}
                for offer in fetch_api(API, params=params, headers=HEADERS).get("ergebnisliste", []):
                    ref = offer.get("referenznummer")
                    if ref and ref not in jobs:
                        jobs[ref] = _job(ref, offer)
    except Exception as e:
        return red(f"The Arbeitsagentur API could not be read ({type(e).__name__})", "Arbeitsagentur API")
    searches = len(cfg["searches"]) * len(cfg["locations"])
    return found(list(jobs.values()), "Arbeitsagentur API", NAME, f" in {searches} searches")


def fetch_description(job_url: str) -> str:
    """Full job description from the official details endpoint ("" if not available)."""
    ref = job_url.rstrip("/").rsplit("/", 1)[-1]
    encoded = base64.b64encode(ref.encode()).decode()
    data = fetch_api(f"{DETAILS_API}/{encoded}", headers=HEADERS)
    hours = "Vollzeit" if data.get("arbeitszeitVollzeit") else ""
    text = clean(data.get("stellenangebotsBeschreibung"), 8000)
    return f"[Arbeitsagentur label: {hours}]\n{text}" if hours and text else text


def _job(ref: str, offer: dict) -> Job:
    places = offer.get("stellenlokationen") or [{}]
    hours = []
    if offer.get("arbeitszeitVollzeit"):
        hours.append("Vollzeit")
    if any(v for k, v in offer.items() if k.startswith("arbeitszeitTeilzeit")):
        hours.append("Teilzeit")
    return Job(
        title=clean(offer.get("stellenangebotsTitel")),
        url=f"{DETAIL_URL}{ref}",
        company=clean(offer.get("firma")),
        location=(places[0].get("adresse") or {}).get("ort", ""),
        employment=", ".join(hours),
        posted=to_iso(offer.get("datumErsteVeroeffentlichung")
                      or (offer.get("veroeffentlichungszeitraum") or {}).get("von")),
    )
