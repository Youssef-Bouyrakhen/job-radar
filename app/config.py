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
    "groq": {"model": "openai/gpt-oss-120b", "max_prompt_chars": 18000},
    "openrouter": {"model": "poolside/laguna-s-2.1:free"},
    "github": {"model": "openai/gpt-4.1-mini", "max_prompt_chars": 18000},
    "mistral": {"enabled": False, "model": "mistral-small-latest"},
    "cerebras": {"enabled": False, "model": "gpt-oss-120b"},
    "ai_budget": {
        "daily_requests": {"Gemini": 50, "Groq": 50, "OpenRouter": 50, "GitHub": 150, "Mistral": 300, "Cerebras": 300},
        "keep_percent": {"mail": 10, "jobs": 30, "low": 60},
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
        "max_ai_pages_per_run": 30,
        "max_auto_added_per_run": 10,
        "max_initiativ_reasons_per_run": 60,
        "ai_seconds_between_requests": 12,
        "max_size_lookups_per_run": 200,
        "daily_run_hour": 3,
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
    "must_apply": {
        "min_score": 8,
        "max_days": 3,
        "max_km": 30,
        "center": "Darmstadt",
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
