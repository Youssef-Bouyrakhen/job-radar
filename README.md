# Job Radar

A personal job-alert tool for Werkstudent IT/software jobs in Darmstadt and Frankfurt.
It watches career pages and job-search links, finds new postings, filters them and
(in later phases) sends good ones to Telegram.

Everything runs on your own laptop. Your data stays in `data/jobradar.db`.

---

## 1. One-time setup (Windows)

You only do this once.

1. **Check that Python is installed.** Open PowerShell (press the Windows key, type
   `PowerShell`, press Enter) and type:

   ```
   py --version
   ```

   You should see something like `Python 3.12.2`. If you get an error, install Python
   from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"** in the installer.

2. **Run the setup.** Open the `job-radar` folder in File Explorer and **double-click
   `setup.bat`**. A black window opens and installs everything (about 3–5 minutes,
   it downloads a hidden Chrome browser of ~120 MB). When it says
   *"Setup finished!"*, press any key to close it.

   <details><summary>Prefer typing commands? Click here.</summary>

   In PowerShell, inside the `job-radar` folder:

   ```
   py -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m playwright install chromium
   copy .env.example .env
   ```
   </details>

## 2. Start Job Radar

**Double-click `start.bat`.** A black window opens and, after 2 seconds, your
browser shows the dashboard at **http://127.0.0.1:8000**.

- Keep the black window open while you use Job Radar.
- To stop it: click into the black window and press **Ctrl + C** (or just close it).

Command version: `.venv\Scripts\python run.py`

## 3. Using it

Paste a link into the bar at the top and click **Add & test**. Job Radar checks it once:

- 🟢 **Green** = it could read the page and found job postings. Click
  *"Show jobs found in the test"* to see what it found.
- 🔴 **Red** = blocked, empty, or no job postings found. The short reason tells you why.

**Which links work best?**

| Link type | Example | Works? |
|---|---|---|
| Personio, Workday, Greenhouse, Lever, SmartRecruiters, Recruitee job boards | `https://firma.jobs.personio.de` | ✅ Best: read via their official feed |
| StudySmarter CareerKit job search | `https://partner.studysmarter.de/careerkit/jobs?q=Werkstudent+IT&city=Frankfurt+am+Main` | ✅ Read via its data feed |
| Company career page that embeds one of the above | `https://firma.de/karriere` | ✅ Usually |
| Company career page with its own job list | | ⚠️ Often, depends on the page |
| Job search sites (Indeed, StepStone, LinkedIn) | | ⚠️ Often blocked. Use their job-alert emails instead (see below) |

Tip: on Workday, SmartRecruiters etc. you can include a search in the link, e.g.
`https://xyz.wd3.myworkdayjobs.com/Careers?q=Werkstudent`. Job Radar passes it on to the feed.

Job Radar is polite: it waits between requests to the same site, follows each site's
`robots.txt` rules, and never tries to get around logins or CAPTCHAs.

### Automatic checks

While Job Radar is running, it checks every active link once per interval (60 min by
default, see `config.yaml`). New jobs are added to the **Jobs** tab. A job counts as
"already known" if it has the same link, or the same title + company, as a saved job,
so the same ad found on two sites shows up only once.

The black window shows a line for every check, e.g. `14:05  Checked stepstone.de: green, 2 new job(s)`.

The **Watched links** tab has the "Add & test" bar and all your links. Each link shows a
dot (filled = trackable, empty = not trackable) and a switch (on = checked automatically,
off = paused). Click a link to see its details and the **Check now** and **Delete** buttons.
Deleting a link keeps the jobs it found.

### Built-in source: Bundesagentur für Arbeit

The official job search of the Arbeitsagentur is always included (you'll see it at the
top of your links). To change what it searches for, edit the `arbeitsagentur:` part of
`config.yaml` (search words, places, radius). To turn it off, set `enabled: false`.

### Built-in source: job-alert emails (Indeed, StepStone, LinkedIn)

Indeed, StepStone and LinkedIn often block programs like Job Radar, but they happily
send you job alerts by email. Job Radar reads those emails from **one Gmail label** and
treats the jobs in them like any other jobs (duplicates, keyword filter, Gemini score,
dashboard, Telegram).

