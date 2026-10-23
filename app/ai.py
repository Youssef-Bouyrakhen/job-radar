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
from zoneinfo import ZoneInfo

import httpx

from . import db, details
from .clock import GERMANY
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
NEVER more than 5 for a job more than 40 km from Darmstadt (not remote) or a full-time job.

FULL-TIME CHECK
Job boards sometimes label student jobs "Vollzeit". Read the description and decide:
- Student works about 10-20 h/week during the semester (more only in semester breaks)
  -> NOT full-time (full_time = false).
- Hours PER MONTH ("40 Stunden im Monat", "monthly working time 40 hours", common for student
  assistants / HiWi at universities and research institutes) are about a quarter of that per week
  (40 a month = about 9 a week) -> NOT full-time.
- "Vollzeit" alone, with no student or part-time hours -> full_time = true.
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
main reason for the score), hours_per_week (e.g. "15-20" or "20", only as the ad states them; hours per month
divided by 4.33; "" if the ad doesn't say - never assume 20 because it is a Werkstudent job),
full_time (true/false), blocker ("" or the blocker).

JOBS (JSON)
{jobs}
"""


class RateLimited(Exception):
    """The provider said "limit reached". until: its daily limit is gone, back at this time (its real reset).
    wait: only a short limit (per minute), seconds until it takes requests again. limit: its real daily limit,
    if it said so."""
    def __init__(self, message: str = "", until: datetime | None = None, wait: float | None = None,
                 limit: int | None = None):
        super().__init__(message)
        self.until, self.wait, self.limit = until, wait, limit


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

# When each provider's free day starts again, on its own clock: Gemini at midnight US Pacific time
# (09:00 in Germany), the others at midnight UTC (02:00 in Germany; in winter 08:00 / 01:00).
# Groq has no fixed time: each request comes back 24 hours after it was made (it says when in its answers).
PACIFIC = ZoneInfo("America/Los_Angeles")
RESET_ZONE = {"Gemini": PACIFIC}
ROLLING = {"Groq"}
REPORT_TTL = 3600  # a provider's own numbers (Groq, OpenRouter) count this many seconds, then the counter again

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


def _zone(name: str):
    return RESET_ZONE.get(name, timezone.utc)


def quota_day(name: str) -> str:
    """The provider's own "today" for the counter: the UTC date, for Gemini the Pacific date ("2026-10-22 PT")."""
    day = datetime.now(_zone(name)).date().isoformat()
    return f"{day} PT" if name in RESET_ZONE else day


def reset_at(name: str) -> datetime:
    """When the provider's next free day starts (its midnight)."""
    zone = _zone(name)
    return datetime.combine(datetime.now(zone).date() + timedelta(days=1), datetime.min.time(), zone)


# A provider that answered "today's limit is used up" stays off until its real reset, also after a restart
# (data: setting "ai_limits"). Its real daily limit is kept too when it said it (Gemini does in the answer).
_limits: dict | None = None


def _saved_limits() -> dict:
    global _limits
    if _limits is None:
        _limits = json.loads(db.get_setting("ai_limits") or "{}")
        _limits.setdefault("until", {})
        _limits.setdefault("daily", {})
    return _limits


def blocked_until(name: str) -> datetime | None:
    """After a "daily limit reached" answer: when the provider is back (None = not blocked)."""
    until = _saved_limits()["until"].get(name)
    if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
        return datetime.fromisoformat(until)
    return None


def _hit_daily_limit(name: str, until: datetime, limit: int | None) -> None:
    saved = _saved_limits()
    saved["until"][name] = until.astimezone(timezone.utc).isoformat(timespec="seconds")
    if limit:
        saved["daily"][name] = limit
    db.set_setting("ai_limits", json.dumps(saved))


# What the providers themselves say is left: Groq in every answer (headers), OpenRouter on request (/key)
_reported: dict[str, dict] = {}   # provider -> {"limit", "remaining", "at": time.monotonic(), "full_at": datetime}


def _report(name: str) -> dict | None:
    if name == "OpenRouter":
        _ask_openrouter_left()
    r = _reported.get(name)
    return r if r and time.monotonic() - r["at"] < REPORT_TTL else None


_openrouter_asked = 0.0


def _ask_openrouter_left() -> None:
    """OpenRouter tells how many free-model requests are left today (costs no request). At most every 5 minutes,
    in between each answer takes one off (ask_json)."""
    global _openrouter_asked
    if time.monotonic() - _openrouter_asked < 300:
        return
    _openrouter_asked = time.monotonic()  # also on failure: don't ask again at once
    try:
        free = httpx.get("https://openrouter.ai/api/v1/key", timeout=5,
                         headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}).json()["data"]["free_model_daily_requests"]
        _reported["OpenRouter"] = {"limit": int(free["limit"]), "remaining": int(free["remaining"]), "at": time.monotonic()}
    except Exception as e:
        log.debug("OpenRouter: could not ask for its free requests left (%s)", e)


