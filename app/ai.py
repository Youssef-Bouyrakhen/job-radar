"""Layer 2 filter: an AI scores each job 1-10 for how well it fits you.

Only jobs that passed the keyword filter are sent. Free providers only: Gemini first, then
Groq, Cloudflare Workers AI (free 10,000 neurons a day) and OpenRouter (free models).
Without any of their keys in .env this
layer is skipped and every keyword-filtered job is shown.
"""
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import db, details
from .config import CONFIG, DEFAULTS

log = logging.getLogger("jobradar")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "score": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
            "hours_per_week": {"type": "STRING"},
            "full_time": {"type": "BOOLEAN"},
            "blocker": {"type": "STRING"},
        },
        "required": ["id", "score", "reason", "full_time", "blocker"],
    },
}

INSTRUCTIONS = """You rate job ads for one person. Be strict and consistent.

THE PERSON
{profile}

SCORE (whole number 1-10)
9-10  Werkstudent / working-student job doing real IT work: software development, data,
      cloud, DevOps, IT security, IT infrastructure or similar. Part-time (max 20 h/week in
      the semester). In or near Darmstadt / Frankfurt am Main, or remote.
7-8   Good IT student job with a small drawback: e.g. mainly IT support / helpdesk,
      IT consulting, IT project assistance, longer commute, hours not stated.
4-6   Only partly IT: e.g. project or product management with some tech, digital marketing,
      business roles using SAP or Excel, data entry.
1-3   Any of these:
      - NOT an IT job: mechanical or electrical engineering, Konstruktion, CAD, construction,
        finance, tax, controlling, HR, logistics, marketing, communication.
      - A SALES role (Sales, Vertrieb, Business Development, Account Management), even if it
        sells software, cloud or AI products.
      - Really full-time (more than 20 h/week during the semester) - see below.
      - Needs a finished degree or several years of work experience.
      - Far from Darmstadt (more than 40 km) and not remote. Read the place from "location" and
        the description ("am Standort Koblenz"). Location "unknown" does NOT mean Darmstadt.

FULL-TIME CHECK
Job boards sometimes label student jobs "Vollzeit". Read the description and decide:
- Student works about 10-20 h/week during the semester (more only in semester breaks)
  -> NOT full-time (full_time = false).
- Asks for 30-40 h/week, a regular "Vollzeit" position without student hours, or a normal
  permanent employee -> full_time = true and score at most 3.

BLOCKER CHECK
blocker = a few English words if ONE of these is true, else "":
- the ad requires 2 or more years of (professional) work experience -> "2+ years experience"
- the ad is posted by a staffing / recruiting agency (Personaldienstleister, Zeitarbeit, Personalvermittlung)
  for a client, not by the employer itself -> "staffing agency"
- only Master's students may apply (Bachelor's students not) -> "Master students only"
Wanting "first experience", "Kenntnisse" or a running Bachelor's / Master's degree is NOT a blocker.

For EVERY job return: id, score, reason (ONE short English sentence, max 25 words, giving the
main reason for the score), hours_per_week (e.g. "15-20" or "20", only as the ad states them; "" if the ad
doesn't say - never assume 20 because it is a Werkstudent job),
full_time (true/false), blocker ("" or the blocker).

JOBS (JSON)
{jobs}
"""


class RateLimited(Exception):
    pass


class Overloaded(Exception):
    """Google's servers are busy ("high demand"). Not your fault, try again later."""


def settings() -> dict:
    return CONFIG["gemini"]


def enabled() -> bool:
    return bool(settings().get("enabled", True) and providers())


# ---- AI providers: Gemini first, then the backups --------------------------------
# Only providers with a key in .env are used. When one hits its limit (or its servers
# are busy), the next one takes over until the first one's pause is over.

