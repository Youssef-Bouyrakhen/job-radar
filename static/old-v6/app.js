const $ = (sel) => document.querySelector(sel);

// Must match VERSION in app/__init__.py. If they differ, an old Job Radar is still running.
const DASHBOARD_VERSION = "6.0";

const state = {
  tab: load("tab", "jobs"),     // jobs | links | settings
  view: load("view", "best"),   // best | all | saved | applied | low | filtered
  open: new Set(),              // ids of expanded rows (jobs: "j12", links: "l3")
  busy: 0,                      // checks running -> no auto-refresh meanwhile
  info: { ai_enabled: false, min_score: 7 },
  jobs: new Map(),              // id -> job, for the buttons
};

// ---- helpers ------------------------------------------------------------------

function load(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode etc. */ }
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

let toastTimer;
function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// ---- dates ------------------------------------------------------------------

const isDayOnly = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s);

function parseDate(s) {
  if (isDayOnly(s)) {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d); // local midnight
  }
  return new Date(s);
}

function daysAgo(date) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(date); day.setHours(0, 0, 0, 0);
  return Math.max(0, Math.round((today - day) / 86400000));
}

const sameYear = (d) => d.getFullYear() === new Date().getFullYear();
const shortDate = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(!sameYear(d) && { year: "numeric" }) });
const longDate = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
const clock = (d) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

/** "5 h ago", "today", "2 d ago", "13 Oct" */
function relative(s) {
  const d = parseDate(s);
  if (!isDayOnly(s)) {
    const hours = Math.floor((Date.now() - d) / 3600000);
    if (hours < 1) return "just now";
    if (hours < 24) return `${hours} h ago`;
  }
  const days = daysAgo(d);
  if (days === 0) return "today";
  if (days < 30) return `${days} d ago`;
  return shortDate(d);
}

/** Collapsed row: posting date if known, otherwise "seen ..." (when Job Radar found it). */
function ageLabel(job) {
  return job.posted_at ? relative(job.posted_at) : `seen ${relative(job.first_seen)}`;
}

function timeAgo(iso) {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return plural(Math.round(hours / 24), "day") + " ago";
}

function whenLabel(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return daysAgo(d) === 0 ? clock(d) : `${shortDate(d)}, ${clock(d)}`;
}

// ---- jobs -------------------------------------------------------------------

function scoreChip(job) {
  if (job.ai_score == null) {
    const waiting = state.info.ai_enabled && job.filter_passed && job.ai_attempts < 3;
    return `<span class="score none" title="${waiting ? "Waiting for Gemini" : "No score"}">${waiting ? "…" : "–"}</span>`;
  }
  const level = job.ai_score >= 8 ? "high" : job.ai_score >= 5 ? "mid" : "low";
  return `<span class="score ${level}" title="Gemini score ${job.ai_score}/10">${job.ai_score}</span>`;
}

function isNew(job) {
  const lowScore = state.info.ai_enabled && job.ai_score != null && job.ai_score < state.info.min_score;
  return !job.seen && job.filter_passed && !lowScore && !job.status;
}

function jobDetails(job) {
  const facts = [];
  if (job.posted_at) {
    facts.push(`Posted ${longDate(parseDate(job.posted_at))}`);
  } else {
    facts.push(`First seen ${longDate(new Date(job.first_seen))} · the site shows no posting date`);
  }
  if (job.hours) facts.push(job.hours);
  if (job.employment) facts.push(job.employment);
  facts.push(`via ${job.source}`);

  let note = "";
  if (job.status === "hidden") note = "You hid this job.";
  else if (!job.filter_passed) note = `Keyword filter: ${job.filter_reason}`;
  else if (job.ai_full_time) note = "Gemini thinks this is really a full-time job.";

  const toggle = (status, label, onLabel) =>
    `<button class="secondary${job.status === status ? " on" : ""}" data-status="${status}">${job.status === status ? onLabel : label}</button>`;

  return `
    <div class="details">
      ${job.ai_reason ? `<p class="reason">${esc(job.ai_reason)}</p>` : ""}
      ${note ? `<p class="meta">${esc(note)}</p>` : ""}
      <p class="meta">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</p>
      <div class="actions">
        <a class="primary" href="${esc(job.url)}" target="_blank" rel="noopener" data-open>Open ad</a>
        ${toggle("saved", "Save", "Saved")}
        ${toggle("applied", "Applied", "Applied ✓")}
        ${toggle("hidden", "Hide", "Unhide")}
        <button class="secondary" data-copy>Copy for Anschreiben</button>
      </div>
    </div>`;
}