**What Job Radar does with your Gmail**: it opens *only* the label `JobRadar`, and
only read-only. It never reads other emails, never marks emails as read, and never
moves, labels or deletes anything. Each email is read once (it remembers which).

One-time setup:

1. **Job alerts**: on Indeed, StepStone and LinkedIn, create job alerts
   (e.g. "Werkstudent IT", Darmstadt / Frankfurt) that are sent to your Gmail address.
2. **Label + filter** (in Gmail on a computer):
   - In the search bar at the top, paste:
     `from:(indeed.com OR stepstone.de OR linkedin.com) (job OR jobs OR stellen OR jobagent)`
   - Click the "show search options" icon at the right end of the search bar, then
     **Create filter**.
   - Tick **Apply the label** > **New label...** > type `JobRadar` (exactly like this) > Create.
   - Also tick **Also apply filter to matching conversations**, then **Create filter**.
   - Optional: tick **Skip the Inbox** too, if you don't want these emails in your inbox.
3. **Show the label to Job Radar**: Gmail > gear icon > **See all settings** > **Labels**.
   Find `JobRadar` and make sure **Show in IMAP** is ticked.
4. **App password** (Job Radar never sees your normal password):
   - You need 2-Step Verification: https://myaccount.google.com/security
   - Go to https://myaccount.google.com/apppasswords, type the name `Job Radar`, click
     **Create** and copy the 16-letter password.
5. Open `.env` with Notepad and fill in:
   ```
   GMAIL_ADDRESS=yourname@gmail.com
   GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
   ```
6. Restart Job Radar. **Job-alert emails (Gmail)** appears at the top of your links.
   Open it and click **Check now**: it should turn green and say e.g.
   "Found 25 job postings in 6 new emails".

The first check reads the alert emails from the last 14 days. Like any newly added
link, those first jobs are only shown in the dashboard, not sent to Telegram. After
that, jobs from new alert emails are sent as usual. Settings (label name, days) are in
the `emails:` part of `config.yaml`; to turn it off, set `enabled: false`.

### The job list

Each job is one row: **score**, title, company · city, and its age.

- **Score colour**: green = 8–10, amber = 5–7, grey = 1–4. "…" = Gemini is still scoring.
- **Small green dot** before the title: new, you haven't opened it yet.
- **"2 d ago"**: posted 2 days ago (date from the job site). **"seen 2 d ago"**: the site shows
  no posting date, so this is when Job Radar found it.
- **Click a row** to open it: Gemini's reason, hours, source, the exact date, and the buttons
  **Open ad**, **Save**, **Applied**, **Hide** and **Copy for Anschreiben**. The last one copies
  title, company, link and the full job description, ready to paste into a cover-letter helper.

Above the list you can switch between **Best new** (the default: new jobs scoring 7+, best
first), **All**, **Saved**, **Applied**, **Low score** and **Filtered out**. Hidden jobs
go to *Filtered out*, where you can open them and press **Unhide**.

The header shows the time of the last check and a small dot for Telegram (filled = alerts on).
All settings are on the **Settings** page.

### 🔥 Must apply

The first view in the sidebar (and the one the page opens with) shows only the jobs worth applying to
**today**. A job goes in only if ALL of these are true:

1. **Werkstudent** / working student / studentische Hilfskraft, at most 20 h/week if the ad says.
2. **AI score 8** or higher.
3. **Posted in the last 3 days** (no posting date: when Job Radar first saw it).
4. **Within 40 km of Darmstadt**, or **fully** remote (places are looked up once on OpenStreetMap and
   remembered).

**Your area** (also for Telegram):
- **Fully remote** counts: "100% remote", "remote only", "Homeoffice deutschlandweit", or a place that is
  only "Remote" / "Bundesweit". Hybrid, "remote options", "bis zu 100% Homeoffice möglich" or "remote within
  the EU" as a benefit don't: then the office city counts (ATOSS: Hamburg → outside your area).
- **The place** comes from the job list, then the job's own page: its structured data (also Greenhouse /
  Personio pages), a "Standort / Location" line, its meta tags, or the job system's data in the page
  (Avelios: "Standort Munich" → outside your area).
- **Location unknown**: shown in the list, but **no Telegram message** (it's sent later if its place turns
  out to be in your area). Outside your area: never sent.
