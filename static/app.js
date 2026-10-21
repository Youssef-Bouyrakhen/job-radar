/* =========================================================
   BACKEND CONNECTION — the only place to adapt to the real API.
   If GET jobs fails (file opened directly), the page runs on demo data.
   ========================================================= */
// Must match VERSION in app/__init__.py. If they differ, an old Job Radar is still running.
const DASHBOARD_VERSION = '9.3';

const ENDPOINTS = {
  jobs:        () => ['GET',    '/api/jobs?view=everything'],
  job:         id => ['GET',    `/api/jobs/${id}`],        // one job incl. description
  info:        () => ['GET',    '/api/info'],
  links:       () => ['GET',    '/api/links'],
  settings:    () => ['GET',    '/api/settings'],
  setStatus:   id => ['POST',   `/api/jobs/${id}/status`], // { status: ''|saved|applied|hidden }
  markSeen:    () => ['POST',   '/api/jobs/seen'],         // { ids: [], seen: true|false }
  checkAll:    () => ['POST',   '/api/check'],
  telegram:    () => ['POST',   '/api/telegram/switch'],   // { on: true|false }
  telegramTest:() => ['POST',   '/api/telegram/test'],
  addLink:     () => ['POST',   '/api/links'],             // { url } -> { link, new_jobs, new_matching }
  checkLink:   id => ['POST',   `/api/links/${id}/check`], // -> { link, new_jobs, new_matching }
  toggleLink:  id => ['PATCH',  `/api/links/${id}`],       // { active }
  deleteLink:  id => ['DELETE', `/api/links/${id}`],
  suggestions: () => ['GET',    '/api/suggestions'],       // -> { progress, suggestions }
  findSources: () => ['POST',   '/api/suggestions/find'],
  addSugg:     id => ['POST',   `/api/suggestions/${id}/add`],  // -> { link, new_jobs, new_matching }
  skipSugg:    id => ['POST',   `/api/suggestions/${id}/skip`],
  companies:   () => ['GET',    '/api/companies'],          // -> { progress, companies }
  runCompanies:() => ['POST',   '/api/companies/run'],
  importCompanies:() => ['POST', '/api/companies/import'],   // { text, label }
  lookalikes:  () => ['POST',   '/api/companies/lookalikes'],
  weeklySummary:() => ['POST',  '/api/companies/weekly-summary'],
  setCompany:  id => ['PATCH',  `/api/companies/${id}`],    // { status } or { category }
  coverage:    () => ['GET',    '/api/coverage'],           // -> { towns, covered_percent, per_night, progress }
  applications:() => ['GET',    '/api/applications'],       // -> { jobs: {id: {timeline, category}}, stats, review, ... }
  appCheck:    () => ['POST',   '/api/applications/check'],
  assignEmail: id => ['POST',   `/api/applications/emails/${id}/assign`],   // { job_id|null, kind }
  unassignEmail:id => ['POST',  `/api/applications/emails/${id}/unassign`]
};
async function api(name, arg, body) {
  const [method, url] = ENDPOINTS[name](arg);
  const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch (e) {} throw new Error(msg); }
  return res.status === 204 ? null : res.json().catch(() => null);
}

/* Translate the backend's field names into the shapes below. */
const STATUS_TO_API = { new: '', saved: 'saved', applied: 'applied', hidden: 'hidden' };
function maxHours(text) {
  const nums = (text || '').match(/\d+(?:[.,]\d+)?/g);
  return nums ? Math.max(...nums.map(n => parseFloat(n.replace(',', '.')))) : null;
}
function jobFromApi(j) {
  const keywordOut = !j.filter_passed;
  return {
    ...j,
    keyword_out: keywordOut,
    status: j.status || (keywordOut ? 'filtered' : 'new'),
    filter_reason: keywordOut ? j.filter_reason : (j.status === 'hidden' ? 'You hid this job' : null),
    max_hours: maxHours(j.hours),
    must_met: (() => { try { return JSON.parse(j.must_reasons || '[]'); } catch (e) { return []; } })(),
    seen: !!j.seen
  };
}
function linkFromApi(l) {
  const builtin = l.kind !== 'url';
  const status = l.status === 'green' ? (l.matching_jobs ? 'ok' : 'warn') : l.status === 'red' ? 'error' : 'unchecked';
  return {
    id: l.id, name: l.name || l.url, builtin, active: l.active, status, auto: !!l.auto_added,
    url: l.kind === 'gmail' ? `Label "${settings?.gmail_label || 'JobRadar'}" · read-only`
       : l.kind === 'arbeitsagentur' ? 'Searches from config.yaml' : l.url,
    status_text: status === 'warn' ? 'No matching jobs yet' : l.reason,
    method: l.method, reason: l.reason,
    good: l.matching_jobs, total: l.total_jobs, last_checked: l.last_checked
  };
}
function settingsFromApi(s) {
  return {
    gemini_model: s.ai_enabled ? s.ai_model : (s.ai_key_present ? 'Off in config.yaml' : 'Off: no AI key in .env'),
    ai_provider: s.ai_enabled ? s.ai_provider : null,
    ai_enabled: s.ai_enabled, min_score: s.min_score, interval_min: s.check_interval_minutes, profile: s.profile,
    ba_searches: s.arbeitsagentur_searches, emails: s.emails, gmail_label: s.gmail_label,
    applications: s.applications, applications_label: s.applications_label,
    telegram_on: s.telegram_enabled, telegram_configured: s.telegram_configured, telegram_off_in_config: s.telegram_off_in_config
  };
}

/* Data shapes the page expects:
   job:  { id, title, company, location, hours|null, max_hours|null, posted_at|null, first_seen,
           ai_score|null (null = still scoring), ai_reason|null, status: new|saved|applied|hidden|filtered,
           filter_reason|null, seen, source, site: linkedin|indeed|stepstone|arbeitsagentur|company|other, url }
   link: { id, name, url, builtin, active, status: ok|warn|error|unchecked, status_text, good, total, last_checked|null }
   settings: { gemini_model, min_score, profile, interval_min, ba_searches: [], gmail_label, gmail_address, telegram_on } */