PROVIDERS = (  # name, key in .env, section in config.yaml
    ("Gemini", "GEMINI_API_KEY", "gemini"),
    ("Groq", "GROQ_API_KEY", "groq"),
    ("Cloudflare", "CLOUDFLARE_API_TOKEN", "cloudflare"),  # also needs CLOUDFLARE_ACCOUNT_ID
    ("OpenRouter", "OPENROUTER_API_KEY", "openrouter"),
    ("GitHub", "GITHUB_MODELS_TOKEN", "github"),   # off: GitHub Models was retired on 2026-07-30
    ("Mistral", "MISTRAL_API_KEY", "mistral"),     # off (enabled: false): they need a payment method
    ("Cerebras", "CEREBRAS_API_KEY", "cerebras"),
)
OPENAI_STYLE_URLS = {
    "Groq": "https://api.groq.com/openai/v1/chat/completions",
    "OpenRouter": "https://openrouter.ai/api/v1/chat/completions",
    "GitHub": "https://models.github.ai/inference/chat/completions",
    "Mistral": "https://api.mistral.ai/v1/chat/completions",
    "Cerebras": "https://api.cerebras.ai/v1/chat/completions",
    "Cloudflare": "https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1/chat/completions",
}
# Cloudflare's free limit is 10,000 "neurons" a day (its unit of AI work), not a number of requests.
# Job Radar counts the neurons each answer reports and stops at cloudflare.daily_neurons (below 10,000),
# so it never needs the Workers Paid plan.
NEURONS = {"Cloudflare"}

# ---- daily budget and priorities ---------------------------------------------------
# Every request is counted per provider and day (data: ai_usage). Work that matters less only
# runs while enough of today's requests are left (ai_budget: keep_percent), so scoring never runs out:
#   1 "score": scoring jobs (incl. the Must apply blocker check) - may use everything
#   2 "mail":  application emails the rules can't sort
#   3 "jobs":  reading careers pages without a feed, finding websites, imports
#   4 "low":   categories, company sizes, Initiativ reasons - only leftovers
PRIORITIES = ("score", "mail", "jobs", "low")


class BudgetSaved(RateLimited):
    """Not an error: the rest of today's AI requests are kept for more important work.
    (A kind of RateLimited, so everything that stops on a limit also stops here.)"""


def stop_note(e: Exception, later: str) -> str:
    """The warning for the Companies page when AI work stopped early, e.g. later = "some job lists will be read next run"."""
    if isinstance(e, BudgetSaved):
        return f"The rest of today's AI requests is kept for more important work (scoring jobs first): {later}."
    return f"The AI's free limit is used up: {later}."


def budget_of(name: str) -> int:
    """Estimated free requests a day (Cloudflare: neurons). A config.yaml without a provider (e.g. GitHub) gets the default."""
    if name in NEURONS:
        return int(_section(name)["daily_neurons"])
    limits = {**DEFAULTS["ai_budget"]["daily_requests"], **(CONFIG["ai_budget"].get("daily_requests") or {})}
    return int(limits.get(name, 0))


def used_today() -> dict[str, int]:
    """Requests used today per provider (Cloudflare: neurons)."""
    neurons = db.ai_neurons_today()
    return {**db.ai_usage_today(), **{p: math.ceil(neurons.get(p, 0)) for p in NEURONS}}


def usage() -> list[dict]:
    """Per provider with a key: requests (or neurons) used today, the daily budget, how many are left."""
    used = used_today()
    return [{"name": p, "used": used.get(p, 0), "budget": budget_of(p), "left": max(0, budget_of(p) - used.get(p, 0)),
             "unit": "neurons" if p in NEURONS else "requests"} for p in providers()]


def _keep(priority: str) -> int:
    keep = {**DEFAULTS["ai_budget"]["keep_percent"], **(CONFIG["ai_budget"].get("keep_percent") or {})}
    return 0 if priority == "score" else int(keep.get(priority, 0))


def _allowed(priority: str) -> bool:
    keep = _keep(priority)
    if not keep:
        return True
    rows = [r for r in usage() if r["budget"]]
    # Each provider counts the same (Cloudflare's budget is in neurons, the others' in requests)
    left = sum(r["left"] / r["budget"] for r in rows)
    return left > len(rows) * keep / 100
_paused: dict[str, float] = {}   # provider -> time.monotonic() when it may be used again
_active: str | None = None       # the provider that answered last
_warned: set[str] = set()


def _section(name: str) -> dict:
    return CONFIG[dict((p[0], p[2]) for p in PROVIDERS)[name]]


def model_of(name: str) -> str:
    return _section(name)["model"]


def prompt_limit(name: str) -> int | None:
    """Longest prompt (characters) this provider takes in one request, None = no limit.
    Groq's free tier and GitHub Models only take about 8,000 tokens per request."""
    return _section(name).get("max_prompt_chars")