5. **No blockers**: the AI checks "2+ years experience", staffing agency and "Master students only" in the
   same request that scores the job.
6. At least one **low-competition sign**: found on the company's own careers page / applicant system,
   a small or mid-size company, the company posted other Werkstudent jobs before, or its category is
   Insurance, Banks/Finance, Cloud/Data centers/Networks or Cybersecurity.

Each job shows the conditions it met (✓). Telegram sends **🔥 MUST APPLY** right away and one reminder
after 24 h if you haven't marked it Applied or hidden it. A job stays in the box until you mark it
**Applied** or **hide** it; after 3 days it turns **red**. A recent good job that isn't in the box shows
why when you open it ("Not in Must apply: no low-competition sign"). All numbers are under `must_apply:`
in config.yaml.

### The keyword filter

Every job is checked against the keyword lists in `config.yaml` (section `filter:`).
A job is shown only if it is:

1. a **student / part-time job**: Werkstudent, Working Student, Teilzeit, ... or at most 25 h/week,
2. **not full-time**: "Vollzeit", "Full-time", "40 Stunden" drop a job, *unless* it also says
   Werkstudent / Teilzeit,
3. **not** a senior role, apprenticeship (Ausbildung), internship or thesis
   ("Praktikant / Werkstudent" is kept),
4. **IT-related**: an IT keyword in the title (IT, Software, Informatik, Data, Cloud, ...).

Dropped jobs are still saved. Click **Filtered out** in the job list to see them, each with the
reason. If a good job lands there, add a keyword to `config.yaml` and restart Job Radar. The
filter then runs again over **all** saved jobs.

The `config.yaml` file explains the keyword syntax (`Software*`, `*entwickler`, ...).

### Gemini AI scoring

Jobs that pass the keyword filter are sent to Google Gemini, which gives each one a
**score from 1 to 10** and a one-sentence reason. You see these on every job card:

- **Green score (7–10)**: shown in your job list.
- **Below 7**: moved to **Low score**, so you can still look at them.
- **Blinking "…"**: Gemini hasn't scored this job yet. The dashboard updates by itself.

Gemini scores **low** for non-IT jobs (e.g. mechanical engineering / Konstruktion, tax,
HR), for **sales** roles (even when they sell IT products), and for jobs that are
**really full-time**. When a job board labels a Werkstudent job "Vollzeit", Job Radar first
loads the **full job ad**, and Gemini decides from the text whether it's really full-time.

**Setup:** you need a free Gemini API key.
1. Go to https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key** and copy it.
3. Open `.env` with Notepad and paste it after `GEMINI_API_KEY=`. Save and restart Job Radar.

Without a key, this step is skipped and every job that passes the keyword filter is shown.

**Settings** (`config.yaml`, section `gemini:`): `min_score` (default 7), the model, and
`profile`, a short description of you that Gemini scores every job against. Edit it if
your situation changes (e.g. other cities, other fields).

The free tier has a daily limit. If Job Radar hits it, scoring pauses for 10 minutes and
then continues. Nothing is lost.

### Telegram messages

For every **good new job** (Gemini score of at least 7, or every keyword match if Gemini
is off), Job Radar sends you a Telegram message with title, company, place, hours,
score + reason, posting date (or "first seen") and a link. Messages are sent right after
Gemini has scored the job.

Not sent (you still see them in the dashboard):
- jobs from the **first check of a link you just added**, which would be a flood of old jobs,
- jobs that were already in Job Radar before Telegram was set up.