function renderJob(job) {
  const key = `j${job.id}`;
  const open = state.open.has(key);
  const row = document.createElement("article");
  row.className = `row${isNew(job) ? " is-new" : ""}${open ? " open" : ""}`;
  row.innerHTML = `
    <button class="row-head" aria-expanded="${open}">
      ${scoreChip(job)}
      <span class="main">
        <span class="title">${isNew(job) ? '<i class="dot" title="New"></i>' : ""}<span>${esc(job.title)}</span></span>
        <span class="sub">${esc([job.company, job.location].filter(Boolean).join(" · "))}<span class="sub-age"> · ${ageLabel(job)}</span></span>
      </span>
      <span class="age" title="${job.posted_at ? "Posted" : "First seen by Job Radar (no posting date)"}">${ageLabel(job)}</span>
    </button>
    ${open ? jobDetails(job) : ""}`;

  row.querySelector(".row-head").onclick = () => {
    state.open.has(key) ? state.open.delete(key) : state.open.add(key);
    if (!job.seen) markSeen(job);
    row.replaceWith(renderJob(job));
  };
  if (open) wireJobButtons(row, job);
  return row;
}

async function markSeen(job) {
  job.seen = 1;
  renderCounts(await api(`/api/jobs/${job.id}/seen`, { method: "POST", keepalive: true }));
}

function wireJobButtons(row, job) {
  row.querySelectorAll("[data-status]").forEach((btn) => {
    btn.onclick = async () => {
      const status = job.status === btn.dataset.status ? "" : btn.dataset.status;
      renderCounts(await api(`/api/jobs/${job.id}/status`, { method: "POST", body: JSON.stringify({ status }) }));
      job.status = status;
      job.seen = 1;
      toast({ saved: "Saved", applied: "Marked as applied", hidden: "Hidden. Find it under Filtered out", "": "Undone" }[status]);
      row.replaceWith(renderJob(job));
    };
  });
  row.querySelector("[data-copy]").onclick = () => copyForCoverLetter(job);
}

async function copyForCoverLetter(job) {
  const full = await api(`/api/jobs/${job.id}`);
  const lines = [
    `Stelle: ${full.title}`,
    `Unternehmen: ${full.company || "–"}`,
    `Ort: ${full.location || "–"}`,
  ];
  if (full.hours) lines.push(`Arbeitszeit: ${full.hours}`);
  lines.push(`Link: ${full.url}`);
  if (full.description) lines.push("", "Stellenbeschreibung:", full.description);
  const text = lines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(full.description ? "Copied title, company and job description" : "Copied (no description saved for this job)");
}

const EMPTY = {
  best: () => {
    const next = state.info.next_check ? clock(new Date(state.info.next_check)) : null;
    return next ? `All caught up. Next check at ${next}.` : "All caught up.";
  },
  all: () => "No jobs yet. Add a link under Watched links.",
  saved: () => "Nothing saved yet. Open a job and press Save.",
  applied: () => "No applications marked yet.",
  low: () => "No low-scored jobs.",
  filtered: () => "Nothing filtered out.",
};

async function loadJobs() {
  const jobs = await api(`/api/jobs?view=${state.view}`);
  state.jobs = new Map(jobs.map((j) => [j.id, j]));
  $("#jobs").replaceChildren(...jobs.map(renderJob));
  $("#jobs-empty").hidden = jobs.length > 0;
  $("#jobs-empty").textContent = EMPTY[state.view]();
  $("#mark-all").hidden = !["best", "all"].includes(state.view) || !state.info.best;
}

function renderCounts(counts) {
  Object.assign(state.info, counts);
  document.querySelectorAll("[data-count]").forEach((el) => {
    el.textContent = counts[el.dataset.count] ?? "";
  });
  document.title = counts.best ? `(${counts.best}) Job Radar` : "Job Radar";
}

// ---- watched links ------------------------------------------------------------