/* ICONS */
const P = (d, extra = '') => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;
const ICON = {
  logo: P('<path d="M12 2.5c.7 5.2 3.6 8.3 9 9.5-5.4 1.2-8.3 4.3-9 9.5-.7-5.2-3.6-8.3-9-9.5 5.4-1.2 8.3-4.3 9-9.5z" stroke-width="1.9"/>'),
  flame: P('<path d="M12 21c-3.9 0-6.5-2.6-6.5-6.2 0-3.1 2-5.3 3.6-7 .3 1.7 1.2 2.9 2.4 3.4-.2-3 1-5.8 3.4-7.7.4 2.6 1.8 4.4 3.2 6.1 1.1 1.4 1.4 2.8 1.4 4.2 0 4.4-3.1 7.2-7.5 7.2z"/>'),
  sparkle: P('<path d="M12 3.5c.6 4.4 2.9 6.9 7.5 8-4.6 1.1-6.9 3.6-7.5 8-.6-4.4-2.9-6.9-7.5-8 4.6-1.1 6.9-3.6 7.5-8z"/>'),
  list: P('<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01" stroke-width="2"/>'),
  bookmark: P('<path d="M6.5 4h11v16.5L12 16.5l-5.5 4z"/>'),
  bookmarkFill: P('<path d="M6.5 4h11v16.5L12 16.5l-5.5 4z" fill="currentColor"/>'),
  checkCircle: P('<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.3l2.4 2.4 4.6-4.9"/>'),
  trendDown: P('<path d="M3.5 7.5l6 6 4-4 7 7M20.5 11v5.5H15"/>'),
  eyeOff: P('<path d="M3 3l18 18M10.6 6.1A10 10 0 0 1 12 6c5 0 9 6 9 6a16 16 0 0 1-3 3.4M6.6 6.6C4.3 8.1 3 12 3 12s4 6 9 6a9 9 0 0 0 4.4-1.1M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
  eye: P('<path d="M3 12s4-6 9-6 9 6 9 6-4 6-9 6-9-6-9-6z"/><circle cx="12" cy="12" r="3"/>'),
  link: P('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
  sliders: P('<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>'),
  send: P('<path d="M21 3.5L3 10.5l7 2.6 2.6 7.4zM10 13.1l5-5"/>'),
  moon: P('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
  sun: P('<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>'),
  star: P('<path d="M12 3.8l2.5 5.2 5.7.8-4.1 4 1 5.6L12 16.7l-5.1 2.7 1-5.6-4.1-4 5.7-.8z"/>'),
  calendar: P('<rect x="4" y="5" width="16" height="15" rx="2.5"/><path d="M8 3v4M16 3v4M4 10h16"/>'),
  checks: P('<path d="M3 12.5l4 4 8-9M13 15.5l1 1 8-9"/>'),
  refresh: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
  search: P('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>'),
  open: P('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
  check: P('<path d="M5 12.5l4.5 4.5L19 7.5" stroke-width="2"/>'),
  copy: P('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>'),
  plus: P('<path d="M12 5v14M5 12h14" stroke-width="2"/>'),
  chevron: P('<path d="M7 10l5 5 5-5"/>', 'class="dd-chevron"'),
  arrowDown: P('<path d="M12 5v14M6.5 13.5L12 19l5.5-5.5" stroke-width="2"/>'),
  arrowUp: P('<path d="M12 19V5M6.5 10.5L12 5l5.5 5.5" stroke-width="2"/>'),
  mail: P('<rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="M4 7l8 6 8-6"/>'),
  building: P('<path d="M5 20.5V5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v15.5M15 9.5h3a1 1 0 0 1 1 1v10M3.5 20.5h17M8.5 8h3M8.5 11.5h3M8.5 15h3"/>'),
  globe: P('<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.2-3.5-8.5s1.2-6.1 3.5-8.5z"/>'),
  trash: P('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
  radar: P('<circle cx="12" cy="12" r="8.5" opacity=".45"/><circle cx="12" cy="12" r="4.5" opacity=".75"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><path d="M12 12l5.5-5.5"/>')
};
function hydrateIcons(root = document) {
  root.querySelectorAll('svg[data-icon]').forEach(el => { el.outerHTML = ICON[el.dataset.icon].replace('<svg ', `<svg class="${el.getAttribute('class') || ''}" aria-hidden="true" `); });
}

/* DEMO DATA — only used when the backend can't be reached */
const daysAgo = d => { const t = new Date(); t.setDate(t.getDate() - d); t.setHours(9, 0, 0, 0); return t.toISOString(); };
const DEMO = {
  jobs: [
    { id: 1, title: 'Werkstudent Cloud Engineering (AWS)', company: 'Software AG', location: 'Darmstadt', hours: '15–20 h', max_hours: 20, posted_at: daysAgo(1), first_seen: daysAgo(1), ai_score: 9, ai_reason: 'AWS, Terraform, Linux · 15–20 h · Darmstadt. Builds internal cloud infrastructure, close to campus.', status: 'new', seen: false, source: 'Arbeitsagentur', source_type: 'portal', site: 'arbeitsagentur', url: 'https://example.com' },
    { id: 2, title: 'Werkstudent Softwareentwicklung (m/w/d)', company: 'iteratec', location: 'Frankfurt am Main', hours: '12–20 h', max_hours: 20, posted_at: daysAgo(2), first_seen: daysAgo(2), ai_score: 8, ai_reason: 'Java, TypeScript, agile team · 12–20 h · Frankfurt. Real product work with code reviews.', status: 'new', seen: false, source: 'StepStone', source_type: 'portal', site: 'stepstone', url: 'https://example.com' },
    { id: 3, title: 'Werkstudent IT-Security und Netzwerkkommunikation', company: 'Ratiodata SE', location: 'Frankfurt am Main', hours: null, max_hours: null, posted_at: null, first_seen: daysAgo(3), ai_score: 8, ai_reason: 'Networks, firewalls, Linux · Frankfurt. Hands-on network and security operations.', status: 'saved', seen: true, source: 'Indeed email', source_type: 'email', site: 'indeed', url: 'https://example.com' },
    { id: 4, title: 'Werkstudent FinOps und Cloud Cost Management', company: 'Heraeus', location: 'Hanau', hours: 'bis 20 h', max_hours: 20, posted_at: daysAgo(43), first_seen: daysAgo(1), ai_score: 7, ai_reason: 'Azure, cloud costs, Python · 20 h · Hanau. Ad is over 30 days old, may be filled.', status: 'new', seen: false, source: 'Arbeitsagentur', source_type: 'portal', site: 'arbeitsagentur', url: 'https://example.com' },
    { id: 5, title: 'Werkstudent Datenanalyse (m/w/d)', company: 'DZ BANK', location: 'Frankfurt am Main', hours: '20 h', max_hours: 20, posted_at: daysAgo(0), first_seen: daysAgo(0), ai_score: null, ai_reason: null, status: 'new', seen: false, source: 'DZ BANK', source_type: 'company', site: 'company', url: 'https://example.com' },
    { id: 6, title: 'Werkstudent IT Service Desk', company: 'Deutsche Telekom', location: 'Darmstadt', hours: '20 h', max_hours: 20, posted_at: daysAgo(6), first_seen: daysAgo(6), ai_score: 5, ai_reason: 'Mostly support tickets, little coding · 20 h · Darmstadt.', status: 'new', seen: true, source: 'LinkedIn email', source_type: 'email', site: 'linkedin', url: 'https://example.com' },
    { id: 7, title: 'Werkstudent Testautomatisierung', company: 'Commerzbank', location: 'Frankfurt am Main', hours: '25–30 h', max_hours: 30, posted_at: daysAgo(5), first_seen: daysAgo(5), ai_score: 6, ai_reason: 'Selenium, Java · 25–30 h. More than your 20 h limit.', status: 'filtered', filter_reason: 'More than 20 h/week', seen: true, source: 'StepStone', source_type: 'portal', site: 'stepstone', url: 'https://example.com' },
    { id: 8, title: 'Werkstudent Architekturmanagement', company: 'BWI', location: 'Frankfurt am Main', hours: null, max_hours: null, posted_at: daysAgo(4), first_seen: daysAgo(4), ai_score: null, ai_reason: null, status: 'applied', seen: true, source: 'Arbeitsagentur', source_type: 'portal', site: 'arbeitsagentur', url: 'https://example.com' }
  ],
  links: [
    { id: 'gmail', name: 'Job-alert emails (Gmail)', url: 'Label "JobRadar" · read-only', builtin: true, active: true, status: 'ok', status_text: 'OK', good: 4, total: 21, last_checked: daysAgo(0) },
    { id: 'ba', name: 'Bundesagentur für Arbeit', url: '5 searches · Darmstadt and Frankfurt', builtin: true, active: true, status: 'ok', status_text: 'OK', good: 10, total: 38, last_checked: daysAgo(0) },
    { id: 3, name: 'stepstone.de', url: 'stepstone.de/jobs/werkstudent-informatik/in-darmstadt', builtin: false, active: true, status: 'ok', status_text: 'OK', good: 12, total: 24, last_checked: daysAgo(0) },
    { id: 4, name: 'talents.studysmarter.de', url: 'talents.studysmarter.de/companies/…', builtin: false, active: true, status: 'warn', status_text: 'No good jobs in 7 days', good: 0, total: 6, last_checked: daysAgo(0) },
    { id: 5, name: 'c24-recruiting.powerappsportals.com', url: 'c24-recruiting.powerappsportals.com', builtin: false, active: false, status: 'error', status_text: 'Not trackable', good: 0, total: 0, last_checked: daysAgo(1) }
  ],
  settings: { gemini_model: 'gemini-3.5-flash', min_score: 7, interval_min: 60, profile: 'Informatik student at <your university>.\nWerkstudent in IT / software, max 20 h/week.', ba_searches: ['Werkstudent Informatik', 'Werkstudent IT', 'Werkstudent Software', 'Werkstudent Data'], gmail_label: 'JobRadar', gmail_address: '', telegram_on: true },
  info: { checking: false, last_check: null, next_check: null, telegram_on: true }
};

/* STATE */
const state = {
  tab: 'jobs', view: 'must', query: '', sort: 'score', mustDays: 3, mustScore: 8, mustCenter: 'Darmstadt',
  openId: null, focusId: null,
  loading: true, demo: false, checking: false, telegramOn: true,
  lastCheck: null, nextCheck: null, minScore: 7,
  aiOn: true, version: DASHBOARD_VERSION
};
let jobs = [], links = [], settings = null;
let apps = null; // application tracking: timelines, stats, Needs review (GET /api/applications)

const isActive = j => j.status !== 'hidden' && j.status !== 'filtered';
const scored = j => typeof j.ai_score === 'number';
// Still waiting for Gemini (it gives up after 3 failed tries, and never scores keyword-filtered jobs)
const scoring = j => state.aiOn && !scored(j) && !j.keyword_out && (j.ai_attempts ?? 0) < 3;
const good = j => !state.aiOn || (scored(j) && j.ai_score >= state.minScore);
// 🔥 Must apply: the backend decides (all 6 conditions); a job leaves only when you apply or hide it
const isMust = j => !!j.must_apply && j.status !== 'applied' && j.status !== 'hidden';
const mustOverdue = j => isMust(j) && daysSince(j) > state.mustDays;
const VIEWS = {
  must:     { label: 'Must apply',   icon: 'flame',       test: isMust },
  best:    { label: 'Best new',     icon: 'sparkle',     test: j => (j.status === 'new' || j.status === 'saved') && good(j) && !isOld(j) },
  all:      { label: 'All',          icon: 'list',        test: j => isActive(j) && j.status !== 'applied' },
  saved:    { label: 'Saved',        icon: 'bookmark',    test: j => j.status === 'saved' },
  applied:  { label: 'Applied',      icon: 'checkCircle', test: j => j.status === 'applied' },
  low:      { label: 'Low score',    icon: 'trendDown',   test: j => state.aiOn && isActive(j) && j.status !== 'applied' && scored(j) && j.ai_score < state.minScore },
  filtered: { label: 'Filtered out', icon: 'eyeOff',      test: j => !isActive(j) }
};

/* HELPERS */
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const dateOf = j => j.posted_at || j.first_seen;
const ageDays = iso => iso ? Math.max(0, Math.floor((Date.now() - new Date(iso)) / 864e5)) : 0;
const isOld = j => !!j.posted_at && ageDays(j.posted_at) > 30;
const fmtShort = iso => { const d = new Date(iso); const sameYear = d.getFullYear() === new Date().getFullYear(); return d.toLocaleDateString('en-GB', sameYear ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' }); };
const fmtAge = iso => { if (!iso) return '–'; const d = ageDays(iso); return d === 0 ? 'today' : d <= 14 ? d + ' d ago' : fmtShort(iso); };
const fmtDate = iso => iso ? new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '–';
const fmtTime = iso => iso ? new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : null;
const fmtAgo = iso => { if (!iso) return 'not checked yet'; const m = Math.round((Date.now() - new Date(iso)) / 6e4); return m < 1 ? 'checked just now' : m < 60 ? `checked ${m} min ago` : m < 1440 ? `checked ${Math.round(m / 60)} h ago` : `checked ${fmtShort(iso)}`; };
const scoreTier = s => s >= state.minScore ? 'good' : s >= 4 ? 'maybe' : 'weak';
const scoreLabel = s => s >= 9 ? 'Top match' : s >= state.minScore ? 'Good fit' : s >= 4 ? 'Maybe' : 'Weak';
const shortReason = r => r ? r.split('. ')[0].replace(/\.$/, '') : '';
const city = l => (l || '').replace(/ am Main/, '').replace(/, DE$/, '');
// No place is "Location unknown" (never Darmstadt); farther than the area's max_km: "Outside your area (Koblenz)"
const placeOf = j => j.area === 'outside' ? `Outside your area (${city(j.location).split(/[,;/(]/)[0].trim()})`
  : j.location ? city(j.location) : 'Location unknown';
const placeText = j => [placeOf(j), j.hours].filter(Boolean).join(' · ');
const initials = c => (c || '?').replace(/[^A-Za-zÄÖÜäöü ]/g, ' ').trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
const hue = s => [...(s || '?')].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

/* SOURCE + TIME FILTERS (work on top of the view, search and sort) */
// One box per site. The backend works out the site from the job link (alert emails link
// straight to LinkedIn/Indeed/StepStone) or the watched link. "other" only shows up if used.
const SOURCES = {
  all:            { label: 'All',            icon: 'list' },
  linkedin:       { label: 'LinkedIn',       short: 'LinkedIn',       mono: 'in' },
  indeed:         { label: 'Indeed',         short: 'Indeed',         mono: 'i' },
  stepstone:      { label: 'StepStone',      short: 'StepStone',      mono: 'S' },
  arbeitsagentur: { label: 'Arbeitsagentur', short: 'Arbeitsagentur', mono: 'BA' },
  company:        { label: 'Company sites',  short: 'Company site',   icon: 'building' },
  other:          { label: 'Other sites',    short: 'Other site',     icon: 'globe' }
};
const srcIcon = s => s.mono ? `<span class="src-mono">${s.mono}</span>` : ICON[s.icon];
const TIMES = { 1: 'Today', 3: 'Last 3 days', 7: 'Last 7 days', 30: 'Last 30 days', any: 'Any time' };

// Remembered only for this browser session (closing the browser resets to the defaults)
function sessionGet(key, allowed, fallback) {
  try { const v = sessionStorage.getItem(key); return v in allowed ? v : fallback; } catch (e) { return fallback; }
}
function sessionSet(key, value) { try { sessionStorage.setItem(key, value); } catch (e) {} }
state.source = sessionGet('jr-source', SOURCES, 'all');
state.time = sessionGet('jr-time', TIMES, '7');

// Whole calendar days since the job was posted (or first seen, if the site gives no date).
// "2026-10-16" (day only) means that day in local time, not midnight UTC.
const parseDay = s => /^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) : new Date(s);
const midnight = d => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
const daysSince = j => Math.max(0, Math.round((midnight(Date.now()) - midnight(parseDay(dateOf(j)))) / 864e5));

const matchSource = (j, source) => source === 'all' || (j.site || 'other') === source;
const matchTime = (j, time) => time === 'any' || daysSince(j) < Number(time);
function matchQuery(j) {
  const q = state.query.trim().toLowerCase();
  // The source too: "statworx (Companies)" is found by the company's name, whatever the ad calls it
  return !q || [j.title, j.company, j.location, j.ai_reason, j.source].join(' ').toLowerCase().includes(q);
}
// Jobs that pass everything. Override one part to count "what if I clicked this pill".
function filteredJobs({ view = state.view, source = state.source, time = state.time } = {}) {
  // Must apply jobs stay until you act on them, however old: the time filter doesn't hide them
  return jobs.filter(j => VIEWS[view].test(j) && matchSource(j, source) && (view === 'must' || matchTime(j, time)) && matchQuery(j));
}

function visibleJobs() {
  const byDate = (a, b) => parseDay(dateOf(b)) - parseDay(dateOf(a));
  const byScore = (a, b) => (b.ai_score ?? -1) - (a.ai_score ?? -1) || byDate(a, b);
  const sorter = { score: byScore, newest: byDate, oldest: (a, b) => byDate(b, a) }[state.sort];
  return filteredJobs().sort(sorter);
}

// Source box counts follow the time filter; time counts follow the source filter.
function renderFilters() {
  const count = opts => state.loading ? '–' : filteredJobs(opts).length;
  $('#sourceBoxes').innerHTML = Object.entries(SOURCES).map(([k, s]) => {
    const n = count({ source: k });
    if (k === 'other' && !state.loading && !jobs.some(j => j.site === 'other') && state.source !== 'other') return '';
    const on = state.source === k;
    return `<button type="button" class="src-box${on ? ' is-active' : ''}" data-source="${k}" aria-pressed="${on}">
      <span class="src-box-head">${srcIcon(s)}<span class="src-box-name">${s.label}</span></span><span class="src-box-count">${n}</span></button>`;
  }).join('');
  $('#timeBtn').innerHTML = `${ICON.calendar}<span>Time: ${TIMES[state.time]}</span>${ICON.chevron}`;
  $('#timeMenu').innerHTML = Object.entries(TIMES).map(([k, label]) => {
    const on = state.time === k;
    return `<button type="button" class="dd-item${on ? ' is-active' : ''}" data-time="${k}" role="option" aria-selected="${on}">
      <span class="dd-check">${on ? ICON.check : ''}</span><span class="dd-label">${label}</span><span class="dd-count">${count({ time: k })}</span></button>`;
  }).join('');
  renderSortArrow();
}
function openTimeMenu(open) {
  $('#timeMenu').hidden = !open;
  $('#timeBtn').setAttribute('aria-expanded', open);
  $('#timeDd').classList.toggle('is-open', open);
}
// "Posted" header: newest first, click again for oldest first
function renderSortArrow() {
  const dir = { newest: 'arrowDown', oldest: 'arrowUp' }[state.sort];
  $('#postedArrow').innerHTML = dir ? ICON[dir] : '';
  $('#postedSort').classList.toggle('is-sorted', !!dir);
  $('#postedSort').title = state.sort === 'newest' ? 'Newest first. Click for oldest first'
    : state.sort === 'oldest' ? 'Oldest first. Click for newest first' : 'Sort by posting date';
  $$('[data-sort]').forEach(b => b.classList.toggle('is-active', b.dataset.sort === state.sort));
}
function setSort(sort) { state.sort = sort; renderSortArrow(); renderList(); }
function setFilter(kind, value) {
  state[kind] = value; state.openId = null;
  sessionSet(kind === 'source' ? 'jr-source' : 'jr-time', value);
  renderNav(); renderHead(); renderFilters(); renderList();
}
function srcTag(j) {
  const s = SOURCES[j.site] || SOURCES.other;
  return `<span class="src-tag" title="Source: ${esc(j.source || '')}">${s.short}</span>`;
}

/* RENDER */
function renderNav() {
  const html = Object.entries(VIEWS).map(([key, v]) => {
    const active = state.tab === 'jobs' && state.view === key;
    const n = state.loading ? '–' : filteredJobs({ view: key }).length;
    return `<button type="button" class="nav-item${active ? ' is-active' : ''}" data-view="${key}" title="${v.label}" aria-current="${active}">${ICON[v.icon]}<span class="nav-label">${v.label}</span><span class="nav-count">${n}</span></button>`;
  }).join('');
  $('#navViews').innerHTML = html;
  // On phones the sidebar is hidden: the app pages go into the same scrolling row
  const pages = [['links', 'Watched links'], ['companies', 'Companies'], ['coverage', 'Coverage'], ['settings', 'Settings']]
    .map(([tab, label]) => `<button type="button" class="nav-item" data-tab="${tab}"><span class="nav-label">${label}</span></button>`).join('');
  $('#mobileViews').innerHTML = html + pages;
  $('#linksCount').textContent = state.loading ? '–' : links.length;
  $('#companiesCount').textContent = coLoaded ? companies.length : '–';
  $('#coverageCount').textContent = coverage ? coverage.covered_percent + '%' : '–';
  $$('.nav-item[data-tab]').forEach(b => b.classList.toggle('is-active', state.tab === b.dataset.tab));
}

function checkText() {
  if (state.checking || state.loading) return state.loading ? 'Loading…' : 'Checking sources…';
  const last = fmtTime(state.lastCheck), next = fmtTime(state.nextCheck);
  if (!last) return state.demo ? 'Demo data' : 'Not checked yet';
  return `Last check ${last}${next ? ' · next ' + next : ''}`;
}
function renderStatus() {
  const checking = state.checking || state.loading;
  $$('[data-check]').forEach(el => { el.classList.toggle('is-checking', checking); el.classList.toggle('is-offline', state.demo); });
  $$('[data-check-text]').forEach(el => el.textContent = checkText());
  $('#checkNow').classList.toggle('is-spinning', checking);
  $$('[data-tg]').forEach(el => { el.classList.toggle('tg-on', state.telegramOn); el.title = state.telegramOn ? 'Telegram alerts on' : 'Telegram alerts off'; });
}

function renderHead() {
  renderTitle();
  $('#pageTitle').textContent = VIEWS[state.view].label;
  const el = $('#summary');
  if (state.loading) { el.innerHTML = '<span class="skel" style="width:240px;height:12px;margin-top:5px"></span>'; return; }
  const best = filteredJobs({ view: 'best' });
  const pending = jobs.filter(j => isActive(j) && scoring(j)).length;
  const scores = best.filter(scored).map(j => j.ai_score);
  const parts = best.length
    ? [`<span><strong>${plural(best.length, 'good job')}</strong> to look at</span>`,
       `<span>${best.filter(j => (j.location || '').includes('Darmstadt')).length} in Darmstadt</span>`]
      .concat(scores.length ? [`<span>best score ${Math.max(...scores)}</span>`] : [])
    : [`<span>No new good jobs right now</span>`].concat(fmtTime(state.nextCheck) ? [`<span>next check ${fmtTime(state.nextCheck)}</span>`] : []);
  if (state.view === 'must') {
    const must = filteredJobs({ view: 'must' }), late = must.filter(mustOverdue).length;
    parts.splice(0, parts.length, must.length ? `<span><strong>${plural(must.length, 'job')}</strong> to apply to</span>` : '<span>Nothing to apply to right now</span>');
    if (late) parts.push(`<span class="demo-note">${late} older than ${state.mustDays} days: apply now or hide</span>`);
    parts.push(`<span>leaves only when you mark it Applied or hide it</span>`);
  }
  if (state.view === 'applied') parts.splice(0, parts.length, ...appSummary());
  if (pending && state.view !== 'applied') parts.push(`<span>${plural(pending, 'job')} still scoring</span>`);
  if (state.query.trim()) parts.push(`<span>${visibleJobs().length} match “${esc(state.query.trim())}”</span>`);
  const timeOn = state.time !== 'any' && state.view !== 'must'; // Must apply ignores the time filter
  if (state.source !== 'all' || timeOn) parts.push(`<span>${[state.source !== 'all' ? SOURCES[state.source].label : null, timeOn ? TIMES[state.time].toLowerCase() : null].filter(Boolean).join(', ')}</span>`);
  if (state.demo) parts.push('<span class="demo-note">Demo data, backend not connected</span>');
  if (!state.demo && state.version !== DASHBOARD_VERSION) parts.push(`<span class="demo-note">An old Job Radar is still running (server ${esc(state.version)}, page ${DASHBOARD_VERSION}). Close the black Job Radar window, then double-click start.bat again.</span>`);
  el.innerHTML = parts.join('');
}

function scoreChip(j) {
  if (scoring(j)) return `<span class="chip chip--pending" title="Gemini is scoring this job"><span class="chip-text">Scoring…</span></span>`;
  if (!scored(j)) return `<span class="chip chip--muted" title="${j.keyword_out ? 'Filtered jobs are not scored' : state.aiOn ? 'Gemini could not score this job' : 'Gemini is off'}"><span class="chip-text">No score</span></span>`;
  const t = scoreTier(j.ai_score);
  return `<span class="chip chip--${t}" title="AI score ${j.ai_score}/10"><span class="score-num">${j.ai_score}</span><span class="score-of">/10</span><span class="score-label">${scoreLabel(j.ai_score)}</span></span>`;
}
function postedChip(j) {
  if (!j.posted_at) return `<span class="chip chip--muted" title="The source gave no posting date. This is when Job Radar first saw it."><span class="chip-text">first seen ${fmtShort(j.first_seen)}</span></span>`;
  const days = ageDays(j.posted_at);
  if (days > 30) return `<span class="chip chip--old" title="Posted ${fmtDate(j.posted_at)}, more than 30 days ago"><span class="chip-text">Old ad · ${fmtShort(j.posted_at)}</span></span>`;
  return `<span class="chip"><span class="chip-text">${fmtAge(j.posted_at)}</span></span>`;
}
function postedPlain(j) { return !j.posted_at ? 'first seen ' + fmtShort(j.first_seen) : isOld(j) ? 'old ad · ' + fmtShort(j.posted_at) : fmtAge(j.posted_at); }

function jobRow(j) {
  const open = state.openId === j.id;
  const saved = j.status === 'saved', applied = j.status === 'applied', hidden = !isActive(j);
  const canHide = j.status !== 'filtered'; // keyword-filtered jobs can't be restored, the filter decides
  const tooMany = j.max_hours && j.max_hours > 20;
  const statusTag = saved && state.view !== 'saved' ? '<span class="status-tag">Saved</span>'
                  : applied ? appTag(j.app_status) : '';
  const reason = hidden && j.filter_reason ? j.filter_reason : shortReason(j.ai_reason);
  const must = isMust(j), overdue = mustOverdue(j);
  const mustTag = must && state.view !== 'must' ? `<span class="status-tag status-tag--must">${ICON.flame}Must apply</span>` : '';
  const overdueTag = overdue ? `<span class="status-tag status-tag--overdue" title="In Must apply for more than ${state.mustDays} days and not applied yet">${daysSince(j)} days old: apply now</span>` : '';
  const mustMet = must && j.must_met.length ? `<ul class="must-met" aria-label="Must-apply conditions met">${j.must_met.map(m => `<li>${ICON.check}${esc(m)}</li>`).join('')}</ul>` : '';
  return `
  <article class="job${open ? ' is-open' : ''}${state.focusId === j.id ? ' is-focused' : ''}${isOld(j) ? ' is-old' : ''}${overdue ? ' is-overdue' : ''}" data-id="${j.id}">
    <div class="job-row" data-action="toggle" aria-expanded="${open}">
      <div class="cell cell-who">
        <span class="avatar" style="--h:${hue(j.company)}">${esc(initials(j.company))}</span>
        <div class="who-text">
          <div class="job-head"><h3 class="job-title">${esc(j.title)}</h3>${!j.seen && !hidden ? '<span class="new-mark">New</span>' : ''}${statusTag}${mustTag}${overdueTag}</div>
          <div class="job-sub">${esc(j.company)} · ${srcTag(j)}<span class="sub-place"> · ${esc(placeText(j))}</span><span class="sub-age"> · ${esc(postedPlain(j))}</span></div>
          ${reason ? `<p class="job-reason">${esc(reason)}</p>` : ''}${state.view === 'must' ? mustMet : ''}
        </div>
      </div>
      <div class="cell cell-score">${scoreChip(j)}</div>
      <div class="cell cell-place"><span class="chip${tooMany || j.area === 'outside' ? ' chip--warn-hours' : ''}"${tooMany ? ' title="More than 20 h per week"' : j.area === 'outside' ? ` title="${Math.round(j.area_km)} km from ${esc(state.mustCenter || 'Darmstadt')}"` : ''}><span class="chip-text">${esc(placeText(j))}</span></span></div>
      <div class="cell cell-age">${postedChip(j)}</div>
      <div class="cell cell-actions">
        <button type="button" class="act" data-action="open" title="Open ad (o)" aria-label="Open ad">${ICON.open}</button>
        <button type="button" class="act${saved ? ' is-on' : ''}" data-action="save" title="${saved ? 'Unsave' : 'Save'} (s)" aria-label="Save">${saved ? ICON.bookmarkFill : ICON.bookmark}</button>
        <button type="button" class="act${applied ? ' is-on' : ''}" data-action="applied" title="Mark applied (a)" aria-label="Mark applied">${ICON.check}</button>
        ${canHide ? `<button type="button" class="act" data-action="hide" title="${hidden ? 'Restore' : 'Hide'} (h)" aria-label="${hidden ? 'Restore' : 'Hide'}">${hidden ? ICON.eye : ICON.eyeOff}</button>` : ''}
      </div>
    </div>
    <div class="job-details">
      <p class="details-reason"><span class="details-label">${scored(j) ? `AI assessment · ${j.ai_score}/10` : 'AI assessment'}</span>${esc(j.ai_reason || (scored(j) ? '' : scoring(j) ? 'Gemini is still scoring this job.' : 'No AI score for this job.'))}${j.ai_full_time ? '<br>Gemini thinks this is really a full-time job.' : ''}${hidden && j.filter_reason ? `<br><span class="details-label" style="margin-top:8px">${j.keyword_out ? 'Keyword filter' : 'Filtered because'}</span>${esc(j.filter_reason)}` : ''}</p>
      ${applied ? timeline(j) : ''}
      ${must ? `<div class="details-reason"><span class="details-label">Must apply · all 6 conditions met</span>${mustMet}</div>`
        : j.must_why_not && isActive(j) && j.status !== 'applied' ? `<p class="details-reason"><span class="details-label">Not in Must apply</span>${esc(j.must_why_not)}</p>` : ''}
      <div class="details-facts">
        <span>${j.posted_at ? 'Posted' : 'First seen'} <b>${fmtDate(dateOf(j))}</b>${j.posted_at ? '' : ' (the site shows no posting date)'}</span>
        <span>Source <b>${esc(j.source || '–')}</b>${SOURCES[j.site] ? ` (${SOURCES[j.site].short})` : ''}</span>
        <span>Location <b>${esc(j.location || 'unknown')}</b>${j.area === 'outside' ? ` (outside your area, ${Math.round(j.area_km)} km from ${esc(state.mustCenter || 'Darmstadt')})` : ''}</span>
        <span>Hours <b>${j.hours ? esc(j.hours) : 'not stated'}</b></span>
        ${j.employment ? `<span>Type <b>${esc(j.employment)}</b></span>` : ''}
      </div>
      <div class="details-actions">
        <button type="button" class="btn btn--primary" data-action="open">${ICON.open}Open ad</button>
        <button type="button" class="btn${saved ? ' is-on' : ''}" data-action="save">${saved ? ICON.bookmarkFill : ICON.bookmark}${saved ? 'Saved' : 'Save'}</button>
        <button type="button" class="btn${applied ? ' is-on' : ''}" data-action="applied">${ICON.check}${applied ? 'Applied' : 'Mark applied'}</button>
        ${canHide ? `<button type="button" class="btn" data-action="hide">${hidden ? ICON.eye : ICON.eyeOff}${hidden ? 'Show anyway' : 'Hide'}</button>` : ''}
        <button type="button" class="btn" data-action="copy">${ICON.copy}Copy for cover letter</button>
      </div>
    </div>
  </article>`;
}

function emptyState() {
  if (state.query.trim()) return `
    <div class="empty"><div class="empty-icon">${ICON.search}</div>
      <h3 class="empty-title">No jobs match “${esc(state.query.trim())}”</h3>
      <p class="empty-text">Try another word, or look in All.</p>
      <button type="button" class="btn" data-empty="clear">Clear search</button></div>`;
  const outside = jobs.filter(VIEWS[state.view].test).length; // jobs in this view the filters hide
  if (outside) return `
    <div class="empty"><div class="empty-icon">${ICON.calendar}</div>
      <h3 class="empty-title">Nothing here with these filters</h3>
      <p class="empty-text">${plural(outside, 'job')} in ${VIEWS[state.view].label} ${outside === 1 ? 'is' : 'are'} hidden by the source or time filter.</p>
      <button type="button" class="btn" data-empty="filters">Show all sources, any time</button></div>`;
  const next = fmtTime(state.nextCheck);
  const m = {
    must: ['Nothing you must apply to right now', `A job comes here when it meets all 6 conditions: Werkstudent, AI score ${state.mustScore}+, posted in the last ${state.mustDays} days, near ${esc(state.mustCenter)} or remote, no blockers, and a low-competition sign. You also get a Telegram message.`, 'Show best new', 'best'],
    best: ['You’re all caught up', next ? `No new good jobs right now. Next check at ${next}.` : 'No new good jobs right now.', 'Show all jobs', 'all'],
    all: ['No jobs yet', 'Add a watched link or wait for the next check.'],
    saved: ['Nothing saved yet', 'Save a job to come back to it later.', 'Show best new', 'best'],
    applied: ['No applications yet', 'Jobs you mark as applied show up here.', 'Show best new', 'best'],
    low: ['No low-score jobs', `Everything scores ${state.minScore} or higher.`],
    filtered: ['Nothing filtered out', 'Hidden and keyword-filtered jobs appear here.']
  }[state.view];
  return `
    <div class="empty"><div class="empty-icon">${ICON.radar}</div>
      <h3 class="empty-title">${m[0]}</h3><p class="empty-text">${m[1]}</p>
      ${m[2] ? `<button type="button" class="btn" data-empty="${m[3]}">${m[2]}</button>` : ''}</div>`;
}

function renderList() {
  const el = $('#jobList');
  if (state.loading) {
    el.innerHTML = [62, 48, 70, 55, 66, 44].map(w => `
      <div class="skel-row" aria-hidden="true"><div class="skel skel-av"></div>
        <div class="skel-lines"><div class="skel skel-line" style="width:${w}%"></div><div class="skel skel-line" style="width:${w - 22}%"></div></div>
        <div class="skel skel-chip"></div></div>`).join('');
    return;
  }
  const list = visibleJobs();
  el.innerHTML = list.length ? list.map(jobRow).join('') : emptyState();
}

function renderLinks() {
  const active = links.filter(l => l.active).length;
  $('#linksSummary').innerHTML = state.loading ? '' : `<span>${plural(links.length, 'source')}</span><span>${active} active</span><span>checked every ${settings?.interval_min || 60} min</span>`;
  if (!links.length) { $('#linkList').innerHTML = `<div class="empty" style="padding:40px 24px"><h3 class="empty-title">No watched links yet</h3><p class="empty-text">Paste a career page or job search link above.</p></div>`; return; }
  $('#linkList').innerHTML = links.map(l => {
    const dot = !l.active ? '' : l.status === 'ok' ? 'link-dot--ok' : l.status === 'warn' ? 'link-dot--warn' : l.status === 'error' ? 'link-dot--error' : '';
    const statusChip = !l.active ? `<span class="chip chip--muted"><span class="chip-text">Paused</span></span>`
      : l.status === 'error' ? `<span class="chip chip--error"><span class="chip-text">${esc(l.status_text || 'Not trackable')}</span></span>`
      : l.status === 'warn' ? `<span class="chip chip--maybe"><span class="chip-text">${esc(l.status_text || 'No good jobs lately')}</span></span>`
      : l.status === 'unchecked' ? `<span class="chip chip--muted"><span class="chip-text">Not checked yet</span></span>`
      : `<span class="chip chip--good"><span class="chip-text">${l.good} good · ${l.total} total</span></span>`;
    return `
    <div class="list-row${l.active ? '' : ' is-paused'}" data-link="${esc(l.id)}">
      <div class="list-main"><span class="link-dot ${dot}"></span>
        <div style="min-width:0"><div class="list-title">${esc(l.name)}${l.builtin ? ' <span class="status-tag">Built-in</span>' : ''}${l.auto ? ' <span class="status-tag" title="Added by the Company Discovery Engine (Companies page)">Auto-added</span>' : ''}</div>
          <div class="list-sub">${esc(l.url)} · ${esc(fmtAgo(l.last_checked))}</div>
          <div class="list-msg" data-link-msg hidden></div></div></div>
      <div class="list-chips">${statusChip}
        <button type="button" class="btn btn--sm" data-link-action="check">${ICON.refresh}Check now</button>
        ${l.builtin ? '' : `<button type="button" class="btn btn--sm btn--danger" data-link-action="delete">${ICON.trash}Delete</button>`}
        <button type="button" class="switch${l.active ? ' is-on' : ''}" data-link-action="toggle" role="switch" aria-checked="${l.active}" title="${l.active ? 'Pause' : 'Resume'}"></button>
      </div>
    </div>`;
  }).join('');
}

// Which AI scores jobs right now, and the backups that take over when it hits its limit
function aiProviderRow(s, row, chip) {
  const p = s.ai_provider;
  if (!p) return row('AI provider', 'Scores every job that passes the filter', chip(s.gemini_model || '–'));
  const order = p.providers.map(x => x.name + (x.status === 'empty' ? ' (used up today)' : x.paused_minutes ? ` (paused ${x.paused_minutes} min)` : '')).join(' → ');
  return row('AI provider', `Scores every job that passes the filter, up to 10 per request · order: ${esc(order)}`,
    p.active ? chip(`${p.active} · ${p.model}`, 'chip--good') : chip(p.all_empty ? 'All used up today, jobs wait' : 'All paused, back in a few minutes', 'chip--maybe'));
}

function renderSettings() {
  if (!settings) { $('#settingsList').innerHTML = ''; return; }
  const s = settings;
  const row = (t, sub, right) => `<div class="list-row"><div><div class="list-title">${t}</div><div class="list-sub">${sub}</div></div><div class="list-chips">${right}</div></div>`;
  const chip = (txt, cls = '') => `<span class="chip ${cls}"><span class="chip-text">${esc(txt)}</span></span>`;
  const tgSub = s.telegram_configured === false ? 'Not set up: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env'
              : s.telegram_off_in_config ? 'Turned off in config.yaml' : 'One message for every good new job';
  const tgTest = s.telegram_configured === false ? '' : `<button type="button" class="btn btn--sm" data-tg-test>${ICON.send}Send test message</button>`
    + `<button type="button" class="btn btn--sm" data-tg-weekly title="The summary Job Radar sends every Sunday evening">Send weekly summary</button>`;
  $('#settingsList').innerHTML = [
    row('Telegram alerts', `${tgSub}<span class="list-msg" data-tg-msg hidden style="display:block"></span>`, `${tgTest}<button type="button" class="switch${state.telegramOn ? ' is-on' : ''}" data-tg role="switch" aria-checked="${state.telegramOn}"></button>`),
    row('Minimum score', `Lower scores go to Low score and are not sent`, chip(`Score ≥ ${s.min_score}`, 'chip--good')),
    aiProviderRow(s, row, chip),
    row('Checks', 'How often all sources are checked', chip(`Every ${s.interval_min} min`)),
    row('Arbeitsagentur searches', 'Darmstadt and Frankfurt, 25 km', (s.ba_searches || []).length ? s.ba_searches.map(x => chip(x)).join('') : chip('Turned off', 'chip--muted')),
    row('Job-alert emails', esc(s.emails || 'Read-only, emails stay unread'), chip(`Label "${s.gmail_label || 'JobRadar'}"`)),
    row('Application emails', `${esc(s.applications || 'Read-only, emails stay unread')} · replies to your applications update the Applied page`, chip(`Label "${s.applications_label || 'Bewerbungen'}"`)),
    `<div class="list-row"><div><div class="list-title">Your profile</div><div class="list-sub">What Gemini knows about you</div></div><div class="profile-text">${esc(s.profile || '')}</div></div>`
  ].join('');
}

// AI usage bar at the top of every page: one box per provider (used / estimated free limit today)
const AI_STATUS = { active: 'Active', next: 'Next', paused: 'Paused', empty: 'Empty' };
function renderAiBar() {
  const bar = $('#aiBar'), ai = state.ai;
  bar.hidden = state.demo || !ai;
  if (bar.hidden) return;
  const reset = new Date(ai.reset_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const boxes = ai.providers.map(p => {
    const pct = Math.min(100, Math.round(100 * p.used / Math.max(1, p.budget)));
    const tag = p.status === 'paused' && p.paused_minutes ? `Paused ${p.paused_minutes} min` : AI_STATUS[p.status];
    // Cloudflare counts "neurons" (its unit of AI work), the others requests
    const neurons = p.unit === 'neurons', n = x => x.toLocaleString('en-US');
    const k = x => x < 1000 ? `${x}` : `${+(x / 1000).toFixed(1)}k`;  // 4.2k: fits the small box
    const title = neurons ? `${n(p.used)} of ${n(p.budget)} free neurons used today (Cloudflare's daily limit: 10,000)`
      : `${p.used} of about ${p.budget} free requests used today`;
    return `<div class="ai-box ai-box--${p.status}" title="${esc(p.name)} · ${esc(p.model)}: ${title}">
      <div class="ai-box-head"><span class="ai-name">${esc(p.name)}</span><span class="ai-tag">${esc(tag)}</span></div>
      <div class="ai-count">${neurons ? `${k(p.used)} / ${k(p.budget)} neurons` : `${p.used} / ~${p.budget}`}</div>
      <div class="ai-meter"><span style="width:${pct}%"></span></div>
    </div>`;
  }).join('');
  const waiting = state.aiPending || 0;
  bar.classList.toggle('is-empty', !!ai.all_empty);
  bar.innerHTML = `${boxes}<div class="ai-info">
      <span class="ai-waiting${waiting ? ' has-waiting' : ''}">${waiting} job${waiting === 1 ? '' : 's'} waiting for a score</span>
      <span class="ai-reset">${ai.all_empty ? 'All AI limits used up · ' : ''}Resets at ${esc(reset)}</span>
    </div>`;
}

function render() { renderAiBar(); renderStatus(); renderNav(); renderHead(); renderAppPanel(); renderFilters(); renderList(); renderLinks(); renderSettings(); }

// Browser tab shows how many good jobs you haven't opened yet, e.g. "(3) Job Radar"
function renderTitle() {
  const n = state.loading ? 0 : jobs.filter(j => VIEWS.best.test(j) && !j.seen).length;
  const must = state.loading ? 0 : jobs.filter(isMust).length;
  document.title = (must ? `🔥${must} ` : '') + (n ? `(${n}) Job Radar` : 'Job Radar');
}

/* TOAST */
let toastTimer, toastFn;
function toast(msg, action) {
  $('#toastMsg').textContent = msg;
  const btn = $('#toastAction');
  btn.hidden = !action;
  if (action) { btn.textContent = action.label || 'Undo'; toastFn = action.fn; }
  $('#toast').classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 5000);
}
function hideToast() { $('#toast').classList.remove('is-visible'); toastFn = null; }
$('#toastAction').addEventListener('click', () => { const fn = toastFn; hideToast(); if (fn) fn(); });

/* LOAD */
async function loadAll() {
  try {
    const [j, l, s, i] = await Promise.all([api('jobs'), api('links').catch(() => []), api('settings').catch(() => null), api('info').catch(() => ({}))]);
    settings = s ? settingsFromApi(s) : null;
    jobs = j.map(jobFromApi);
    links = l.map(linkFromApi);
    applyInfo(i);
    state.demo = false;
  } catch (e) {
    jobs = DEMO.jobs.map(x => ({ ...x })); links = DEMO.links.map(x => ({ ...x })); settings = { ...DEMO.settings };
    applyInfo(DEMO.info); state.demo = true;
  }
  if (settings?.min_score) state.minScore = settings.min_score;
  if (settings && typeof settings.telegram_on === 'boolean') state.telegramOn = settings.telegram_on;
  state.loading = false;
  render();
  loadSuggestions();
  loadCompanies();
  loadCoverage();
  loadApps();
}
function applyInfo(i) {
  if (!i) return;
  state.checking = !!i.checking;
  state.lastCheck = i.last_check || state.lastCheck;
  state.nextCheck = i.next_check || state.nextCheck;
  if (typeof i.telegram_on === 'boolean') state.telegramOn = i.telegram_on;
  if (typeof i.telegram_enabled === 'boolean') state.telegramOn = i.telegram_enabled;
  if (typeof i.ai_enabled === 'boolean') state.aiOn = i.ai_enabled;
  if ('ai' in i) state.ai = i.ai;
  if (typeof i.ai_pending === 'number') state.aiPending = i.ai_pending;
  if (i.min_score) state.minScore = i.min_score;
  if (i.must) Object.assign(state, { mustDays: i.must.max_days, mustScore: i.must.min_score, mustCenter: i.must.center });
  if (i.version) state.version = i.version;
  // A mismatch is usually an old page still in the browser: reload once before warning
  let reloaded = null; try { reloaded = sessionStorage.getItem('jr-reloaded'); } catch (e) {}
  if (i.version && i.version !== DASHBOARD_VERSION && reloaded !== i.version) {
    sessionSet('jr-reloaded', i.version); location.reload();
  }
}
async function refreshQuiet() {
  if (state.demo) return;
  try {
    const [j, l, i] = await Promise.all([api('jobs'), api('links').catch(() => null), api('info').catch(() => null)]);
    jobs = j.map(jobFromApi);
    // Not while you're on Watched links: that would wipe "Really delete?" and check messages
    if (l && state.tab !== 'links') { links = l.map(linkFromApi); renderLinks(); }
    applyInfo(i); renderAiBar(); renderStatus(); renderNav(); renderHead(); renderFilters(); if (!state.openId) renderList();
  } catch (e) {}
  loadApps(true);
}

/* ACTIONS */
const byId = id => jobs.find(j => String(j.id) === String(id));
async function persist(name, arg, body) { if (state.demo) return; return api(name, arg, body); }

// 'new' means "no status"; a keyword-filtered job with no status stays in Filtered out
const localStatus = (job, status) => status === 'new' && job.keyword_out ? 'filtered' : status;
const apiStatus = status => STATUS_TO_API[status] ?? '';

async function setStatus(job, status, msg) {
  const prev = job.status;
  job.status = localStatus(job, status); job.seen = true; // the backend also marks it seen
  const row = document.querySelector(`.job[data-id="${job.id}"]`);
  if (row && !VIEWS[state.view].test(job)) { row.classList.add('is-leaving'); setTimeout(render, 180); } else render();
  try {
    await persist('setStatus', job.id, { status: apiStatus(status) });
    toast(msg, { label: 'Undo', fn: async () => { job.status = prev; render(); try { await persist('setStatus', job.id, { status: apiStatus(prev) }); } catch (e) { toast('Could not undo: ' + e.message); } } });
  } catch (e) { job.status = prev; render(); toast('Could not save: ' + e.message); }
}

async function markSeen(list, seen) {
  list.forEach(j => j.seen = seen); render();
  try { await persist('markSeen', null, { ids: list.map(j => j.id), seen }); }
  catch (e) { list.forEach(j => j.seen = !seen); render(); toast('Could not save: ' + e.message); }
}

function doAction(id, action) {
  const j = byId(id); if (!j) return;
  if (action === 'toggle') {
    state.openId = state.openId === j.id ? null : j.id; state.focusId = j.id;
    if (!j.seen && state.openId) markSeen([j], true); else renderList();
    return;
  }
  if (action === 'open') {
    window.open(j.url, '_blank', 'noopener');
    if (!j.seen) markSeen([j], true);
    if (j.status !== 'applied') toast(`Opened ${j.company} ad`, { label: 'Mark applied', fn: () => setStatus(j, 'applied', 'Marked as applied') });
    return;
  }
  if (action === 'save') return setStatus(j, j.status === 'saved' ? 'new' : 'saved', j.status === 'saved' ? 'Removed from saved' : 'Saved');
  if (action === 'applied') return setStatus(j, j.status === 'applied' ? 'new' : 'applied', j.status === 'applied' ? 'Unmarked as applied' : 'Marked as applied');
  if (action === 'hide') {
    if (j.status === 'filtered') return toast('The keyword filter removed this job, it can’t be restored');
    return isActive(j) ? setStatus(j, 'hidden', 'Job hidden. Find it under Filtered out') : setStatus(j, 'new', 'Moved back to your list');
  }
  if (action === 'copy') return copyForCoverLetter(j);
}

async function copyForCoverLetter(j) {
  let description = '';
  if (!state.demo) { try { description = (await api('job', j.id))?.description || ''; } catch (e) {} }
  const text = [`Position: ${j.title}`, `Company: ${j.company || '–'}`, `Location: ${j.location || '–'}`, j.hours ? `Hours: ${j.hours}` : null,
    `${j.posted_at ? 'Posted' : 'First seen'}: ${fmtDate(dateOf(j))} (${j.source || '–'})`,
    scored(j) ? `AI score: ${j.ai_score}/10` : null, j.ai_reason ? `Why it fits: ${j.ai_reason}` : null, `Link: ${j.url}`,
    description ? `\nJob description:\n${description}` : null].filter(Boolean).join('\n');
  const done = () => toast(description ? 'Copied, with the job description. Paste it into your cover letter chat.' : 'Copied (no description saved for this job).');
  const fallback = () => {
    const ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { toast('Could not copy'); }
    ta.remove();
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}

function markAllSeen() {
  const unseen = jobs.filter(j => !j.seen && isActive(j));
  if (!unseen.length) return toast('Everything is already seen');
  markSeen(unseen, true);
  toast(`Marked ${plural(unseen.length, 'job')} as seen`, { label: 'Undo', fn: () => markSeen(unseen, false) });
}

async function checkNow() {
  if (state.checking || state.loading) return;
  if (state.demo) return toast('Demo data: connect the backend to check sources');
  const before = jobs.filter(VIEWS.best.test).length;
  state.checking = true; renderStatus();
  try {
    const i = await api('checkAll'); applyInfo(i); state.checking = true; renderStatus();
    for (let n = 0; n < 60; n++) {
      await new Promise(r => setTimeout(r, 3000));
      const info = await api('info').catch(() => null);
      if (info && !info.checking) { applyInfo(info); break; }
    }
  } catch (e) { toast('Check failed: ' + e.message); }
  state.checking = false;
  await refreshQuiet();
  const gained = jobs.filter(VIEWS.best.test).length - before;
  toast(gained > 0 ? `${plural(gained, 'new good job')} found` : 'Check done · no new good jobs');
}

async function toggleTelegram() {
  const was = state.telegramOn;
  state.telegramOn = !was; renderStatus(); renderSettings();
  try {
    const r = await persist('telegram', null, { on: state.telegramOn });
    if (r && typeof r.telegram_enabled === 'boolean') state.telegramOn = r.telegram_enabled;
    if (settings) settings.telegram_on = state.telegramOn;
    toast(state.telegramOn ? 'Telegram alerts on' : 'Telegram alerts paused', { label: 'Undo', fn: toggleTelegram });
  } catch (e) { state.telegramOn = was; renderStatus(); renderSettings(); toast('Could not change Telegram: ' + e.message); }
}

// After adding or checking a link: the server's answer has no "matching jobs" count, the list has
async function reloadLinks() {
  try { links = (await api('links')).map(linkFromApi); } catch (e) {}
  renderLinks(); renderNav();
}

/* Suggested sources (Phase 8) */
let suggestions = [], suggestProgress = {}, suggestTimer = null;
const SIZE_LABEL = { big: 'Big company', mid: 'Mid-size', startup: 'Startup' };

async function loadSuggestions() {
  if (state.demo) return renderSuggestions();
  try {
    const r = await api('suggestions');
    suggestions = r.suggestions; suggestProgress = r.progress;
  } catch (e) { suggestProgress = { error: 'Could not load suggestions: ' + e.message }; }
  renderSuggestions();
  clearTimeout(suggestTimer);
  if (suggestProgress.running) suggestTimer = setTimeout(loadSuggestions, 2500); // live progress
}

function renderSuggestions() {
  const p = suggestProgress, btn = $('#findBtn');
  btn.disabled = !!p.running;
  btn.lastChild.textContent = p.running ? 'Searching…' : 'Find new sources';
  const summary = $('#suggestSummary');
  if (p.running) {
    summary.innerHTML = `<span class="status-line is-checking"><span class="status-dot"></span>${esc(p.step)}${p.total ? ` ${p.done} of ${p.total}` : ''}</span>`;
  } else if (p.error) {
    summary.innerHTML = `<span class="demo-note">${esc(p.error)}</span>`;
  } else if (suggestions.length) {
    const good = suggestions.filter(s => s.state === 'green').length;
    summary.innerHTML = `<span>${plural(suggestions.length, 'company')}</span><span>${good} with good jobs right now</span>`
      + (p.finished_at ? `<span>last search ${fmtTime(p.finished_at)}${p.done ? `, ${p.found} of ${p.done} had a working link` : ''}</span>` : '');
  } else {
    summary.innerHTML = state.demo ? '<span>Demo data: connect the backend to find sources</span>'
      : '<span>Gemini suggests IT companies near Darmstadt and Frankfurt, Job Radar tests their careers pages. Takes 2–5 minutes.</span>';
  }
  const list = $('#suggestList');
  list.hidden = !suggestions.length;
  list.innerHTML = suggestions.map(s => {
    const dot = s.state === 'green' ? 'link-dot--ok' : s.state === 'amber' ? 'link-dot--warn' : 'link-dot--error';
    const chip = s.state === 'red'
      ? `<span class="chip chip--error" title="${esc(s.reason)}"><span class="chip-text">No job list found</span></span>`
      : `<span class="chip chip--${s.state === 'green' ? 'good' : 'maybe'}" title="Good = Werkstudent IT job (keyword filter) in the Darmstadt / Frankfurt area"><span class="chip-text">${s.good_jobs} good · ${s.total_jobs} total</span></span>`;
    return `
    <div class="list-row" data-sugg="${s.id}">
      <div class="list-main"><span class="link-dot ${dot}"></span>
        <div style="min-width:0">
          <div class="list-title">${esc(s.company)}${SIZE_LABEL[s.size] ? ` <span class="status-tag">${SIZE_LABEL[s.size]}</span>` : ''}${s.data_count ? ` <span class="status-tag" title="How often it appeared in your email alerts and Arbeitsagentur jobs">In your jobs ${s.data_count}×</span>` : ''}</div>
          <div class="list-sub">${esc([s.city, s.focus].filter(Boolean).join(' · '))}</div>
          <div class="list-sub"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url.replace(/^https?:\/\/(www\.)?/, ''))}</a> · ${esc(s.method)}</div>
          <div class="list-msg" data-sugg-msg hidden></div>
        </div></div>
      <div class="list-chips">${chip}
        <button type="button" class="btn btn--sm btn--primary" data-sugg-action="add">${ICON.plus}Add</button>
        <button type="button" class="btn btn--sm" data-sugg-action="skip">Skip</button>
      </div>
    </div>`;
  }).join('');
}

$('#findBtn').addEventListener('click', async () => {
  if (state.demo) return toast('Demo data: connect the backend to find sources');
  try {
    const r = await api('findSources');
    suggestions = r.suggestions; suggestProgress = r.progress;
  } catch (e) { suggestProgress = { error: e.message }; }
  renderSuggestions();
  $('#suggestSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (suggestProgress.running) { clearTimeout(suggestTimer); suggestTimer = setTimeout(loadSuggestions, 2500); }
});

$('#suggestList').addEventListener('click', async e => {
  const b = e.target.closest('[data-sugg-action]'); if (!b) return;
  const id = Number(b.closest('[data-sugg]').dataset.sugg);
  const s = suggestions.find(x => x.id === id); if (!s) return;
  const drop = () => { suggestions = suggestions.filter(x => x !== s); renderSuggestions(); };
  if (b.dataset.suggAction === 'skip') {
    drop();
    try { await api('skipSugg', id); toast(`Skipped ${s.company}. It won’t be suggested again`); }
    catch (x) { toast('Could not skip: ' + x.message); loadSuggestions(); }
    return;
  }
  b.disabled = true; b.lastChild.textContent = 'Adding…';
  const msg = b.closest('[data-sugg]').querySelector('[data-sugg-msg]');
  msg.textContent = 'Testing the link. This can take up to a minute.'; msg.hidden = false;
  try {
    const r = await api('addSugg', id);
    drop(); await reloadLinks();
    toast(`Added ${s.company} · ${plural(r.new_matching || 0, 'matching job')}`);
    refreshQuiet();
  } catch (x) {
    if (/already/.test(x.message)) { drop(); toast(x.message); return; }
    b.disabled = false; b.lastChild.textContent = 'Add'; msg.textContent = x.message;
  }
});

async function sendTelegramTest(btn, endpoint = 'telegramTest') {
  const msg = $('[data-tg-msg]');
  if (state.demo) return toast('Demo data: connect the backend to send a test');
  btn.disabled = true;
  try { await api(endpoint); msg.textContent = 'Sent. Check Telegram on your phone.'; }
  catch (e) { msg.textContent = e.message; }
  msg.hidden = false; btn.disabled = false;
}

/* Watched links */
function linkMsg(id, msg) {
  const el = document.querySelector(`[data-link="${CSS.escape(String(id))}"] [data-link-msg]`);
  if (!el) return;
  el.textContent = msg; el.hidden = false;
  clearTimeout(el._t); el._t = setTimeout(() => { el.hidden = true; }, 5000);
}
$('#addForm').addEventListener('submit', async e => {
  e.preventDefault();
  const input = $('#addInput'), err = $('#addError');
  const url = input.value.trim();
  if (!url) { err.textContent = 'Paste a career page URL first.'; input.focus(); return; }
  try { new URL(url); } catch (x) { err.textContent = 'That doesn’t look like a link. It should start with https://'; input.focus(); return; }
  if (links.some(l => l.url === url)) { err.textContent = 'This link is already on your list.'; return; }
  err.textContent = '';
  const btn = $('#addBtn'); btn.disabled = true; btn.lastChild.textContent = 'Testing…';
  try {
    if (state.demo) {
      links.push({ id: Date.now(), name: new URL(url).hostname, url, builtin: false, active: true, status: 'unchecked', status_text: '', good: 0, total: 0, last_checked: null });
      input.value = ''; renderLinks(); renderNav(); toast('Link added');
    } else {
      const r = await api('addLink', null, { url });
      input.value = ''; await reloadLinks();
      toast(r.link.status === 'red' ? `Can’t track this link: ${r.link.reason || 'no jobs found'}` : `Trackable · ${plural(r.new_jobs || 0, 'job')} found, ${r.new_matching || 0} matching`);
      refreshQuiet();
    }
  } catch (x) { err.textContent = x.message || 'Could not add this link.'; }
  btn.disabled = false; btn.lastChild.textContent = 'Add and test';
});
$('#addInput').addEventListener('input', () => { $('#addError').textContent = ''; });

$('#linkList').addEventListener('click', async e => {
  const b = e.target.closest('[data-link-action]'); if (!b) return;
  const rowEl = b.closest('[data-link]'); const id = rowEl.dataset.link;
  const l = links.find(x => String(x.id) === id); if (!l) return;
  const act = b.dataset.linkAction;
  if (act === 'toggle') {
    l.active = !l.active; renderLinks();
    try { await persist('toggleLink', l.id, { active: l.active }); } catch (x) { l.active = !l.active; renderLinks(); toast('Could not save: ' + x.message); }
    return;
  }
  if (act === 'check') {
    if (state.demo) return linkMsg(l.id, 'Demo data: connect the backend to check this link.');
    b.disabled = true;
    try {
      const r = await api('checkLink', l.id);
      await reloadLinks();
      const fresh = links.find(x => String(x.id) === id) || l;
      linkMsg(fresh.id, fresh.status === 'error' ? `Not trackable: ${fresh.status_text}` : `Checked · ${plural(r.new_jobs || 0, 'new job')} · ${fresh.good} matching in total`);
      refreshQuiet();
    } catch (x) { b.disabled = false; linkMsg(l.id, 'Check failed: ' + x.message); }
    return;
  }
  if (act === 'delete') {
    if (b.dataset.confirm !== '1') { b.dataset.confirm = '1'; b.lastChild.textContent = 'Really delete?'; setTimeout(() => { if (b.isConnected) { b.dataset.confirm = ''; b.lastChild.textContent = 'Delete'; } }, 4000); return; }
    try { await persist('deleteLink', l.id); links = links.filter(x => x !== l); renderLinks(); renderNav(); toast('Link deleted'); }
    catch (x) { toast('Could not delete: ' + x.message); }
  }
});

/* Companies (Company Discovery Engine) */
const CO_STATUS = { all: 'All', new: 'New', watched: 'Watched', initiativ: 'Initiativ', no_career_page: 'No careers page', skipped: 'Skipped' };
const CO_STATUS_ICON = { all: 'list', new: 'sparkle', watched: 'link', initiativ: 'send', no_career_page: 'eyeOff', skipped: 'trash' };
const CO_SOURCE = { arbeitsagentur: 'Arbeitsagentur', your_jobs: 'Your jobs', osm_it: 'OSM IT office', osm_software: 'OSM software office',
                    osm_telecommunication: 'OSM telecom office', osm_company: 'OSM company', lookalike: 'Look-alike' };
// "list:konaktiva 2026" = a list you imported; "region:Darmstadt:osm:engineer" = found by the region coverage
const REGION_SOURCE = { aa: 'AA', osm: 'OSM', register: 'Register' };
const coSourceName = k => k.startsWith('list:') ? k.slice(5)
  : k.startsWith('region:') ? `${REGION_SOURCE[k.split(':')[2]] || k.split(':')[2]} ${k.split(':')[1]}` : CO_SOURCE[k] || k;
const CO_CATEGORY = { insurance: 'Insurance', finance: 'Banks/Finance', cloud: 'Cloud/Data centers/Networks',
                      security: 'Cybersecurity', software: 'IT services/Software', other: 'Other' };
const CO_CATEGORY_FROM = { name: 'from its name or website', jobs: 'from its job titles', osm: 'an IT office on OpenStreetMap',
                           ai: 'sorted by the AI', you: 'chosen by you' };
// Category searches on the Arbeitsagentur: "ba_insurance" = found by the insurance keywords
Object.entries(CO_CATEGORY).forEach(([k, label]) => { CO_SOURCE['ba_' + k] = 'AA search: ' + label; });
const CO_PAGE = 200;
let companies = [], coProgress = {}, coTimer = null, coShown = CO_PAGE, coLoaded = false;
state.coStatus = sessionGet('jr-co-status', CO_STATUS, 'all');
state.coQuery = '';
state.coTown = '';  // set from the Coverage page: only this town's companies
const CO_CATEGORY_FILTER = { all: 'All categories', ...CO_CATEGORY, unsorted: 'Not sorted yet' };
state.coCategory = sessionGet('jr-co-category', CO_CATEGORY_FILTER, 'all');

async function loadCompanies() {
  if (state.demo) { coLoaded = true; return renderCompanies(); }
  try {
    const r = await api('companies');
    companies = r.companies; coProgress = r.progress; coLoaded = true;
  } catch (e) { coProgress = { error: 'Could not load companies: ' + e.message }; }
  renderCompanies(); renderNav();
  clearTimeout(coTimer);
  if (coProgress.running) coTimer = setTimeout(loadCompanies, 2500); // live progress
}

// Best fit first; then job feed, careers page, not checked yet, nothing found; then how often it was found
const coRank = c => ({ green: 0, amber: 1 }[c.career_state] ?? (c.last_checked ? 3 : 2));
const coTotal = c => Object.values(c.sources || {}).reduce((a, b) => a + b, 0);
function coMatches(c, status = state.coStatus, category = state.coCategory) {
  if (status !== 'all' && c.status !== status) return false;
  if (category !== 'all' && (c.category || 'unsorted') !== category) return false;
  if (state.coTown && c.town !== state.coTown) return false;
  const q = state.coQuery.trim().toLowerCase();
  return !q || [c.name, c.city, c.town, c.website, c.contact_email, CO_CATEGORY[c.category], ...Object.keys(c.sources || {}).map(coSourceName)].some(x => (x || '').toLowerCase().includes(q));
}
const bareUrl = u => u.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '');

function coSources(c) {
  const why = k => k === 'arbeitsagentur' ? 'IT job ads on the Arbeitsagentur'
    : k === 'your_jobs' ? 'Times it appears in your jobs, alerts and links'
    : k === 'lookalike' ? 'Suggested by the AI: similar to companies whose jobs you saved or applied to'
    : k.startsWith('list:') ? 'From a list you imported'
    : k.startsWith('ba_') ? 'IT job ads found by the keyword searches for this category on the Arbeitsagentur'
    : k.startsWith('region:') ? { aa: `Job ads of any kind in ${k.split(':')[1]} on the Arbeitsagentur`,
        osm: `On OpenStreetMap in ${k.split(':')[1]} (${k.split(':')[3] || ''})`,
        register: `In the company register (OffeneRegister.de, 2019), seat in ${k.split(':')[1]}` }[k.split(':')[2]] || k
    : 'Office on OpenStreetMap';
  const mine = k => k.startsWith('list:') || k === 'lookalike';
  return Object.entries(c.sources || {}).sort((a, b) => mine(b[0]) - mine(a[0]) || b[1] - a[1]).map(([k, n]) =>
    `<span class="status-tag" title="${why(k)}">${esc(coSourceName(k))}${n > 1 && !mine(k) ? ` ${n}×` : ''}</span>`).join('');
}
function coJobs(c) {
  if (!c.jobs_read_at) return '';
  if (c.jobs_method == null) return `<div class="co-sub" title="${esc(c.jobs_note || '')}">Jobs: ${esc(c.jobs_note || 'could not be read')}</div>`;
  const near = c.jobs_near ?? 0, parts = [`${near} job${near === 1 ? '' : 's'} here`];
  if (c.it_jobs) parts.push(`${c.it_jobs} IT`);
  if (c.student_jobs) parts.push(`${c.student_jobs} Werkstudent`);
  parts.push(c.good_jobs ? `<strong>${c.good_jobs} good</strong>` : '0 good');
  const title = `Read with: ${c.jobs_method}. ${c.jobs_total} jobs in total, ${near} in the Darmstadt / Frankfurt area (or remote / nationwide).`
    + (c.good_note ? ` Good = Werkstudent / part-time + passes your filter + in the area: ${c.good_note}.` : '')
    + (c.jobs_note ? ' ' + c.jobs_note : '');
  const text = near ? `<a href="#" data-co-jobs="${c.id}">${parts.join(' · ')}</a>` : parts.join(' · ');
  return `<div class="co-sub" title="${esc(title + (near ? ' Click to see these jobs.' : ''))}">${text}</div>`;
}
// Opens the company's jobs in the Jobs views: searched by its name, any source, any time
// (its ads are often older than the time filter), in the first view that has some of them.
function showCompanyJobs(c) {
  state.query = c.name; $('#search').value = c.name;
  setFilter('source', 'all'); setFilter('time', 'any');
  state.view = ['all', 'low', 'filtered'].find(v => filteredJobs({ view: v }).length) || 'all';
  state.openId = null; state.focusId = null;
  setTab('jobs'); render();
}
function coCareer(c) {
  const link = (u, label) => u ? `<a href="${esc(u)}" target="_blank" rel="noopener" title="${esc(u)}">${label || esc(bareUrl(u))}</a>` : '';
  const note = esc(c.notes || '');
  if (c.career_state === 'green') return `<span class="chip chip--good" title="${note}"><span class="chip-text">${esc(c.ats_type)} job list</span></span>
      <div class="co-sub">${link(c.feed_url, 'job feed')}${c.career_url && c.career_url !== c.feed_url ? ` · ${link(c.career_url, 'careers page')}` : ''}</div>${coJobs(c)}`;
  if (c.career_state === 'amber') return `<span class="chip chip--maybe" title="${note}"><span class="chip-text">Careers page</span></span>
      <div class="co-sub">${link(c.career_url)}</div>${coJobs(c)}`;
  if (c.last_checked) return `<span class="chip chip--error" title="${note}"><span class="chip-text">No careers page</span></span>
      <div class="co-sub" title="${note}">${note}</div>`;
  return `<span class="chip chip--muted"><span class="chip-text">Not checked yet</span></span>`;
}
function coRow(c) {
  const place = [c.city, c.distance_km != null ? `${Math.round(c.distance_km)} km` : null].filter(Boolean).join(' · ');
  const site = c.website ? `<a href="${esc(c.website)}" target="_blank" rel="noopener">${esc(bareUrl(c.website).replace(/\/.*$/, ''))}</a>` : 'no website yet';
  const size = c.size ? ` <span class="status-tag" title="Company size (AI, or its number of IT job ads)">${esc(c.size)}</span>` : '';
  const fitTag = c.fit_score != null ? `<span class="co-fit co-fit--${c.fit_score >= 60 ? 'good' : c.fit_score >= 40 ? 'maybe' : 'weak'}" title="Fit score 0–100: ${esc(c.fit_reasons || '')}">${c.fit_score}</span>` : '';
  const auto = c.link_id ? ' <span class="status-tag" title="Added to your watched links by the engine">auto-added</span>' : '';
  const reason = c.status === 'initiativ' && c.initiativ_reason ? `<div class="co-reason">${esc(c.initiativ_reason)}</div>` : '';
  const email = c.contact_email ? `<div class="co-sub">Email: <a href="mailto:${esc(c.contact_email)}">${esc(c.contact_email)}</a>${c.contact_url ? ` · <a href="${esc(c.contact_url)}" target="_blank" rel="noopener">source</a>` : ''}</div>` : '';
  const statusNote = c.status_note ? `<div class="co-note" title="${esc(c.status_note)}">${esc(c.status_note)}</div>` : '';
  const options = Object.entries(CO_STATUS).filter(([k]) => k !== 'all')
    .map(([k, label]) => `<option value="${k}"${c.status === k ? ' selected' : ''}>${label}</option>`).join('');
  const catOptions = (c.category ? '' : '<option value="" selected>Not sorted yet</option>') + Object.entries(CO_CATEGORY)
    .map(([k, label]) => `<option value="${k}"${c.category === k ? ' selected' : ''}>${label}</option>`).join('');
  const catWhy = c.category ? `Category ${CO_CATEGORY_FROM[c.category_from] || ''}. Change it if it's wrong.` : 'Not sorted yet: the AI sorts a few hundred companies a day';
  return `<div class="co-row" data-company="${c.id}">
    <div class="co-who"><div class="co-name">${fitTag}${esc(c.name)}${size}${auto}</div><div class="co-sub">${c.origin_note ? `<span class="co-origin">${esc(c.origin_note)}</span> · ` : ''}${site}${c.fit_reasons ? ` · ${esc(c.fit_reasons)}` : ''}</div>
      <div class="co-sub co-sub-extra">${esc(place)}</div>${reason}${email}</div>
    <div class="co-city">${esc(place || '–')}</div>
    <div class="co-tags-cell"><div class="co-tags">${coSources(c)}</div></div>
    <div class="co-career">${coCareer(c)}</div>
    <div class="co-status"><select class="co-select" aria-label="Status of ${esc(c.name)}">${options}</select>
      <select class="co-select co-cat${c.category ? '' : ' is-unsorted'}" aria-label="Category of ${esc(c.name)}" title="${esc(catWhy)}">${catOptions}</select>${statusNote}</div>
  </div>`;
}

function renderCompanies() {
  const p = coProgress, btn = $('#companiesRun');
  ['#companiesRun', '#lookalikesRun', '#importBtn'].forEach(id => $(id).disabled = !!p.running);
  btn.lastChild.textContent = p.running && p.kind === 'run' ? 'Running…' : 'Run discovery';
  $('#lookalikesRun').lastChild.textContent = p.running && p.kind === 'lookalikes' ? 'Finding…' : 'Find look-alikes';
  const checked = companies.filter(c => c.last_checked);
  const parts = [];
  if (p.running) parts.push(`<span class="status-line is-checking"><span class="status-dot"></span>${esc(p.step)}${p.total ? ` · ${p.done} of ${p.total}` : ''}</span>`);
  else if (state.demo) parts.push('<span>Demo data: connect the backend to discover companies</span>');
  else if (!companies.length) parts.push('<span>Collects IT employers around Darmstadt and Frankfurt and finds their careers pages. The first run takes about 5 minutes.</span>');
  if (companies.length) parts.push(`<span><strong>${companies.length} ${companies.length === 1 ? 'company' : 'companies'}</strong></span>`,
    `<span>${checked.length} checked</span>`,
    `<span>${checked.filter(c => c.career_url).length} careers pages</span>`,
    `<span>${checked.filter(c => c.feed_url).length} job feeds</span>`,
    `<span>${companies.filter(c => c.good_jobs).length} with good jobs</span>`);
  if (!p.running && p.finished_at && !p.error) parts.push(`<span>${p.kind === 'import' ? 'import' : p.kind === 'lookalikes' ? 'look-alikes' : 'last run'} ${fmtTime(p.finished_at)}: ${p.careers} new careers pages, ${p.jobs_read} job lists read, ${p.new_jobs} new jobs, ${p.auto_added} auto-added</span>`);
  (p.notes || []).forEach(n => parts.push(`<span>${esc(n)}</span>`));
  (p.warnings || []).forEach(w => parts.push(`<span class="demo-note">${esc(w)}</span>`));
  if (p.error) parts.push(`<span class="demo-note">${esc(p.error)}</span>`);
  $('#companiesSummary').innerHTML = parts.join('');
  const townBar = $('#coTownFilter');
  townBar.hidden = !state.coTown;
  townBar.innerHTML = state.coTown ? `<span>Only companies in <strong>${esc(state.coTown)}</strong></span><button type="button" class="btn btn--sm" id="coTownClear">Show all</button>` : '';

  $('#companyBoxes').innerHTML = Object.entries(CO_STATUS).map(([k, label]) => {
    const on = state.coStatus === k, n = coLoaded ? companies.filter(c => coMatches(c, k)).length : '–';
    return `<button type="button" class="src-box${on ? ' is-active' : ''}" data-co-status="${k}" aria-pressed="${on}">
      <span class="src-box-head">${ICON[CO_STATUS_ICON[k]]}<span class="src-box-name">${label}</span></span><span class="src-box-count">${n}</span></button>`;
  }).join('');
  // Category counts follow the chosen status
  $('#categoryBoxes').innerHTML = Object.entries(CO_CATEGORY_FILTER).map(([k, label]) => {
    const on = state.coCategory === k, n = coLoaded ? companies.filter(c => coMatches(c, state.coStatus, k)).length : '–';
    if (k === 'unsorted' && !n && !on) return '';
    return `<button type="button" class="src-box${on ? ' is-active' : ''}" data-co-category="${k}" aria-pressed="${on}">
      <span class="src-box-head"><span class="src-box-name">${label}</span></span><span class="src-box-count">${n}</span></button>`;
  }).join('');

  const list = companies.filter(c => coMatches(c))
    .sort((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1) || coRank(a) - coRank(b) || coTotal(b) - coTotal(a) || a.name.localeCompare(b.name));
  $('#companyList').innerHTML = !coLoaded ? '<div class="empty"><p class="empty-text">Loading…</p></div>'
    : list.length ? list.slice(0, coShown).map(coRow).join('')
    : `<div class="empty"><h3 class="empty-title">${companies.length ? 'No companies match' : 'No companies yet'}</h3><p class="empty-text">${companies.length ? 'Try another status or search.' : 'Click “Run discovery” to collect them.'}</p></div>`;
  const more = $('#companiesMore');
  more.hidden = list.length <= coShown;
  more.textContent = `Show more (${list.length - coShown} left)`;
}

$('#companiesRun').addEventListener('click', async () => {
  if (state.demo) return toast('Demo data: connect the backend to discover companies');
  try { coProgress = (await api('runCompanies')).progress; } catch (e) { coProgress = { error: e.message }; }
  renderCompanies();
  if (coProgress.running) { clearTimeout(coTimer); coTimer = setTimeout(loadCompanies, 2500); }
});
function startCompanies(r) {
  coProgress = r.progress; renderCompanies();
  if (coProgress.running) { clearTimeout(coTimer); coTimer = setTimeout(loadCompanies, 2500); }
}
function showImport(open) {
  $('#importForm').hidden = !open; $('#importOpen').setAttribute('aria-expanded', open);
  $('#importError').textContent = '';
  if (open) $('#importText').focus();
}
$('#importOpen').addEventListener('click', () => showImport($('#importForm').hidden));
$('#importCancel').addEventListener('click', () => showImport(false));
$('#importForm').addEventListener('submit', async e => {
  e.preventDefault();
  if (state.demo) return toast('Demo data: connect the backend to import companies');
  const text = $('#importText').value.trim(), label = $('#importLabel').value.trim();
  const err = $('#importError');
  if (!text) { err.textContent = 'Paste company names (one per line) or a link.'; return $('#importText').focus(); }
  if (!label) { err.textContent = 'Give the list a name, e.g. konaktiva 2026. It shows as the source of these companies.'; return $('#importLabel').focus(); }
  try { startCompanies(await api('importCompanies', null, { text, label })); }
  catch (x) { err.textContent = x.message; return; }
  $('#importText').value = ''; showImport(false);
  toast(`Importing “${label}”. The companies go through the normal checks`);
});
$('#lookalikesRun').addEventListener('click', async () => {
  if (state.demo) return toast('Demo data: connect the backend to find look-alikes');
  try { startCompanies(await api('lookalikes')); } catch (e) { toast(e.message); }
});
$('#companyBoxes').addEventListener('click', e => {
  const b = e.target.closest('[data-co-status]'); if (!b) return;
  state.coStatus = b.dataset.coStatus; coShown = CO_PAGE; sessionSet('jr-co-status', state.coStatus); renderCompanies();
});
$('#categoryBoxes').addEventListener('click', e => {
  const b = e.target.closest('[data-co-category]'); if (!b) return;
  state.coCategory = b.dataset.coCategory; coShown = CO_PAGE; sessionSet('jr-co-category', state.coCategory); renderCompanies();
});
$('#coTownFilter').addEventListener('click', e => {
  if (!e.target.closest('#coTownClear')) return;
  state.coTown = ''; coShown = CO_PAGE; renderCompanies();
});
$('#companySearch').addEventListener('input', e => { state.coQuery = e.target.value; coShown = CO_PAGE; renderCompanies(); });
$('#companiesMore').addEventListener('click', () => { coShown += CO_PAGE; renderCompanies(); });
$('#companyList').addEventListener('click', e => {
  const a = e.target.closest('[data-co-jobs]'); if (!a) return;
  e.preventDefault();
  const c = companies.find(x => String(x.id) === a.dataset.coJobs);
  if (c) showCompanyJobs(c);
});
$('#companyList').addEventListener('change', async e => {
  const sel = e.target.closest('.co-select'); if (!sel) return;
  const c = companies.find(x => String(x.id) === sel.closest('[data-company]').dataset.company); if (!c) return;
  if (sel.classList.contains('co-cat')) {
    if (!sel.value) return;
    const before = [c.category, c.category_from];
    c.category = sel.value; c.category_from = 'you'; renderCompanies();
    try { await persist('setCompany', c.id, { category: c.category }); toast(`${c.name}: ${CO_CATEGORY[c.category]}`); }
    catch (x) { [c.category, c.category_from] = before; renderCompanies(); toast('Could not save: ' + x.message); }
    return;
  }
  const prev = [c.status, c.status_note];
  c.status = sel.value; c.status_note = null; renderCompanies();
  try { await persist('setCompany', c.id, { status: c.status }); toast(`${c.name}: ${CO_STATUS[c.status]}`); }
  catch (x) { [c.status, c.status_note] = prev; renderCompanies(); toast('Could not save: ' + x.message); }
});

/* Coverage: per town, how many of its businesses have a final status */
const COV_STATUS = [['watched', 'Watched'], ['initiativ', 'Initiativ'], ['no_career_page', 'No careers page'], ['skipped', 'Skipped'], ['waiting', 'Waiting']];
let coverage = null, covTimer = null;
async function loadCoverage() {
  if (state.demo) return renderCoverage();
  try { coverage = await api('coverage'); } catch (e) { coverage = { error: 'Could not load the coverage: ' + e.message }; }
  renderCoverage(); renderNav();
  clearTimeout(covTimer);
  if (coverage.progress?.running && state.tab === 'coverage') covTimer = setTimeout(loadCoverage, 5000);
}
const pluralOf = (n, one, many) => `${n} ${n === 1 ? one : many}`;
// total: the "Whole region" row on top (its numbers aren't links: the Companies page filters one town)
function covRow(t, total) {
  const found = total ? `${coverage.towns.length} towns` : Object.entries(t.sources || {}).map(([k, n]) => `${k.replace(/^the /, '')} ${n}`).join(' · ');
  const chips = COV_STATUS.map(([k, label]) => total ? `<span class="cov-status"><span class="cov-status-n">${t[k]}</span>${label}</span>`
    : `<button type="button" class="cov-status" data-cov-town="${esc(t.town)}" data-cov-status="${k}"
      title="Show these companies"><span class="cov-status-n">${t[k]}</span>${label}</button>`).join('');
  const left = t.nights_left ? `${pluralOf(t.unchecked, 'company', 'companies')} not checked yet: about ${pluralOf(t.nights_left, 'night', 'nights')} left` : 'every company checked';
  return `<div class="cov-row${total ? ' cov-row--total' : ''}">
    <div class="cov-town"><div class="co-name">${esc(t.town)}</div><div class="co-sub" title="Found via (places / job ads / register entries used)">${esc(found || 'not collected yet')}</div></div>
    <div class="cov-num"><span class="cov-label">Companies</span>${t.companies}</div>
    <div class="cov-num"><span class="cov-label">Careers pages</span>${t.careers}<div class="co-sub">${t.feeds} job feeds · ${t.emails} emails</div></div>
    <div class="cov-check"><div class="cov-pct">${t.percent}%</div><div class="cov-meter"><span style="width:${t.percent}%"></span></div><div class="co-sub">${esc(left)}</div></div>
    <div class="cov-statuses">${chips}</div>
  </div>`;
}
function renderCoverage() {
  const r = coverage, p = r?.progress || {};
  const parts = ['<span>Every business in the region that could hire an IT Werkstudent</span>'];
  if (state.demo) parts.push('<span>Demo data: connect the backend to see the coverage</span>');
  else if (r && !r.error) parts.push(`<span>${r.per_night} more companies checked per night${r.run_hour != null ? ` (${r.run_hour}:00)` : ''}</span>`);
  if (p.running) parts.push(`<span class="status-line is-checking"><span class="status-dot"></span>${esc(p.step)}${p.total ? ` · ${p.done} of ${p.total}` : ''}</span>`);
  if (r?.error) parts.push(`<span class="demo-note">${esc(r.error)}</span>`);
  $('#coverageSummary').innerHTML = parts.join('');
  if (!r || r.error || state.demo) {
    $('#coverageHero').innerHTML = '';
    $('#coverageList').innerHTML = `<div class="empty"><p class="empty-text">${r || state.demo ? 'No coverage data.' : 'Loading…'}</p></div>`;
    return;
  }
  const total = r.towns.reduce((a, t) => a + t.companies, 0), done = r.towns.reduce((a, t) => a + t.checked, 0);
  $('#coverageHero').innerHTML = `<div class="cov-hero-num">${r.covered_percent}%</div>
    <div class="cov-hero-text"><div class="cov-hero-title">Region covered</div>
      <div class="co-sub">${done} of ${pluralOf(total, 'company', 'companies')} in ${r.towns.map(t => esc(t.town)).join(', ')} have a final status</div>
      <div class="cov-meter cov-meter--big"><span style="width:${r.covered_percent}%"></span></div></div>`;
  $('#coverageList').innerHTML = r.towns.length && total ? (r.total && r.towns.length > 1 ? covRow(r.total, true) : '') + r.towns.map(t => covRow(t)).join('')
    : `<div class="empty"><h3 class="empty-title">Not collected yet</h3><p class="empty-text">The next discovery run (Companies page, or at night) collects the businesses of ${r.towns.map(t => esc(t.town)).join(', ')}.</p></div>`;
}
// A status opens the Companies page with only that town's companies in that status ("Waiting" = New)
$('#coverageList').addEventListener('click', e => {
  const b = e.target.closest('[data-cov-status]'); if (!b) return;
  state.coTown = b.dataset.covTown;
  state.coStatus = b.dataset.covStatus === 'waiting' ? 'new' : b.dataset.covStatus;
  state.coCategory = 'all'; state.coQuery = ''; $('#companySearch').value = ''; coShown = CO_PAGE;
  setTab('companies');
});

function setTab(tab) {
  state.tab = tab;
  ['jobs', 'links', 'companies', 'coverage', 'settings'].forEach(t => $('#tab-' + t).hidden = t !== tab);
  if (tab === 'links' && !state.loading) loadSuggestions();
  if (tab === 'companies' && !state.loading) loadCompanies();
  if (tab === 'coverage' && !state.loading) loadCoverage();
  if (tab === 'settings' && !state.loading && !state.demo) {  // the active AI provider can change
    api('settings').then(s => { settings = settingsFromApi(s); renderSettings(); }).catch(() => {});
  }
  $$('.jobs-only').forEach(el => el.hidden = tab !== 'jobs');
  $('#mainScroll').scrollTop = 0;
  renderNav();
}

/* APPLICATION TRACKING (Applied page)
   Replies in the Gmail label "Bewerbungen" move an applied job on: Confirmed / Test / Interview / Rejected.
   Emails the backend can't match for sure wait in "Needs review" until you pick the job. */
const APP_STATUS = {
  confirmation: 'Confirmed', test: 'Test', interview: 'Interview', rejection: 'Rejected', other: 'Other'
};
const appTag = st => st ? `<span class="app-tag app-tag--${esc(st)}">${APP_STATUS[st] || esc(st)}</span>` : '<span class="app-tag" title="No reply yet">Applied</span>';
const appliedJobs = () => jobs.filter(j => j.status === 'applied');

async function loadApps(quiet) {
  if (state.demo) return;
  try { apps = await api('applications'); } catch (e) { if (!quiet) apps = null; return; }
  if (state.view === 'applied' && state.tab === 'jobs') { renderHead(); renderAppPanel(); if (!state.openId || !quiet) renderList(); }
}

function statsText(s) {
  const parts = [`<b>${s.applied}</b> applied`, `<b>${s.rejection}</b> rejected`, `<b>${s.interview}</b> interview${s.interview === 1 ? '' : 's'}`];
  if (s.test) parts.push(`<b>${s.test}</b> test${s.test === 1 ? '' : 's'}`);
  return parts.join(' · ');
}
// "X applied · Y rejected · Z interviews", counted from the jobs on your Applied list
function appSummary() {
  const list = appliedJobs();
  if (!list.length) return ['<span>No applications yet</span>'];
  const n = st => list.filter(j => j.app_status === st).length;
  const parts = [`<span>${statsText({ applied: list.length, rejection: n('rejection'), interview: n('interview'), test: n('test') })}</span>`];
  const waiting = list.filter(j => !j.app_status || j.app_status === 'confirmation').length;
  if (waiting) parts.push(`<span>${waiting} waiting for an answer</span>`);
  if (apps?.review?.length) parts.push(`<span class="demo-note">${plural(apps.review.length, 'email')} to review</span>`);
  return parts;
}

function timeline(j) {
  const info = apps?.jobs?.[j.id];
  const items = [`<li><span class="tl-date">${j.applied_at ? fmtShort(j.applied_at) : '–'}</span>${appTag(null)}<span class="tl-subj">You marked it applied</span></li>`]
    .concat((info?.timeline || []).map(t => `<li><span class="tl-date">${t.at ? fmtShort(t.at) : '–'}</span>${appTag(t.kind)}
      <span class="tl-subj">${esc(t.subject || '(no subject)')}</span><span class="tl-from">${esc(t.sender || '')}${t.by === 'ai' ? ' · sorted by AI' : t.by === 'you' ? ' · by you' : ''}</span>
      <button type="button" class="link-btn" data-unassign="${esc(t.gmail_id)}" title="This email is about another application: move it to Needs review">Not this job</button></li>`));
  const cat = info?.category_label ? ` · ${esc(info.category_label)}` : '';
  return `<div class="details-reason"><span class="details-label">Timeline${cat}</span><ol class="timeline">${items.join('')}</ol>
    ${info?.timeline?.length ? '' : `<span class="list-sub">No reply yet in your Gmail label "${esc(apps?.label || 'Bewerbungen')}".</span>`}</div>`;
}

function renderAppPanel() {
  const el = $('#appPanel');
  const show = state.tab === 'jobs' && state.view === 'applied' && !state.loading && !state.demo && !!apps;
  el.hidden = !show;
  if (!show) return;
  const labels = apps.category_labels || {};
  const cats = Object.keys(labels).filter(k => apps.stats?.[k]?.applied);
  const catLine = cats.length > 1 || (cats.length === 1 && cats[0] !== 'other')
    ? `<div class="app-cats" aria-label="Per category">${cats.map(k => `<span>${esc(labels[k])}: ${statsText(apps.stats[k])}</span>`).join('')}</div>` : '';
  const last = apps.last?.at ? `Emails checked ${fmtTime(apps.last.at)}${apps.last.ok === false ? ': ' + esc(apps.last.note || 'failed') : ''}`
    : apps.enabled ? `Label "${esc(apps.label)}" is checked every few minutes` : 'Off: no Gmail login in .env';
  const options = sel => ['<option value="">Pick the job…</option>'].concat(appliedJobs()
    .sort((a, b) => (a.company || '').localeCompare(b.company || ''))
    .map(j => `<option value="${j.id}"${String(j.id) === String(sel) ? ' selected' : ''}>${esc(((j.company || '?') + ' · ' + j.title).slice(0, 90))}</option>`)).join('');
  const kinds = k => Object.entries(APP_STATUS).map(([v, l]) => `<option value="${v}"${v === k ? ' selected' : ''}>${l}</option>`).join('');
  const rows = (apps.review || []).map(r => `
    <div class="list-row" data-review="${esc(r.gmail_id)}">
      <div class="list-main">
        <div class="list-title">${esc(r.subject || '(no subject)')}</div>
        <div class="list-sub">${r.at ? fmtDate(r.at) : '–'} · ${esc(r.sender)}${r.note ? ` · <b>${esc(r.note)}</b>` : ''}</div>
        ${r.snippet ? `<div class="app-snippet">${esc(r.snippet)}</div>` : ''}
      </div>
      <div class="list-chips">
        <select class="co-select" data-review-job aria-label="Job this email belongs to">${options(r.guess)}</select>
        <select class="co-select" data-review-kind aria-label="What the email says">${kinds(r.kind)}</select>
        <button type="button" class="btn btn--sm btn--primary" data-review-act="assign">Assign</button>
        <button type="button" class="btn btn--sm" data-review-act="dismiss" title="Not about one of your applications">Dismiss</button>
      </div>
    </div>`).join('');
  el.innerHTML = `${catLine}
    <div class="app-review-head"><h2 class="section-title">Needs review${apps.review?.length ? ` (${apps.review.length})` : ''}</h2>
      <button type="button" class="btn btn--sm" data-app-check>${ICON.mail}Check emails now</button><span class="list-sub" data-app-msg>${last}</span></div>
    ${rows ? `<div class="list-panel">${rows}</div>` : `<p class="list-sub">Nothing to review: every reply found its job.</p>`}`;
}

async function unassignEmail(id) {
  try { apps = await api('unassignEmail', id); } catch (e) { return toast('Could not save: ' + e.message); }
  await refreshQuiet(); renderHead(); renderAppPanel(); renderList();
  toast('Moved to Needs review');
}

$('#appPanel').addEventListener('click', async e => {
  const check = e.target.closest('[data-app-check]');
  if (check) {
    check.disabled = true; $('[data-app-msg]').textContent = 'Reading new emails… (can take a minute)';
    try { apps = await api('appCheck'); await refreshQuiet(); renderHead(); renderAppPanel(); renderList(); toast('Emails checked'); }
    catch (err) { check.disabled = false; $('[data-app-msg]').textContent = err.message; }
    return;
  }
  const act = e.target.closest('[data-review-act]'); if (!act) return;
  const row = act.closest('[data-review]');
  const assign = act.dataset.reviewAct === 'assign';
  const jobId = row.querySelector('[data-review-job]').value, kind = row.querySelector('[data-review-kind]').value;
  if (assign && !jobId) return toast('Pick the job this email belongs to first');
  act.disabled = true;
  try {
    apps = await api('assignEmail', row.dataset.review, assign ? { job_id: Number(jobId), kind } : { job_id: null });
    await refreshQuiet(); renderHead(); renderAppPanel(); renderList();
    toast(assign ? 'Email added to the job’s timeline' : 'Email dismissed');
  } catch (err) { act.disabled = false; toast('Could not save: ' + err.message); }
});

/* EVENTS */
document.addEventListener('click', e => {
  const v = e.target.closest('.nav-item[data-view]');
  if (v) { state.view = v.dataset.view; state.openId = null; state.focusId = null; setTab('jobs'); render(); v.scrollIntoView?.({ inline: 'nearest', block: 'nearest' }); return; }
  const t = e.target.closest('.nav-item[data-tab]');
  if (t) { setTab(t.dataset.tab); return; }
  if (e.target.closest('[data-tg]')) { toggleTelegram(); return; }
  const tgTest = e.target.closest('[data-tg-test]');
  if (tgTest) { sendTelegramTest(tgTest); return; }
  const tgWeekly = e.target.closest('[data-tg-weekly]');
  if (tgWeekly) { sendTelegramTest(tgWeekly, 'weeklySummary'); return; }
  if (e.target.closest('[data-theme-toggle]')) { toggleTheme(); return; }
  const src = e.target.closest('[data-source]');
  if (src) { setFilter('source', src.dataset.source); return; }
  const tm = e.target.closest('[data-time]');
  if (tm) { openTimeMenu(false); setFilter('time', tm.dataset.time); $('#timeBtn').focus(); return; }
  if (e.target.closest('#timeBtn')) { openTimeMenu($('#timeMenu').hidden); return; }
  if (e.target.closest('#postedSort')) { setSort(state.sort === 'newest' ? 'oldest' : 'newest'); return; }
  const s = e.target.closest('[data-sort]');
  if (s) setSort(s.dataset.sort);
});

// A click anywhere outside the time menu closes it
document.addEventListener('pointerdown', e => { if (!e.target.closest('#timeDd')) openTimeMenu(false); });

$('#jobList').addEventListener('click', e => {
  const emptyBtn = e.target.closest('[data-empty]');
  if (emptyBtn) {
    const v = emptyBtn.dataset.empty;
    if (v === 'clear') { state.query = ''; $('#search').value = ''; }
    else if (v === 'filters') { setFilter('source', 'all'); setFilter('time', 'any'); }
    else state.view = v;
    return render();
  }
  const un = e.target.closest('[data-unassign]');
  if (un) { e.stopPropagation(); return unassignEmail(un.dataset.unassign); }
  const actEl = e.target.closest('[data-action]'); if (!actEl) return;
  const row = actEl.closest('.job'); if (!row) return;
  e.stopPropagation();
  doAction(row.dataset.id, actEl.dataset.action);
});

$('#search').addEventListener('input', e => { state.query = e.target.value; state.openId = null; renderNav(); renderHead(); renderFilters(); renderList(); });
$('#markSeen').addEventListener('click', markAllSeen);
$('#checkNow').addEventListener('click', checkNow);

/* Theme (only the theme is remembered, never the view) */
const root = document.documentElement;
function syncThemeIcons() { $$('[data-theme-toggle]').forEach(b => b.innerHTML = root.dataset.theme === 'dark' ? ICON.sun : ICON.moon); }
function toggleTheme() {
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('jr-theme', root.dataset.theme); } catch (e) {}
  syncThemeIcons();
}
try { const t = localStorage.getItem('jr-theme'); if (t) root.dataset.theme = t; else if (matchMedia('(prefers-color-scheme: dark)').matches) root.dataset.theme = 'dark'; } catch (e) {}

/* Keyboard */
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('#timeMenu').hidden) { openTimeMenu(false); $('#timeBtn').focus(); return; }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setTab('jobs'); $('#search').focus(); return; }
  if (state.tab !== 'jobs' || state.loading || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.matches('input, select')) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === '/') { e.preventDefault(); $('#search').focus(); return; }
  const list = visibleJobs(); if (!list.length) return;
  let i = list.findIndex(j => j.id === state.focusId);
  const move = d => {
    i = i < 0 ? 0 : Math.min(list.length - 1, Math.max(0, i + d));
    state.focusId = list[i].id; renderList();
    document.querySelector(`.job[data-id="${CSS.escape(String(state.focusId))}"]`)?.scrollIntoView({ block: 'nearest' });
  };
  const k = e.key.toLowerCase();
  if (k === 'j' || e.key === 'ArrowDown') { e.preventDefault(); return move(1); }
  if (k === 'k' || e.key === 'ArrowUp') { e.preventDefault(); return move(-1); }
  if (i < 0) return;
  if (k === 'escape') { state.openId = null; renderList(); return; }
  const map = { enter: 'toggle', o: 'open', s: 'save', a: 'applied', h: 'hide', c: 'copy' };
  if (map[k]) { e.preventDefault(); doAction(state.focusId, map[k]); }
});

hydrateIcons();
syncThemeIcons();
render();
loadAll();
// Pick up new jobs from background checks; faster while Gemini is scoring.
let ticks = 0;
setInterval(() => {
  ticks++;
  if (jobs.some(j => isActive(j) && scoring(j)) || ticks % 4 === 0) refreshQuiet();
}, 15000);