**Setup** (once):
1. In Telegram, open **@BotFather**, send `/newbot` and follow the steps. Copy the **token**.
2. Open your new bot and press **Start** (otherwise it can't message you).
3. Find your **chat id**: open **@userinfobot** and press Start. It replies with your id.
4. Put both into `.env`: `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_CHAT_ID=...`. Restart Job Radar.
5. Dashboard → **Settings** → **Send test message**.

### Duplicates

The same job often appears on several sites with slightly different titles or company
names (e.g. "H World International Germany" vs. "H World International - Team Center").
Job Radar treats a similar title at a similar company as the same job and keeps only the
first copy. Different cities stay separate jobs.

### Companies (Company Discovery Engine)

The **Companies** page builds a list of employers around Darmstadt and Frankfurt (25 km) and
finds their careers pages. Click **Run discovery**; the page shows live progress.

1. **Collect** company names from:
   - the Arbeitsagentur: *all* IT job ads (any type, full-time too) in its 4 IT job fields,
     plus keyword searches per category (see **Categories** below),
   - OpenStreetMap: offices tagged IT, telecommunication or company (with their website tag),
   - your own data: jobs, email alerts, suggestions and watched links.

   Spelling variants are merged ("ACME GmbH" = "Acme", branches like "Niederlassung
   Darmstadt" are dropped). **Found via** shows where each company came from and how often.
2. **Website**: the OpenStreetMap tag first, otherwise the AI's answer. A website only counts
   if it loads and belongs to the company (its name is in the address or on the page).
3. **Careers page**: links on the homepage (Karriere, Jobs, Stellen, Careers, Join us), then the
   sitemap, then the usual addresses (/karriere, /jobs, /career). Embedded applicant systems
   (Personio, Join, Softgarden, Recruitee, d.vinci, rexx, onlyfy, SmartRecruiters, Greenhouse,
   Lever, Workday, SuccessFactors) are recognised and their direct job list is saved.

Colours: **green** = direct job list of an applicant system, **amber** = careers page,
**red** = nothing found (hover for the reason). Each run checks the next 150 companies
(IT employers first, staffing agencies and non-IT offices last), so a big list takes a few runs.

**Polite crawling**: Job Radar identifies itself as `JobRadar-CompanyDiscovery`, respects
robots.txt, loads at most 1 page per second per site, never logs in and never gets around bot
protection (such sites stay red). Only links that actually loaded are saved.

Status (your choice per company): New, Watched, Initiativ, No careers page, Skipped. Your choice
is never overwritten by a later run.

**Phase B: jobs, fit score, auto-add, Initiativ list.** After the careers pages, each run also:

1. **Reads the jobs** of up to 120 companies (each again after 20 hours): the applicant system's
   feed if there is one, else the JobPosting data in the page, else the AI reads the job list from
   the page (only the job-related lines are sent, max 30 pages per run). Only jobs in the
   Darmstadt / Frankfurt area (or remote, or without a place) are saved. They go through the normal
   flow: keyword filter, duplicates, AI score, Telegram. The first time a company is read, its
   jobs are not sent to Telegram (a flood of old jobs), except jobs posted in the last 7 days.
2. **Fit score** (0–100, hover the number for the reasons): small company 20 / mid-size 15 /
   big 0 (the AI says how big a company is; staffing agencies lose 20), distance up to 20, share of
   IT jobs up to 20, Werkstudent jobs now or before **30**, fresh jobs 10. The list is sorted by it.
3. **Auto-add**: the best companies with a good job (a Werkstudent / part-time job that passes your
   filter) become watched links, max 10 per run, marked **Auto-added**. Only links the hourly
   check can read are added.
4. **Initiativ list**: companies with IT jobs but no Werkstudent job (now or before) get status
   Initiativ and a one-line AI reason why an Initiativbewerbung could fit you. If one later posts a
   Werkstudent job, it moves back to New. Statuses you set yourself are never changed.

Discovery also runs by itself **once a day at 03:00** (German time; set `daily_run_hour: null` in config.yaml to turn it off).

**Auto-resume** (`auto_resume: true`): you don't need to start runs by hand. Every 5 minutes, if no run is
going and there is still work (companies never checked, or careers pages whose job list was never read), a
run starts by itself and works through the queue (the same steps as the night run, without collecting the
sources again). After a restart or deploy it goes on by itself within 5 minutes, where it stopped.
- **No endless loops**: a run that got nowhere isn't repeated. When the work left needs the AI and all free
  AI requests are used up, it waits until an AI is available again; when the web search is used up, until
  midnight; otherwise 1, 2, 4 … max 24 hours. New work (e.g. from the night run) starts it again.
- **Never at the same time as 03:00 / 19:00**: no start in the 30 minutes before, it stops starting new
  companies 15 minutes before, and it waits while the 19:00 read is going. The 03:00 run waits for a run
  that is still finishing instead of being skipped. Same polite crawler and web search limits as always.
- **All done**: it does nothing until new work appears.
- The **Coverage** page shows it, e.g. "Auto-resume: running", "waiting for AI until 02:00 tomorrow" or
  "all done".

Each company shows **N good**: its Werkstudent / part-time jobs that pass your keyword filter and are
in the area (Darmstadt / Frankfurt, remote, or nationwide like "Bundesweit"). Hover it to see why the
others don't count (e.g. "1 Werkstudent job filtered out (Not IT-related) · 4 outside the area (Dortmund)").

A careers page only counts if it talks about jobs (Stellenangebote, Jobs, Karriere, Bewerbung, open
positions) or has an applicant system; blog, magazine, news and press pages never count. If
OpenStreetMap is busy, Job Radar tries 3 times; if it still fails, the companies from earlier runs stay.

In the night run a company's jobs are read **at most once a week** (`recheck_after_days: 7`, it can't
go lower), and its website / careers page is looked for only once. **Watched** and **Initiativ** companies
are read **every day at 19:00** (`watched_read_hour`); the AI only reads a page again when its job part
changed. The Companies page shows for each company when it was last read and when it is read next.

**Import list** (Companies page): paste company names (one per line), or the link of a page that
lists companies, e.g. a career fair's exhibitor list, tech hub members, h_da dual-study partners
(web pages and PDFs work). Give the list a name like `konaktiva 2026`: it shows as the source of
those companies, and you can type it into the search box to see only them. A clean list of names
needs no AI; a web page or PDF uses 1–5 AI requests. The companies then go through the same steps
as above (website, careers page, jobs, fit score), first in line. Some sites (konaktiva, for
example) block automated visitors: then open the page yourself, copy the names and paste them.

**Find look-alikes**: one AI request suggests small companies near Darmstadt / Frankfurt that are
similar to the companies whose jobs you **saved or applied to**. They show "like …" under their
name and go through the same steps. This also happens by itself once a week during the night run.

Companies you **skipped** are never added back by an import, a look-alike search or "Find new sources".

**Weekly summary**: every **Sunday at 19:00** one short Telegram message: new companies, careers
pages found, good jobs, new Initiativ candidates and the top 3 companies by fit score (last 7 days,
no AI). Settings has a **Send weekly summary** button to see it now. Change the time under
`weekly_summary:` in config.yaml (`day: null` turns it off).

**Banks and insurers**: each run reads the Bundesbank's official bank code file (Bankleitzahlendatei,
free download) and adds every bank with its head office in postcodes 60000–65999 (about 165 banks).
The BaFin company search can't be read automatically (its robots.txt forbids all bots), so insurers come
in by hand: open the [BaFin company search](https://portal.mvp.bafin.de/database/InstInfo/), choose
"Versicherungsunternehmen und Pensionsfonds", copy the result list (names with addresses) and paste it into
**Import list** with a name like `BaFin Versicherer`. A pasted list with addresses keeps only postcodes
60000–65999 (`import_postcodes`) and leaves out branch offices (Niederlassung, Repräsentanz).
Companies in **Insurance, Banks/Finance or Cloud/Data centers/Networks** get **+10** fit points.

**Categories** (industry method): every company gets one of **Insurance**, **Banks/Finance**,
**Cloud/Data centers/Networks**, **Cybersecurity**, **IT services/Software** or **Other**. The row
of category buttons under the status boxes filters the list; the small second dropdown on each
company shows its category (hover it to see who decided) and lets you correct it. Your choice is
never changed.

- **Rules first, no AI**: the name and website ("R+V Versicherung", "secunet", "Sparkasse",
  "Rechenzentrum"), then the job titles (an IT company whose jobs are mostly security jobs is
  Cybersecurity). An IT office on OpenStreetMap without other clues counts as IT services/Software.
  Staffing agencies and guard companies ("X Security GmbH") are Other.
- **The AI only for unclear ones**: 80 companies per request, at most 6 requests a day
  (`max_category_ai_per_day`), companies with a careers page first. Existing companies are sorted
  over the first few nights; until then they show "Not sorted yet".
- **More companies per category**: each run also searches the Arbeitsagentur (no AI) within 25 km of
  Darmstadt and Frankfurt with keywords per category (`category_searches:` in config.yaml), e.g.
  "Versicherung IT", "Bank Daten", "Rechenzentrum", "Pentest", "Softwareentwicklung". Only IT job ads
  count (a "SOC" or "Security" search also finds guard jobs; those are dropped). They show as
  **AA search: …** under Found via, and new employers go through the normal steps (website, careers
  page, jobs, fit score).

### Coverage (region coverage)

The **Coverage** page shows how much of a town is covered: not only IT employers, but every business
that could hire an IT Werkstudent. The towns: **Darmstadt** (all districts), **Weiterstadt**, **Griesheim**,
**Pfungstadt**, **Langen**, **Egelsbach**, **Dreieich** and **Neu-Isenburg**, then **Frankfurt Airport**
(see below), then **Offenbach am Main**, **Mörfelden-Walldorf**, **Kelsterbach**, **Rüsselsheim am Main**,
**Mühltal**, **Roßdorf**, **Ober-Ramstadt**, **Seeheim-Jugenheim**, **Erzhausen** and last **Mainz** (the
biggest). The nightly checks go through them in this order. On top: **Region covered: X%**
(companies with a final status) and a **Whole region** row. Per town: companies, careers pages, checked %,
how many nights are left, and one button per status; a click opens the Companies page with only that
town's companies in that status.

Where the companies come from (all automatic, in the nightly run, no AI):
- **OpenStreetMap**: every named office inside the city boundary (any business type, not only IT), works,
  research institutes, universities, town halls and hospitals. Shops, restaurants, cafés, doctors and
  craftsmen aren't offices, so they don't come in; lawyers, tax advisors, insurance agents, clubs and
  fraternities are left out too. Units of one employer (TU institutes, city offices) become one company.
- **Arbeitsagentur**: every employer with a job ad in the town's postcodes, any job type (staffing
  agencies are left out by the API).