def providers() -> list[str]:
    """Providers with a key in .env, in order. OpenRouter only with a free model."""
    found = []
    for name, key, section in PROVIDERS:
        if not os.environ.get(key) or (name == "Cloudflare" and not os.environ.get("CLOUDFLARE_ACCOUNT_ID")):
            continue
        if name != "Gemini" and not CONFIG[section].get("enabled", True):  # gemini: enabled = AI on/off
            continue
        if name == "OpenRouter" and not model_of(name).endswith(":free"):
            if name not in _warned:
                _warned.add(name)
                log.warning("OpenRouter skipped: %s is not a free model (must end with ':free')", model_of(name))
            continue
        found.append(name)
    return found


def _ready(name: str, used: dict[str, int]) -> bool:
    """Not paused and still has requests left today."""
    return time.monotonic() >= _paused.get(name, 0) and used.get(name, 0) < budget_of(name)


def next_provider() -> str | None:
    """The provider the next request goes to, None if all are paused or used up."""
    used = used_today()
    return next((p for p in providers() if _ready(p, used)), None)


def provider_status() -> dict:
    """For the AI usage bar and Settings: each provider with used / estimated limit today and its
    status - active (gets the next request), next (a backup), paused (limit hit or servers busy,
    back in a few minutes) or empty (today's requests used up)."""
    now = time.monotonic()
    active, rows = None, []
    for u in usage():
        name = u["name"]
        paused = max(0.0, _paused.get(name, 0) - now)
        if u["left"] <= 0:
            status = "empty"
        elif paused:
            status = "paused"
        elif active is None:
            status, active = "active", name
        else:
            status = "next"
        rows.append({**u, "model": model_of(name), "status": status, "paused_minutes": math.ceil(paused / 60)})
    reset = datetime.combine(datetime.now(timezone.utc).date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    return {
        "active": active,
        "model": model_of(active) if active else None,
        "providers": rows,
        "all_empty": bool(rows) and all(r["status"] == "empty" for r in rows),
        "reset_at": reset.isoformat(timespec="seconds"),  # the daily counts start again (UTC midnight)
    }


_last_call = 0.0


def _score_prompt(jobs: list[dict]) -> str:
    payload = [{
        "id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"] or "unknown",
        "employment_label": j["employment"], "hours_found": j["hours"],
        "description": (j["description"] or "")[:3500],
    } for j in jobs]
    return INSTRUCTIONS.format(profile=settings()["profile"].strip(), jobs=json.dumps(payload, ensure_ascii=False))


def _ask_gemini(jobs: list[dict]) -> list[dict]:
    return ask_json(_score_prompt(jobs), RESPONSE_SCHEMA, temperature=0.2, priority="score")


def ask_json(prompt: str, schema: dict, temperature: float, priority: str = "low") -> list | dict:
    """Send one prompt to the first AI provider that isn't paused and has requests left today,
    and get JSON back in the given shape. Raises RateLimited / Overloaded only when every
    provider is paused or used up, BudgetSaved when the rest is kept for more important work,
    another error when every provider that was tried failed."""
    global _active
    if not _allowed(priority):
        raise BudgetSaved("AI requests left today are kept for more important work")
    error = None
    for name in providers():
        if not _ready(name, used_today()):
            continue  # paused, or this provider's free requests for today are used up
        if (prompt_limit(name) or len(prompt)) < len(prompt):
            continue  # too long for this provider's free tier
        db.count_ai_request(name)
        try:
            answer = _ask_gemini_api(prompt, schema, temperature) if name == "Gemini" \
                else _ask_openai_style(name, prompt, schema, temperature)
        except (RateLimited, Overloaded) as e:
            if isinstance(e, RateLimited) and "day" in str(e).lower():  # its daily quota is gone: skip it until tomorrow
                if name in NEURONS:
                    db.count_ai_neurons(name, budget_of(name))
                else:
                    db.use_up_ai(name, budget_of(name))
            minutes = 10 if isinstance(e, RateLimited) else 3
            _paused[name] = time.monotonic() + minutes * 60
            log.warning("%s: %s, pausing it for %d min and using the next AI provider (%s)",
                        name, "limit reached" if isinstance(e, RateLimited) else "servers busy", minutes, str(e)[:160])
            error = e
            continue
        except Exception as e:  # e.g. a broken answer or "request too large": the next provider may manage
            _paused[name] = time.monotonic() + 5 * 60
            log.warning("%s failed (%s), pausing it for 5 min and using the next AI provider", name, str(e)[:160])
            error = e
            continue
        if _active != name:
            log.info("AI provider now: %s (%s)", name, model_of(name))
        _active = name
        return answer
    raise error or RateLimited("every AI provider is paused or has used up today's requests")