function linkDetails(link) {
  const builtin = link.kind !== "url";
  return `
    <div class="details">
      <p class="reason">${esc(link.reason || "Not checked yet")}</p>
      <p class="meta">
        <span>${esc(link.method || "–")}</span>
        <span>${plural(link.total_jobs, "job")} found in total</span>
        <span>${link.last_new_job_at ? `last new match ${timeAgo(link.last_new_job_at)}` : "no new matches yet"}</span>
      </p>
      <p class="meta url">${link.kind === "gmail" ? "Built-in source. Reads only your Gmail label from config.yaml, read-only (emails stay unread)" : builtin ? "Built-in source. Searches are set in config.yaml" : `<a href="${esc(link.url)}" target="_blank" rel="noopener">${esc(link.url)}</a>`}</p>
      <div class="actions">
        <button class="secondary" data-check>Check now</button>
        ${builtin ? "" : '<button class="secondary" data-delete>Delete</button>'}
      </div>
    </div>`;
}

function renderLink(link) {
  const key = `l${link.id}`;
  const open = state.open.has(key);
  const row = document.createElement("article");
  row.className = `row${link.active ? "" : " paused"}${open ? " open" : ""}`;
  const summary = link.status === "green"
    ? `${link.matching_jobs} matching · checked ${timeAgo(link.last_checked)}`
    : link.status === "red" ? "Not trackable" : "Not checked yet";
  row.innerHTML = `
    <div class="row-head" role="button" tabindex="0" aria-expanded="${open}">
      <span class="link-dot ${link.status}" title="${link.status === "green" ? "Trackable" : "Not trackable"}"></span>
      <span class="main">
        <span class="title"><span>${esc(link.name || link.url)}</span></span>
        <span class="sub">${esc(link.active ? summary : `Paused · ${summary}`)}</span>
      </span>
      <label class="switch" title="${link.active ? "Active: checked automatically" : "Paused"}">
        <input type="checkbox" ${link.active ? "checked" : ""} aria-label="Check automatically">
        <span class="slider"></span>
      </label>
    </div>
    ${open ? linkDetails(link) : ""}`;

  const head = row.querySelector(".row-head");
  const toggle = (e) => {
    if (e.target.closest(".switch")) return;
    state.open.has(key) ? state.open.delete(key) : state.open.add(key);
    row.replaceWith(renderLink(link));
  };
  head.onclick = toggle;
  head.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e); } };

  row.querySelector(".switch input").onchange = async (e) => {
    await api(`/api/links/${link.id}`, { method: "PATCH", body: JSON.stringify({ active: e.target.checked }) });
    toast(e.target.checked ? "Resumed" : "Paused. It won't be checked automatically");
    loadLinks();
  };

  if (open) {
    row.querySelector("[data-check]").onclick = (e) => checkNow(link, e.currentTarget);
    const del = row.querySelector("[data-delete]");
    if (del) {
      del.onclick = async () => {
        if (del.dataset.armed) {
          await api(`/api/links/${link.id}`, { method: "DELETE" });
          toast("Deleted. Its jobs stay in your list");
          loadLinks();
          return;
        }
        del.dataset.armed = "1";
        del.textContent = "Really delete?";
        setTimeout(() => { delete del.dataset.armed; del.textContent = "Delete"; }, 3000);
      };
    }
  }
  return row;
}

async function checkNow(link, btn) {
  btn.disabled = true;
  btn.textContent = "Checking…";
  state.busy++;
  try {
    const { link: updated, new_jobs, new_matching } = await api(`/api/links/${link.id}/check`, { method: "POST" });
    toast(updated.status === "green"
      ? `${updated.name}: ${plural(new_jobs, "new job")}, ${new_matching} matching`
      : `${updated.name || "Link"}: not trackable`);
  } catch (err) {
    toast(err.message);
  } finally {
    state.busy--;
    refresh();
  }
}

async function loadLinks() {
  const links = await api("/api/links");
  $("#links").replaceChildren(...links.map(renderLink));
  $("#links-empty").hidden = links.length > 0;
}

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#url-input").value.trim();
  if (!url) return;
  const btn = $("#add-btn");
  const note = $("#add-message");
  btn.disabled = true;
  btn.textContent = "Testing…";
  note.hidden = false;
  note.textContent = "Testing the link. This can take up to a minute.";
  state.busy++;
  try {
    const { link, new_matching } = await api("/api/links", { method: "POST", body: JSON.stringify({ url }) });
    $("#url-input").value = "";
    note.textContent = link.status === "green"
      ? `Trackable. ${link.reason}. ${new_matching} match your filter.`
      : `Not trackable. ${link.reason}.`;
    state.open.add(`l${link.id}`);
  } catch (err) {
    note.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Add & test";
    state.busy--;
    refresh();
  }
});