def _note_headers(name: str, r: httpx.Response) -> None:
    """Groq says its daily requests (limit, left, when all are back) in every answer."""
    h = r.headers
    if name in ROLLING and h.get("x-ratelimit-limit-requests") and h.get("x-ratelimit-remaining-requests"):
        full = _seconds(h.get("x-ratelimit-reset-requests"))
        _reported[name] = {"limit": int(h["x-ratelimit-limit-requests"]), "remaining": int(h["x-ratelimit-remaining-requests"]),
                           "at": time.monotonic(), "full_at": datetime.now(timezone.utc) + timedelta(seconds=full or 0)}


def _seconds(text: str | None) -> float | None:
    """ "41s", "1m26.4s", "585ms", "2h3m", "30" -> seconds (None if empty)."""
    if not text:
        return None
    text = str(text).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = re.findall(r"([\d.]+)(ms|h|m|s)", text)
    return sum(float(n) * {"ms": 0.001, "h": 3600, "m": 60, "s": 1}[u] for n, u in parts) if parts else None


def budget_of(name: str) -> int:
    """Free requests a day (Cloudflare: neurons): the provider's own number if it said it, else the estimate
    from config.yaml. A config.yaml without a provider (e.g. GitHub) gets the default."""
    if name in NEURONS:
        return int(_section(name)["daily_neurons"])
    if name in _saved_limits()["daily"]:
        return int(_saved_limits()["daily"][name])
    limits = {**DEFAULTS["ai_budget"]["daily_requests"], **(CONFIG["ai_budget"].get("daily_requests") or {})}
    return int(limits.get(name, 0))


def used_today() -> dict[str, int]:
    """Requests used in each provider's own today (Cloudflare: neurons), as counted by Job Radar."""
    found = {}
    for p in providers():
        day = quota_day(p)
        found[p] = math.ceil(db.ai_neurons_on(day).get(p, 0)) if p in NEURONS else db.ai_usage_on(day).get(p, 0)
    return found


def usage() -> list[dict]:
    """Per provider with a key: requests (or neurons) used today, the daily limit, how many are left, and where
    the numbers come from: "provider" (it said so itself) or "counter" (Job Radar counted)."""
    used, rows = used_today(), []
    for p in providers():
        budget, n, source = budget_of(p), used.get(p, 0), "counter"
        real = _report(p)
        if real:
            budget, n, source = real["limit"], real["limit"] - real["remaining"], "provider"
        left = 0 if blocked_until(p) else max(0, budget - n)
        rows.append({"name": p, "used": n, "budget": budget, "left": left, "source": source,
                     "unit": "neurons" if p in NEURONS else "requests"})
    return rows


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


def spare_requests(priority: str) -> int:
    """Requests left today for this kind of work before the reserve for more important work (keep_percent)
    is reached, e.g. Groq 1000 a day, 700 left, keep 30% -> 400. Cloudflare counts neurons, not requests: left out."""
    keep = _keep(priority) / 100
    return sum(max(0, r["left"] - math.ceil(r["budget"] * keep)) for r in usage()
               if r["budget"] and r["unit"] == "requests")
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