def _ask_openai_style(name: str, prompt: str, schema: dict, temperature: float) -> list | dict:
    """Groq, Cloudflare and OpenRouter speak the OpenAI format. Same prompt; the JSON shape is described
    in words because they only promise "some JSON object" (so a list goes under "items")."""
    wrapped = schema["type"] == "ARRAY"
    shape = {"type": "OBJECT", "properties": {"items": schema}} if wrapped else schema
    text = (f"{prompt}\n\nAnswer ONLY with JSON in exactly this shape (JSON schema):\n"
            f"{json.dumps(shape)}{_section(name).get('prompt_suffix', '')}")
    key = dict((p[0], p[1]) for p in PROVIDERS)[name]
    body = {"model": model_of(name), "temperature": temperature, "messages": [{"role": "user", "content": text}],
            "response_format": {"type": "json_object"}}
    if _section(name).get("max_tokens"):
        body["max_tokens"] = _section(name)["max_tokens"]
    url = OPENAI_STYLE_URLS[name].format(account=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""))
    r = httpx.post(url, headers={"Authorization": f"Bearer {os.environ[key]}"}, json=body, timeout=180)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if name in NEURONS:
        _count_neurons(name, data)
    if not data.get("error") and data.get("errors"):  # Cloudflare's own error format
        data["error"] = {"message": "; ".join(str(e.get("message", e) if isinstance(e, dict) else e) for e in data["errors"])}
    if "daily free allocation" in str((data.get("error") or {}).get("message", "")).lower():
        raise RateLimited("day: Cloudflare's free neurons for today are used up")
    status = r.status_code
    code = (data.get("error") or {}).get("code")
    if status == 200 and isinstance(code, int):  # OpenRouter sometimes reports errors inside a 200
        status = code
    if status == 429:  # GitHub says which limit in a header: "UserByModelByDay" = today's requests are gone
        raise RateLimited(f"{r.headers.get('x-ratelimit-type', '')} "
                          f"{str((data.get('error') or {}).get('message', ''))[:200]}".strip())
    if isinstance(status, int) and status >= 500:
        raise Overloaded(f"HTTP {status}")
    if status != 200 or not data.get("choices"):
        raise RuntimeError(f"{name} HTTP {status}: {str((data.get('error') or {}).get('message', r.text))[:200]}")
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", data["choices"][0]["message"]["content"].strip())
    answer = json.loads(content)
    if wrapped and isinstance(answer, dict):  # some models name the list differently ("jobs", ...)
        lists = [v for v in answer.values() if isinstance(v, list)]
        return answer.get("items") or (lists[0] if lists else [])
    return answer


def _count_neurons(name: str, data: dict) -> None:
    """Cloudflare reports the neurons of each answer. Without that number: the price from config.yaml."""
    u = data.get("usage") or {}
    neurons = u.get("neurons")
    if neurons is None:
        cfg = _section(name)
        neurons = (u.get("prompt_tokens", 0) * cfg["neurons_per_m_input"]
                   + u.get("completion_tokens", 0) * cfg["neurons_per_m_output"]) / 1e6
    db.count_ai_neurons(name, float(neurons))