- **OffeneRegister.de**: the company register as open data. Its only download is from **2019**
  (260 MB, the query service is offline), so it is read **once** (2–5 minutes) and a small extract per town is kept
  in `data/` (e.g. `offeneregister-Darmstadt.json`; a new town gets its extract the next time the download
  works). IT-sounding companies (Software, IT, Digital, Data, ...) are added; the others only mark known
  companies as being in the town. Holdings and management companies are left out.
  Turn it off with `offeneregister: false`.

Duplicates merge with the companies already known (same name or same website). Then:
1. **Rules first**: restaurants, bakeries, hairdressers, care services, craftsmen, shops and staffing
   agencies are **Skipped** right away (the reason shows under the status), also companies whose only
   ads are for non-office jobs (cook, driver, nurse, ...).
2. **The normal pipeline** for the rest, a few per night (`max_checks_per_run: 300` for all towns together,
   on top of the usual 150; one town after the other, in the order above, Darmstadt first): website (the OpenStreetMap tag, then a guess from the name, e.g. `acme-software.de`, which only
   counts if the page names the town or one of its postcodes; the AI last), careers page, jobs, fit score.
3. **Final status** for every town company:
   - **Watched**: has a careers page; it's read every day at 19:00 and good jobs come to you (with good jobs it
     also becomes a watched link).
   - **Initiativ**: IT jobs but no student job (as before), or a **small IT company without a careers
     page**: Job Radar reads its **Impressum** and shows the contact email (`max_contacts_per_run: 60`).
   - **No careers page**: none found.
   - **Skipped**: by the rules above, or only in the 2019 register with no website (probably closed).
   A status you choose yourself is never changed.