def _left() -> dict[str, int]:
    return {u["name"]: u["left"] for u in usage()}


def _ready(name: str, left: dict[str, int]) -> bool:
    """Not paused, not at its daily limit and still has requests left today."""
    return time.monotonic() >= _paused.get(name, 0) and left.get(name, 0) > 0


def next_provider() -> str | None:
    """The provider the next request goes to, None if all are paused or used up."""
    left = _left()
    return next((p for p in providers() if _ready(p, left)), None)


def provider_status() -> dict:
    """For the AI usage bar and Settings: each provider with used / limit today and its status - active
    (gets the next request), next (a backup), paused (a short limit or servers busy, back in a few minutes),
    limit (it answered "today's limit reached") or empty (Job Radar's count of today's requests is used up).
    back_at: when a paused / limit / empty one works again; resets_at: when its free day starts again."""
    now, utc_now = time.monotonic(), datetime.now(timezone.utc)
    active, rows = None, []
    for u in usage():
        name = u["name"]
        paused = max(0.0, _paused.get(name, 0) - now)
        until, real = blocked_until(name), _report(name)
        resets = None if name in ROLLING else reset_at(name)
        back = None
        if until:
            status, back = "limit", until
        elif u["left"] <= 0:
            status, back = "empty", (real or {}).get("full_at") or resets
        elif paused:
            status, back = "paused", utc_now + timedelta(seconds=paused)
        elif active is None:
            status, active = "active", name
        else:
            status = "next"
        iso = lambda t: t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None
        rows.append({**u, "model": model_of(name), "status": status, "paused_minutes": math.ceil(paused / 60),
                     "back_at": iso(back), "resets_at": iso(resets)})
    waiting = [r["back_at"] for r in rows if r["back_at"]]
    return {
        "active": active,
        "model": model_of(active) if active else None,
        "providers": rows,
        "all_empty": bool(rows) and all(r["status"] in ("limit", "empty") for r in rows),
        "next_back_at": min(waiting) if waiting else None,  # the first provider that works again
    }


def next_reset() -> datetime | None:
    """The next time any provider's free day starts again (more requests left)."""
    found = [reset_at(p) for p in providers() if p not in ROLLING]
    found += [r["full_at"] for p in providers() if p in ROLLING and (r := _report(p)) and r.get("full_at")
              and r["full_at"] > datetime.now(timezone.utc)]
    return min(found) if found else None


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
        if not _ready(name, _left()):
            continue  # paused, at its daily limit, or this provider's free requests for today are used up
        if (prompt_limit(name) or len(prompt)) < len(prompt):
            continue  # too long for this provider's free tier
        day = quota_day(name)
        db.count_ai_request(name, day)  # counted before: requests running at the same time see it
        try:
            answer = _ask_gemini_api(prompt, schema, temperature) if name == "Gemini" \
                else _ask_openai_style(name, prompt, schema, temperature)
        except (RateLimited, Overloaded) as e:
            db.count_ai_request(name, day, -1)  # a refused request doesn't count at the provider either
            if isinstance(e, RateLimited) and e.until:  # today's limit is gone: off until its real reset
                _hit_daily_limit(name, e.until, e.limit)
                log.warning("%s: daily limit reached%s, off until %s German time, using the next AI provider (%s)",
                            name, f" ({e.limit} requests)" if e.limit else "",
                            e.until.astimezone(GERMANY).strftime("%d.%m. %H:%M"), str(e)[:160])
            else:
                wait = (e.wait if isinstance(e, RateLimited) else None) or (600 if isinstance(e, RateLimited) else 180)
                _paused[name] = time.monotonic() + wait
                log.warning("%s: %s, pausing it for %s and using the next AI provider (%s)",
                            name, "limit reached" if isinstance(e, RateLimited) else "servers busy",
                            f"{math.ceil(wait / 60)} min" if wait >= 60 else f"{math.ceil(wait)} s", str(e)[:160])
            error = e
            continue
        except Exception as e:  # e.g. a broken answer or "request too large": the next provider may manage
            _paused[name] = time.monotonic() + 5 * 60
            log.warning("%s failed (%s), pausing it for 5 min and using the next AI provider", name, str(e)[:160])
            error = e
            continue
        if name in _reported and name not in ROLLING:  # OpenRouter: one less until it is asked again
            _reported[name]["remaining"] = max(0, _reported[name]["remaining"] - 1)
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
    _note_headers(name, r)
    if name in NEURONS:
        _count_neurons(name, data)
    if not data.get("error") and data.get("errors"):  # Cloudflare's own error format
        data["error"] = {"message": "; ".join(str(e.get("message", e) if isinstance(e, dict) else e) for e in data["errors"])}
    if "daily free allocation" in str((data.get("error") or {}).get("message", "")).lower():
        raise RateLimited("day: Cloudflare's free neurons for today are used up", until=reset_at(name))
    status = r.status_code
    code = (data.get("error") or {}).get("code")
    if status == 200 and isinstance(code, int):  # OpenRouter sometimes reports errors inside a 200
        status = code
    if status == 429:
        raise _limit_error(name, r, str((data.get("error") or {}).get("message", ""))[:200])
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