// ---- settings -----------------------------------------------------------------

async function loadSettings() {
  const s = await api("/api/settings");
  $("#tg-state").textContent = s.telegram_enabled ? "On"
    : s.telegram_configured ? "Off (turned off in config.yaml)" : "Not set up: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env";
  $("#tg-test").disabled = !s.telegram_configured;
  $("#ai-state").textContent = s.ai_enabled ? "On"
    : s.ai_key_present ? "Off (turned off in config.yaml)" : "Off: no GEMINI_API_KEY in .env";
  $("#ai-model").textContent = s.ai_model;
  $("#ai-min").textContent = `${s.min_score} of 10. Lower scores go to "Low score"`;
  $("#ai-profile").textContent = s.profile;
  $("#set-interval").textContent = `Every ${s.check_interval_minutes} minutes`;
  $("#set-last").textContent = whenLabel(state.info.last_check);
  $("#set-next").textContent = whenLabel(state.info.next_check);
  $("#set-ba").textContent = s.arbeitsagentur_searches.length ? s.arbeitsagentur_searches.join(", ") : "Turned off";
  $("#set-emails").textContent = s.emails;
}

$("#tg-test").onclick = async () => {
  const btn = $("#tg-test");
  const out = $("#tg-result");
  btn.disabled = true;
  out.textContent = "Sending…";
  try {
    await api("/api/telegram/test", { method: "POST" });
    out.textContent = "Sent. Check Telegram on your phone.";
  } catch (err) {
    out.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};

// ---- page -------------------------------------------------------------------

function renderHeader() {
  const info = state.info;
  $("#restart-banner").hidden = info.version === DASHBOARD_VERSION;
  $("#last-check").textContent = info.last_check ? `Last check ${whenLabel(info.last_check)}` : "No checks yet";
  const dot = $("#tg-dot");
  dot.className = `tg-dot${info.telegram_enabled ? " on" : ""}`;
  dot.title = info.telegram_enabled ? "Telegram alerts on" : "Telegram alerts off";
  dot.setAttribute("aria-label", dot.title);
  document.querySelectorAll(".ai-only").forEach((el) => { el.hidden = !info.ai_enabled; });
}

async function refresh() {
  const info = await api("/api/info");
  Object.assign(state.info, info);
  renderCounts(info);
  renderHeader();
  if (!info.ai_enabled && state.view === "low") setView("best");
  // Don't rebuild the job list under your cursor while a job is open.
  const reading = [...state.open].some((k) => k.startsWith("j"));
  await Promise.all([reading ? null : loadJobs(), loadLinks()]);
  if (state.tab === "settings") loadSettings();
}

function setView(view) {
  state.view = view;
  save("view", view);
  state.open.clear();
  applyView();
  loadJobs();
}

function applyView() {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === state.tab));
  ["jobs", "links", "settings"].forEach((t) => { $(`#panel-${t}`).hidden = state.tab !== t; });
  document.querySelectorAll("[data-view]").forEach((p) => p.classList.toggle("active", p.dataset.view === state.view));
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    state.tab = tab.dataset.tab;
    save("tab", state.tab);
    applyView();
    if (state.tab === "settings") loadSettings();
  };
});
document.querySelectorAll("[data-view]").forEach((pill) => { pill.onclick = () => setView(pill.dataset.view); });

$("#mark-all").onclick = async () => {
  renderCounts(await api("/api/jobs/seen-all", { method: "POST" }));
  state.open.clear();
  loadJobs();
  toast("All jobs marked as seen");
};

applyView();
refresh();
// Pick up new jobs from background checks; faster while Gemini is scoring.
let ticks = 0;
setInterval(() => {
  ticks++;
  const scoring = state.info.ai_enabled && state.info.ai_pending > 0;
  if (!state.busy && (scoring || ticks % 6 === 0)) refresh();
}, 10000);