Everything goes through the polite crawler (robots.txt, max 1 request/s per site, honest User-Agent).
The AI is only used in the usual places (websites the rules can't find, job lists without a feed,
categories, sizes) and always after scoring jobs (see **AI limits**). More towns can be added under
`region: towns:` in config.yaml (the official municipality key `ags` and the postcodes; optional `names`:
other spellings like "Offenbach", and `not_place`: places that only start like the town, e.g. Mainz-Kastel,
which is Wiesbaden).
When the free web search (step 5 of the careers page search) is used up for the day, the companies that
still need it stay unchecked and the next night goes on with them.

**Frankfurt Airport** is its own row (after the first 8 towns, also in **Whole region**). It is an area, not a town:
- **OpenStreetMap**: the Frankfurt district "Flughafen" (Terminals, Cargo City Nord / Süd, Gateway Gardens,
  The Squaire) and the airport grounds, not all of Frankfurt. Also the operators of hangars and other
  buildings ("Flugzeughalle 5" belongs to Lufthansa Technik AG).
- **Arbeitsagentur**: every employer with ads in postcode **60549** (the airport's postcode; 60546 / 60547
  are the big-customer postcodes of Lufthansa and Fraport and count too). No staffing agencies.
- **Fraport's lists**: the **Airlines A–Z** list of frankfurt-airport.com (each airline's website is read once
  from its page there and kept in `data/fraport-airlines.json`) and the companies of **Gateway Gardens**
  (from its leaflet, October 2024). Cargo City and The Squaire publish no list of their companies.
