"""Loads config.yaml, then your personal config.local.yaml on top (not in git),
with sensible defaults if a value is missing."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "check_interval_minutes": 60,
    "arbeitsagentur": {
        "enabled": True,
        "searches": ["Werkstudent Informatik"],
        "locations": ["Darmstadt", "Frankfurt am Main"],
        "radius_km": 25,
    },
    "emails": {"enabled": True, "label": "JobRadar", "days_back": 14, "max_emails_per_check": 50},
    "applications": {"enabled": True, "label": "Bewerbungen", "days_back": 60, "max_emails_per_check": 80,
                     "check_every_minutes": 15, "telegram_max_age_days": 3,
                     "ignore_senders": ["vaia.com", "studysmarter.de", "jobagent.stepstone.de", "shop-apotheke.com",
                                        "freelance.de", "jobseeker.com", "match.indeed.com", "updates-noreply@linkedin.com",
                                        "notifications-noreply@linkedin.com"]},
    "filter": {"enabled": True},
    "telegram": {"enabled": True, "max_messages_per_run": 15},
    "gemini": {
        "enabled": True,
        "model": "gemini-3.5-flash",
        "thinking_level": "low",
        "min_score": 7,
        "batch_size": 10,
        "seconds_between_requests": 6,
        "max_jobs_per_run": 40,
        "fetch_full_description": True,
        "profile": "Computer science student looking for a Werkstudent IT job, max 20 h/week.",
    },
    "groq": {"model": "openai/gpt-oss-120b", "max_prompt_chars": 18000,
             "daily_tokens": 200000},  # its free tokens per day (TPD); Groq's own number once it says it
    "openrouter": {"model": "poolside/laguna-s-2.1:free"},
    "cloudflare": {"model": "@cf/qwen/qwen3-30b-a3b-fp8", "prompt_suffix": " /no_think", "max_tokens": 3000,
                   "max_prompt_chars": 60000, "daily_neurons": 9000,
                   "neurons_per_m_input": 4625, "neurons_per_m_output": 30475,
                   # Job Radar's count x this = Cloudflare's (2026-10-24: Cloudflare stopped at 10,000 when Job Radar
                   # had counted 6,665); learned again each time Cloudflare says today's neurons are used up
                   "neurons_factor": 1.5},
    "github": {"enabled": False, "model": "openai/gpt-4.1-mini", "max_prompt_chars": 18000},
    "mistral": {"enabled": False, "model": "mistral-small-latest"},
    "cerebras": {"enabled": False, "model": "gpt-oss-120b"},
    "ai_budget": {
        "daily_requests": {"Gemini": 20, "Groq": 1000, "OpenRouter": 50, "GitHub": 150, "Mistral": 300, "Cerebras": 300},
        # % of each provider's day: scoring keeps score_reserved for itself, careers pages at most careers,
        # categories and everything else together at most other
        "shares": {"score_reserved": 30, "careers": 60, "other": 10},
    },
    "companies": {
        "radius_km": 25,
        "places": {"Darmstadt": [49.8728, 8.6512], "Frankfurt am Main": [50.1109, 8.6821]},
        "max_companies_per_run": 150,
        "workers": 6,
        "delay_per_site_seconds": 1,
        "ai_batch_size": 25,
        "max_job_reads_per_run": 120,
        "recheck_after_days": 7,
        "watched_read_hour": 19,
        "midday_read_hour": 13,  # the busy companies only, Monday to Friday (German time); None = off
        "midday_read_days": "mon-fri",
        "busy_days": 30,  # busy: a new job in this many days (read at 13:00 and 19:00)
        "quiet_days": 60,  # quiet: no new job in this many days (read at 19:00 every quiet_every_days)
        "quiet_every_days": 3,
        "full_collect_day": "sun",  # the 03:00 run collects all areas again only in the night to this day
        "max_ai_pages_per_run": 30,
        "max_ai_pages_watched": 400,  # 19:00 read and auto-resume: what the AIs have left, at most this
        "max_auto_added_per_run": 10,
        "max_initiativ_reasons_per_run": 60,
        "ai_seconds_between_requests": 12,
        "max_size_lookups_per_run": 200,
        "daily_run_hour": 3,
        "auto_resume": True,  # every 5 minutes: go on with the work that is left (auto_resume.py)
        "night_end_hour": 7,
        "lookalikes_every_days": 7,
        "max_lookalikes": 15,
        "import_max_ai_requests": 5,
        "weekly_summary": {"day": "sun", "hour": 19},
        "max_category_ai_per_day": 2,
        "bank_postcodes": [60000, 65999],
        "fit_bonus_categories": ["insurance", "finance", "cloud"],
        "fit_category_bonus": 10,
        "import_postcodes": [60000, 65999],
        "category_searches": {
            "insurance": ["Versicherung IT", "Versicherung Informatik", "Versicherung Daten"],
            "finance": ["Bank IT", "Bank Informatik", "Bank Daten", "Finanz IT", "Finanz Informatik", "Finanz Daten"],
            "cloud": ["Rechenzentrum", "Cloud", "Netzwerk", "Hosting", "DevOps", "Linux"],
            "security": ["IT-Sicherheit", "Security", "Pentest", "SOC"],
            "software": ["Softwareentwicklung", "IT-Dienstleister", "Anwendungsentwicklung"],
        },
    },
    # LinkedIn's public job search (no login): companies near Darmstadt that hire in IT (linkedin.py)
    "linkedin": {
        "enabled": True,
        "hour": 8,  # once a day, German time (not 03:00, 13:00 or 19:00)
        "location": "Darmstadt, Hessen, Deutschland",
        "distance_miles": 25,  # LinkedIn only has 10 / 25 / 50 miles; then each place: at most area.max_km
        "keywords_per_day": 2,  # 10 keywords, 2 a day: each one once a week
        "keywords": ["Werkstudent IT", "Informatik", "Software", "Entwickler Developer", "Data", "Cloud",
                     "IT-Support Administrator", "Security", "SAP", "Praktikum IT"],
        "max_pages": 5,  # 10 jobs per page
        "pause_seconds": [5, 10],  # random pause between two pages
    },
    "region": {
        # ags: the town's official municipality key (OpenStreetMap boundary); postcodes: all of its districts
        # in this order: the nightly checks finish a town before the next one starts
        "towns": {
            "Darmstadt": {"ags": "06411000", "postcodes": [64283, 64285, 64287, 64289, 64291, 64293, 64295, 64297]},
            "Weiterstadt": {"ags": "06432023", "postcodes": [64331]},
            "Griesheim": {"ags": "06432008", "postcodes": [64347]},
            "Pfungstadt": {"ags": "06432018", "postcodes": [64319]},
            "Langen": {"ags": "06438006", "postcodes": [63225]},
            "Egelsbach": {"ags": "06438003", "postcodes": [63329], "hall": "Gemeinde Egelsbach"},
            "Dreieich": {"ags": "06438002", "postcodes": [63303]},
            "Neu-Isenburg": {"ags": "06438009", "postcodes": [63263]},
            # Not a town: the airport area (Terminals, Cargo City Nord/Süd, Gateway Gardens, The Squaire).
            # osm_areas: the Frankfurt district "Flughafen" + the airport grounds; 60549 is its postcode,
            # 60546 / 60547 the big-customer postcodes of Lufthansa and Fraport
            "Frankfurt Airport": {"osm_areas": [3891373, 5813621], "postcodes": [60549, 60546, 60547],
                                  "aa_where": "60549", "place": r"flughafen|airport", "city": "Frankfurt am Main",
                                  "airport": True},
            # names: other spellings of the town in addresses and the company register ("Offenbach", "Rüsselsheim");
            # not_place: places that start with the town's name but are another town (Mainz-Kastel is Wiesbaden)
            "Offenbach am Main": {"ags": "06413000", "postcodes": [63065, 63067, 63069, 63071, 63073, 63075],
                                  "names": ["Offenbach"], "not_place": r"offenbach[- ](?:an der queich|hundheim)"},
            "Mörfelden-Walldorf": {"ags": "06433008", "postcodes": [64546], "names": ["Mörfelden"]},  # not "Walldorf": SAP's is in Baden
            "Kelsterbach": {"ags": "06433007", "postcodes": [65451]},
            "Rüsselsheim am Main": {"ags": "06433012", "postcodes": [65428], "names": ["Rüsselsheim"]},
            "Mühltal": {"ags": "06432014", "postcodes": [64367], "hall": "Gemeinde Mühltal"},
            "Roßdorf": {"ags": "06432020", "postcodes": [64380], "hall": "Gemeinde Roßdorf",
                        "aa_where": "64380"},  # the Arbeitsagentur finds nothing for "Roßdorf" (the ß)
            "Ober-Ramstadt": {"ags": "06432016", "postcodes": [64372]},
            "Seeheim-Jugenheim": {"ags": "06432022", "postcodes": [64342], "hall": "Gemeinde Seeheim-Jugenheim",
                                  "names": ["Seeheim"]},  # not "Jugenheim": there is one in Rheinhessen too
            "Erzhausen": {"ags": "06432006", "postcodes": [64390], "hall": "Gemeinde Erzhausen"},
            # 55099: the university's own postcode
            "Mainz": {"ags": "07315000", "postcodes": [55099, 55116, 55118, 55120, 55122, 55124, 55126, 55127, 55128,
                                                       55129, 55130, 55131],
                      "not_place": r"mainz[- ](?:kastel|kostheim|amöneburg)"},
        },
        "max_checks_per_run": 300,
        "max_contacts_per_run": 60,
        # Never Skipped by the rules, always checked normally (their only ads at the airport are sales /
        # technician jobs, but they are big office employers)
        "never_skip": ["Euler Hermes", "Alstom"],
        "offeneregister": True,
    },
    # Your area: jobs farther than max_km from center (and not remote / Bundesweit) are shown as
    # "Outside your area", never Must apply and never sent to Telegram
    "area": {
        "center": "Darmstadt",
        "max_km": 40,
    },
    "must_apply": {
        "min_score": 8,
        "max_days": 3,
        "max_hours": 20,
        "reminder_hours": 24,
        "low_competition_categories": ["insurance", "finance", "cloud", "security"],
    },
    "fetch": {
        "delay_per_site_seconds": 5,
        "delay_per_api_seconds": 2,
        "timeout_seconds": 20,
        "browser_fallback": True,
        "browser_timeout_seconds": 90,
    },
}


def _read(name: str) -> dict:
    path = ROOT / name
    return (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}


def _merge(base: dict, user: dict) -> dict:
    config = {**base, **user}
    for key, value in base.items():
        if isinstance(value, dict):
            config[key] = {**value, **(user.get(key) or {})}
    return config


def load_config() -> dict:
    return _merge(_merge(DEFAULTS, _read("config.yaml")), _read("config.local.yaml"))


CONFIG = load_config()
