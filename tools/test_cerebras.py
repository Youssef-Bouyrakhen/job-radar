"""Standalone test: is Cerebras (gpt-oss-120b) good enough as one more free AI?

Not part of the app: no scheduler, no database, no Telegram, nothing on the server. It sends the app's own
prompts (scoring: ai.INSTRUCTIONS, careers pages: company_jobs.EXTRACT_PROMPT) through the app's own
OpenAI-style request code and prints the raw answer, the tokens and the time it took.

Run from the job-radar folder:  .venv\\Scripts\\python tools\\test_cerebras.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JOBRADAR_DB"] = str(Path(tempfile.gettempdir()) / "jr-cerebras-test.db")  # never the real database

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

from app import ai, company_jobs  # noqa: E402

JOBS = [  # two made-up ads: one should score high, one low
    {"id": 1, "title": "Werkstudent Softwareentwicklung Python (m/w/d)", "company": "Beispiel Software GmbH",
     "location": "Darmstadt", "employment": "Werkstudent", "hours": "15-20 h/Woche",
     "description": "Du unterstützt unser Backend-Team bei der Entwicklung von REST-APIs mit Python und FastAPI, "
                    "schreibst Tests und arbeitest mit Docker und Git. Du studierst Informatik oder ähnliches, "
                    "erste Programmiererfahrung reicht. 15-20 Stunden pro Woche im Semester, flexible Zeiten, "
                    "Homeoffice möglich."},
    {"id": 2, "title": "Account Manager Cloud Solutions (m/w/d)", "company": "Muster Vertrieb AG",
     "location": "Koblenz", "employment": "Vollzeit", "hours": "",
     "description": "Als Account Manager betreust du unsere Bestandskunden und verkaufst Cloud-Lösungen. "
                    "Abgeschlossenes Studium und mindestens 3 Jahre Vertriebserfahrung. Vollzeit, 40 Stunden, "
                    "am Standort Koblenz."},
]

CAREERS_HTML = """<html><body>
<nav><a href="/">Startseite</a><a href="/karriere">Karriere</a></nav>
<h1>Offene Stellen</h1>
<p>Wir bieten: Obstkorb, Jobrad, flexible Arbeitszeiten</p>
<div class="job"><a href="/jobs/101">Werkstudent IT-Support (m/w/d)</a><span>Darmstadt</span><span>Teilzeit</span></div>
<div class="job"><a href="/jobs/102">Junior Data Engineer (m/w/d)</a><span>Frankfurt am Main</span><span>Vollzeit</span></div>
<div class="job"><a href="/jobs/103">Praktikum Marketing (m/w/d)</a><span>Darmstadt</span></div>
<a href="/jobs">Alle Jobs anzeigen</a>
<footer><a href="/impressum">Impressum</a></footer>
</body></html>"""

last = {}  # the HTTP answer of the last request, caught on its way to the app's code
_post = httpx.post


def _catch(*args, **kwargs):
    start = time.perf_counter()
    r = _post(*args, **kwargs)
    last.update(response=r, seconds=time.perf_counter() - start)
    return r


httpx.post = _catch


def run(title: str, prompt: str, schema: dict, temperature: float) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(f"prompt: {len(prompt):,} characters")
    error = parsed = None
    try:
        parsed = ai._ask_openai_style("Cerebras", prompt, schema, temperature, {"tokens": 0, "neurons": 0.0})
    except Exception as e:
        error = e
    r = last.get("response")
    if r is None:
        print(f"no answer at all: {error!r}")
        return
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    print(f"HTTP {r.status_code}, took {last['seconds']:.2f} s (whole round trip)")
    if data.get("time_info"):
        print(f"Cerebras' own timing: {json.dumps(data['time_info'])}")
    print(f"tokens: {json.dumps(data.get('usage'))}")
    limits = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    if limits:
        print("limits Cerebras reports:")
        for k, v in sorted(limits.items()):
            print(f"  {k}: {v}")
    choice = (data.get("choices") or [{}])[0]
    print(f"finish_reason: {choice.get('finish_reason')}")
    print("--- raw answer ---")
    print((choice.get("message") or {}).get("content") or r.text[:2000])
    print("--- what the app would make of it ---")
    print(f"ERROR: {error!r}" if error else json.dumps(parsed, ensure_ascii=False, indent=2))


def main() -> None:
    if not os.environ.get("CEREBRAS_API_KEY"):
        sys.exit("CEREBRAS_API_KEY is not set in .env")
    print(f"model: {ai.model_of('Cerebras')}  url: {ai.OPENAI_STYLE_URLS['Cerebras']}")

    run("1) Score two jobs (ai.INSTRUCTIONS, as Gemini/Groq/Cloudflare/OpenRouter get it)",
        ai._score_prompt(JOBS), ai.RESPONSE_SCHEMA, 0.2)

    part = company_jobs.page_parts(CAREERS_HTML, "https://www.example.com/karriere")["short"]
    run("2) Read a careers page job list (company_jobs.EXTRACT_PROMPT)",
        company_jobs.EXTRACT_PROMPT.format(company="Beispiel Software GmbH", part=part),
        company_jobs.EXTRACT_SCHEMA, 0.1)


if __name__ == "__main__":
    main()