- **OffeneRegister.de**: companies with an address in these postcodes, as soon as the download works again.
- **Stronger rules**: shops, duty-free, restaurants, cafés, caterers, parking, gates, check-in and service
  counters, car rentals, ground handling, cleaning and security staffing are Skipped right away.
  Companies listed under `region: never_skip:` (Euler Hermes, Alstom) are never Skipped by a rule.
- **Duplicates** only merge with the same name or the same **website**: "Lufthansa", "Lufthansa Cargo" and
  "Lufthansa Technik" stay separate when each has its own website; "Fraport AG Zentrale" and "Fraport AG"
  (same website) become one (the other is Skipped with "Same website as ...").

## 4. Settings

- `config.yaml`: check interval, Arbeitsagentur searches, Gmail label, delays, browser fallback. Restart after editing.
  Section `companies:` sets the radius, the two places, how many companies one run checks and the delay per site.
- `config.local.yaml` (create it yourself, it is not in git): your personal values, e.g. your
  `gemini: profile:` and Gmail label. Everything in it overrides `config.yaml`.
- `deploy.local.bat` (not in git): one line `set SERVER=root@your-server-ip` for `deploy.bat`.
- `.env`: your secret keys (Telegram, Gemini, Gmail). Open it with Notepad.
  Never share this file.

## 5. Troubleshooting

- **"py is not recognized"**: Python isn't installed or isn't on PATH, see step 1.
- **Dashboard doesn't open**: make sure the black window is still open, then go to
  http://127.0.0.1:8000 manually.
- **"Job Radar ... is already running"**: another Job Radar window is still open. Close it
  (Ctrl + C or the X), then start again. Only one copy can run at a time.
- **Red banner "Job Radar was updated, but the old version is still running"**: same fix:
  close the old black window, then start `start.bat` again.
- **Test message fails**: the dashboard shows Telegram's reason. "Unauthorized" = wrong
  token. "chat not found" = wrong chat id, or you haven't pressed Start in your bot yet.
- **Red "browser is not installed"**: run `setup.bat` again.
- **Job-alert emails red "label not found"**: the label must be called exactly `JobRadar`,
  and **Show in IMAP** must be ticked for it (Gmail settings > Labels).
- **Job-alert emails red "Gmail refused the login"**: `GMAIL_APP_PASSWORD` must be an app
  password (step 4 above), not your normal Gmail password. Also check `GMAIL_ADDRESS`.
- **"No module named ..." after an update**: run `setup.bat` again (it installs new packages
  and keeps your data and `.env`).

---

## Roadmap

- [x] **Phase 1**: Add links, instant green/red test, dashboard list
- [x] **Phase 2**: Scheduled checks, saving new jobs, de-duplication, "check now"
      button, plus the official **Bundesagentur für Arbeit Jobsuche API** as a built-in source.
      Also a first job list with **posting dates** ("posted 2 days ago", or a clearly marked
      "first seen" date when the source has none) and **NEW / fresh highlighting**
      (green NEW badge until you open the job, "fresh" badge if posted in the last 24 h,
      "show only new" filter, "5 new jobs" counter)
- [x] **Phase 3**: Keyword filter, including **part-time only** (rejects Vollzeit/full-time
      unless it also says Werkstudent/Teilzeit) and hours detection
- [x] **Phase 4**: Gemini AI scoring (knows you can work max. 20 h/week)
- [x] **Phase 5**: Telegram messages (incl. posting date) + test button
- [x] **Phase 6**: Job-alert **emails** (Indeed, StepStone, LinkedIn) read from one Gmail
      label (read-only), incl. how to create a Gmail app password