def _ask_gemini_api(prompt: str, schema: dict, temperature: float) -> list | dict:
    global _last_call
    cfg = settings()
    gap = cfg["seconds_between_requests"] - (time.monotonic() - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.monotonic()

    generation = {"responseMimeType": "application/json", "responseSchema": schema, "temperature": temperature}
    if cfg.get("thinking_level"):
        generation["thinkingConfig"] = {"thinkingLevel": cfg["thinking_level"]}

    for attempt in range(3):
        r = httpx.post(API.format(model=cfg["model"]), headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                       json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation},
                       timeout=180)
        if r.status_code not in (500, 503) or attempt == 2:
            break
        time.sleep(15 * (attempt + 1))  # "model is overloaded": usually gone after a few seconds
    if r.status_code == 429:
        raise RateLimited(r.json().get("error", {}).get("message", "")[:200])
    if r.status_code in (500, 503):
        raise Overloaded(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.json().get('error', {}).get('message', r.text)[:200]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return json.loads(text)


def _complete_descriptions(jobs: list[dict]) -> None:
    """The full ad for jobs with only a title or a short teaser, no place or no hours, so the AI
    (and Must apply) see the real city, hours and job type."""
    for j in jobs:
        if details.needs_details(j):
            details.complete(j)


def score_pending_jobs() -> int:
    """Score jobs that passed the keyword filter but have no AI score yet. Returns how many.
    The waiting line: when every provider is at its limit, the jobs simply stay unscored and
    the next round (every 5 minutes) tries again as soon as any provider is free."""
    cfg = settings()
    if not enabled() or not next_provider():
        return 0
    jobs = db.jobs_to_score(cfg["max_jobs_per_run"])
    if not jobs:
        return 0
    if cfg.get("fetch_full_description", True):
        _complete_descriptions(jobs)

    scored, retries = 0, 0
    while jobs:
        # As many jobs as fit into one request of the provider that answers next (Groq / GitHub take less)
        limit = prompt_limit(next_provider() or "Gemini")
        batch = jobs[:cfg["batch_size"]]
        while len(batch) > 1 and limit and len(_score_prompt(batch)) > limit:
            batch = batch[:-1]
        jobs = jobs[len(batch):]
        try:
            answers = _ask_gemini(batch)
        except (RateLimited, Overloaded) as e:
            # One that only takes shorter requests is still free: smaller batch, same jobs
            retries += 1
            if next_provider() and retries <= len(providers()):
                jobs = batch + jobs
                continue
            log.warning("AI scoring: every provider is at its limit or busy, %d job(s) wait for a score (%s)",
                        db.count_jobs()["ai_pending"], str(e)[:120])
            break
        except Exception as e:
            # counts as a failed try; after 3 failed tries a job is shown without a score
            db.count_ai_attempt([j["id"] for j in batch])
            log.warning("AI scoring failed: %s", e)
            break
        ids = {j["id"] for j in batch}
        answered = {a.get("id") for a in answers}
        db.count_ai_attempt([i for i in ids if i not in answered])
        results = []
        for a in answers:
            if a.get("id") not in ids:
                continue
            score = max(1, min(10, int(a.get("score", 1))))
            if a.get("full_time"):
                score = min(score, 3)
            hours = (a.get("hours_per_week") or "").strip()
            results.append({"id": a["id"], "score": score, "reason": (a.get("reason") or "").strip()[:300],
                            "full_time": bool(a.get("full_time")),
                            "blocker": (a.get("blocker") or "").strip().strip(".")[:120],
                            "hours": f"{hours.replace('-', '–')} h/week" if hours else ""})
        db.save_scores(results, f"{_active}: {model_of(_active)}")
        scored += len(results)
    if scored:
        log.info("AI scored %d job(s)", scored)
    return scored


REQUEUE = "1"  # bump to put unscored jobs back in the waiting line once more


def requeue_unscored(days: int = 14) -> int:
    """Once: recent jobs that were given up on (3 failed tries, mostly limits and too-long requests
    before v9.1) go back into the waiting line."""
    if db.get_setting("ai_requeue") == REQUEUE:
        return 0
    n = db.requeue_unscored(days)
    db.set_setting("ai_requeue", REQUEUE)
    if n:
        log.info("AI scoring: %d job(s) without a score are back in the waiting line", n)
    return n


_background = threading.Lock()


def score_in_background() -> None:
    """Start scoring in a background thread (unless one is already running)."""
    if not enabled() or not _background.acquire(blocking=False):
        return

    def run():
        from . import must_apply, telegram  # imported here: telegram itself imports this module
        try:
            while score_pending_jobs():  # keep going while there is work and no error/pause
                must_apply.update()  # "🔥 MUST APPLY" goes out first, then the other good jobs
                telegram.notify_must()
                telegram.notify_pending()  # send good jobs as soon as they are scored
            must_apply.update()  # also every 5 minutes without new jobs: places looked up later, reminders
            telegram.notify_must()
            telegram.notify_pending()
        except Exception:
            log.exception("Gemini scoring crashed")
        finally:
            _background.release()

    threading.Thread(target=run, daemon=True, name="gemini").start()
