# Job Radar

Finds Werkstudent / part-time IT jobs in the Darmstadt / Frankfurt region and sends the
good ones to Telegram. Sources: watched links (company careers pages, ATS feeds), the
Arbeitsagentur API and job-alert emails from one Gmail label. A keyword filter (layer 1)
and an AI score 1-10 (layer 2: Gemini, with Groq/OpenRouter/Mistral/Cerebras as backups)
decide what shows up. FastAPI backend + plain HTML/JS dashboard at http://127.0.0.1:8000.
The owner is a beginner: explain in plain language and give exact commands.

## Folder structure
- `run.py` - starts uvicorn and opens the dashboard. `start.bat` runs it, `setup.bat` installs.
- `app/main.py` - web server and the API the dashboard calls (`/api/info` returns the version)
- `app/__init__.py` - `VERSION`; bump it with every change you deploy
- `app/config.py` - loads `config.yaml`, then `config.local.yaml` on top (personal, not in git)
- `app/db.py` - SQLite, one file: `data/jobradar.db` (`JOBRADAR_DB` env var overrides the path)
- `app/scheduler.py` - background checks (APScheduler)
- `app/checker.py` - checks links, saves new jobs; `app/sources.py` - is a link trackable
- `app/fetcher.py` - polite downloads; `app/render.py` - hidden Chrome for JS pages
- `app/extractors/` - `ats.py` (Personio, Workday, ... feeds), `generic.py` (HTML career
  pages), `alerts.py` (Indeed / StepStone / LinkedIn alert emails)
- `app/arbeitsagentur.py`, `app/gmail.py` - built-in sources
- `app/filters.py` - layer 1 keyword filter; `app/ai.py` - layer 2 AI scoring + daily budget
- `app/details.py` - full job description; `app/dedup.py` - same job from two sources
- `app/must_apply.py` - the "Must apply" list; `app/telegram.py`, `app/weekly.py` - messages
- `app/companies.py`, `careers.py`, `company_jobs.py`, `company_import.py`, `categories.py`,
  `discover.py` - Company Discovery Engine (find companies, careers pages, rank them)
- `static/` - dashboard (`index.html`, `app.js`, `style.css`); `static/old-v6/` is old,
  `job-radar-v2.html` is the design mockup. Neither is used by the app.

## Secrets and personal files (never commit, never print)
- `.env` - API keys, Telegram, Gmail. `.env.example` lists every key name with NO values.
- `config.local.yaml` - personal profile; `deploy.local.bat` - server address.
- `data/` - database and backups.
- The repo is PUBLIC on GitHub: no keys, server IP, email or personal details in tracked
  files. Run `git status` and check the diff before every commit.

## How to test
There is no automated test suite. Before saying something works:
1. Syntax: `.venv\Scripts\python -m compileall -q app`
2. Run against a COPY of the database, with no background checks or Telegram messages:
   ```
   copy data\jobradar.db %TEMP%\jr-test.db
   set JOBRADAR_DB=%TEMP%\jr-test.db
   set JOBRADAR_NO_SCHEDULER=1
   .venv\Scripts\python run.py
   ```
   Then open http://127.0.0.1:8000 and check `/api/info` shows the new version.
3. Make sure you are testing the right process: an old Job Radar may still be running
   on port 8000 (run.py refuses to start then). Stop it first.
4. The laptop database is old. The real data is on the server; don't draw conclusions
   about real jobs or companies from the laptop copy.

## How to deploy
Double-click `deploy.bat` (needs `deploy.local.bat` with `set SERVER=root@<server-ip>`).
It runs over ssh:
1. backs up the server database to `data/backup-<date>.db` (and checks it)
2. copies only `app/`, `static/`, `requirements.txt`, `README.md`
3. installs packages as the `jobradar` user, restarts the `jobradar` systemd service
4. checks that `/api/info` on the server reports the same `VERSION` as this folder
The server half is `deploy_server.sh` (keep LF line endings, see `.gitattributes`).

## Server rules
- App lives in `/home/jobradar/job-radar`, runs as user `jobradar`, service `jobradar`.
- NEVER overwrite, delete or copy over `data/` (database + backups) or `.env` on the server.
- Never copy `config.yaml` or `config.local.yaml` to the server; it keeps its own.
- Always back up the database before changing anything on the server.
- Only deploy with `deploy.bat`; no manual file copies.