- [x] **Phase 7**: Full dashboard (your own design): job history, search, views (Best new, Saved,
      Applied, Low score, Filtered out), source boxes (LinkedIn / Indeed / StepStone / Arbeitsagentur / company sites, v8.2), time dropdown, sortable Posted column,
      light and dark mode
- [x] **Phase 8**: **Find new sources** (Watched links page): Gemini suggests IT employers around
      Darmstadt / Frankfurt, also companies that show up often in your alerts and Arbeitsagentur jobs.
      Job Radar finds and tests each careers page (applicant systems like Personio / Workday first),
      drops links that don't load, and shows good jobs per company. Add or Skip; skipped companies
      never come back. Takes 2–5 minutes and one Gemini request.
- [x] **Backup AI**: when Gemini hits its limit, scoring switches automatically to **Groq**, then
      **OpenRouter** (free models only). Only providers with a key in `.env` (`GROQ_API_KEY`,
      `OPENROUTER_API_KEY`) are used; Settings shows which one is active. 10 jobs per request.
- [x] **AI limits (v9.1)**: 100% free. Gemini → Groq → OpenRouter (free models). GitHub Models is
      built in but off (GitHub retired it on 2026-07-30); Mistral / Cerebras are off. Rules first, AI last (company sizes from
      job ad counts, Initiativ reasons only for the top 10). Jobs that can't be scored wait and are
      scored as soon as any provider is free. The **AI usage bar** at the top of every page shows
      used / estimated limit per provider, the active one (green), empty ones (red), the reset time
      and how many jobs wait for a score.
- [x] **Honest AI limits (v10.8)**: Groq and OpenRouter report their real requests left, Gemini its real
      daily limit when it is reached. After a "daily limit reached" answer a provider stays red until its
      own real reset ("resets at 09:00": Gemini at midnight US Pacific time, Cloudflare / OpenRouter at
      00:00 UTC, Groq bit by bit) - also after a restart. Refused requests no longer count as used.
- [x] **German time everywhere (v10.9)**: dashboard, AI reset times, Telegram, logs and the 03:00 / 19:00
      runs use Europe/Berlin with summer / winter time, also when the PC's time zone or clock is wrong.
- [x] **Company Discovery Engine, Phase A** (v8.3): **Companies** page. Collects employers from the
      Arbeitsagentur (all IT jobs), OpenStreetMap and your own data, merges duplicates, finds and
      checks websites and careers pages, detects 12 applicant systems. Polite crawler (robots.txt,
      1 request/s per site, honest User-Agent).
- [x] **Company Discovery Engine, Phase B** (v8.4): reads each company's jobs (feed, structured data or
      AI), runs them through filter / duplicates / scoring / Telegram, fit score, auto-adds up to 10
      watched links per run, Initiativ list with an AI reason, daily automatic run.
- [x] **Company Discovery Engine, Phase C** (v8.5): weekly Telegram summary (Sunday 19:00), **Import list**
      (names, web page or PDF, marked with your label, e.g. "konaktiva 2026"), **look-alikes** of the companies
      you saved / applied to, jobs re-read at most once a week, skipped companies never come back.
- [x] **v8.6**: "good jobs" counts nationwide jobs ("Bundesweit"), per-company good count with reasons,
      stricter careers-page check (old pages re-checked once), OpenStreetMap retries.
- [x] **v8.8**: **🔥 Must apply** view (6 conditions, Telegram + 24 h reminder, red after 3 days), banks from the
      Bundesbank bank code file, Import list keeps only postcodes 60000–65999 for lists with addresses
      (BaFin), fit score +10 for Insurance / Banks / Cloud.
- [x] **v8.7**: company **categories** (Insurance, Banks/Finance, Cloud/Data centers/Networks,
      Cybersecurity, IT services/Software, Other): rules first, the AI for unclear ones within a daily
      budget; Arbeitsagentur keyword searches per category.
- [x] **v9.3**: **Region coverage**, starting with the City of Darmstadt: every business that could hire
      an IT Werkstudent, from OpenStreetMap, the Arbeitsagentur (all employers) and OffeneRegister.de;
      rules skip shops / restaurants / craftsmen, the rest goes through the normal pipeline at night;
      every company ends Watched, Initiativ (Impressum email for small IT companies), No careers page or
      Skipped. New **Coverage** page with "Region covered: X%".