def _limit_error(name: str, r: httpx.Response, message: str) -> RateLimited:
    """A "limit reached" answer of Groq / OpenRouter / Cloudflare: a short limit (per minute) is a pause,
    a daily one keeps the provider off until it is back - Groq says when (retry-after), OpenRouter too
    (X-RateLimit-Reset), otherwise at its midnight. GitHub names the limit in a header ("UserByModelByDay")."""
    text = f"{r.headers.get('x-ratelimit-type', '')} {message}".lower()
    wait = _seconds(r.headers.get("retry-after"))
    if not any(w in text for w in ("per day", "per-day", "byday", "(rpd)", "(tpd)")):
        return RateLimited(message, wait=wait)
    now = datetime.now(timezone.utc)
    until = reset_at(name)
    if name in ROLLING and wait:
        until = now + timedelta(seconds=wait)
    elif (ms := _seconds(r.headers.get("x-ratelimit-reset"))) and ms / 1000 > now.timestamp():
        until = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return RateLimited(f"day: {message}", until=until)


def _gemini_limit(error: dict) -> RateLimited:
    """Gemini's "limit reached" names the limit: "...PerDay..." = today's requests are gone (back at midnight
    US Pacific time, it also says how many there are), "...PerMinute..." = wait the retryDelay."""
    details = error.get("details") or []
    violations = [v for d in details for v in d.get("violations") or []]
    ids = ", ".join(v.get("quotaId", "") for v in violations)
    message = f"{error.get('message', '')[:160]}{f' [{ids}]' if ids else ''}"
    daily = [v for v in violations if "perday" in v.get("quotaId", "").lower()]
    if daily or "per day" in message.lower():
        value = str((daily or [{}])[0].get("quotaValue", ""))
        return RateLimited(f"day: {message}", until=reset_at("Gemini"), limit=int(value) if value.isdigit() else None)
    wait = next((_seconds(d["retryDelay"]) for d in details if d.get("retryDelay")), None)
    return RateLimited(message, wait=wait)


def _count_neurons(name: str, data: dict) -> None:
    """Cloudflare reports the neurons of each answer. Without that number: the price from config.yaml."""
    u = data.get("usage") or {}
    neurons = u.get("neurons")
    if neurons is None:
        cfg = _section(name)
        neurons = (u.get("prompt_tokens", 0) * cfg["neurons_per_m_input"]
                   + u.get("completion_tokens", 0) * cfg["neurons_per_m_output"]) / 1e6
    db.count_ai_neurons(name, quota_day(name), float(neurons))


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
        raise _gemini_limit(r.json().get("error", {}))
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
