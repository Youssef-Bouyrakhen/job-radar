"""Application tracking: replies to your applications from the Gmail label "Bewerbungen".

What it does with your Gmail - the same safe way as the job-alert emails (gmail.py):
- It opens ONLY the label set in config.yaml (applications: label:), read-only, and reads
  emails with BODY.PEEK, so they stay unread. It never replies, moves, labels or deletes.
- Each email is read once (remembered in data/jobradar.db, table app_emails).

For each new email:
1. Is it about an application at all? Newsletters, job alerts and senders in
   applications: ignore_senders are skipped.
2. What kind is it: confirmation, rejection, interview, test or other. Rules first
   ("leider", "Absage" -> rejection; "Vorstellungsgespräch" -> interview); the AI only when
   the rules aren't sure. Interview-looking emails go to the AI first, and the AI requests
   here never stop for the daily AI budget (priority "mail" in ai.py).
3. Which job you marked Applied does it belong to: by the company's name (sender, subject,
   text) and the sender's email domain. No clear match -> the "Needs review" list, nothing
   is changed.
4. The job's status follows its emails: Applied -> Confirmed / Test / Interview / Rejected.
5. Telegram only for interview invites and tests.
"""
import html
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime

from . import ai, db, gmail, telegram
from .config import CONFIG

log = logging.getLogger("jobradar")

KINDS = ("confirmation", "rejection", "interview", "test", "other")
# A job's status is its "furthest" email: a rejection after an interview counts as rejected
RANK = {"confirmation": 1, "test": 2, "interview": 3, "rejection": 4}
LABELS = {"confirmation": "Confirmed", "rejection": "Rejected", "interview": "Interview", "test": "Test", "other": "Other"}


def settings() -> dict:
    return CONFIG["applications"]


def enabled() -> bool:
    return bool(settings().get("enabled", True)) and gmail.configured()


# ---- 1. reading an email ----------------------------------------------------------

def _text(msg) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except Exception:
        return ""
    if body.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(style|script|head).*?</\1>", " ", content)
        content = re.sub(r"<[^>]+>", " ", content)
    content = html.unescape(content)  # plain-text parts sometimes have "&ouml;" too
    content = re.sub(r"https?://\S+", " ", content)  # tracking links: long and useless here
    return re.sub(r"\s+", " ", content).strip()


def parse(raw: bytes) -> dict:
    msg = message_from_bytes(raw, policy=policy.default)
    try:
        received = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        received = db.now()
    return {"sender": str(msg.get("From", ""))[:300], "subject": re.sub(r"\s+", " ", str(msg.get("Subject", "")))[:300],
            "body": _text(msg)[:5000], "received_at": received}


def _address(sender: str) -> str:
    return parseaddr(sender)[1].lower()


def ignored_sender(sender: str) -> bool:
    address = _address(sender)
    domain = address.rpartition("@")[2]
    for entry in settings().get("ignore_senders") or []:
        entry = entry.lower().strip()
        if address == entry or domain == entry or domain.endswith("." + entry):
            return True
    return address == gmail.credentials()[0].lower()  # your own emails (sent to yourself)


# ---- 2. what kind of email ----------------------------------------------------------

def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I)

# About one of YOUR applications (not "join 40 other applicants" or "practise your interview")
ABOUT_APPLICATION = _rx(
    r"\b(deine[rm]?|ihre[rm]?|eure[rm]?|your)\s+(online-?)?(bewerbung|application)", r"bewerbung\s+(als|auf|bei|für|um|über|vom)\b",
    r"eingangsbest[äa]tigung", r"vorstellungsgespr[äa]ch", r"bewerbungsgespr[äa]ch", r"kennenlern(gespr[äa]ch|termin)",
    r"application (was )?(sent|received|viewed)", r"thank you for (your )?(applying|application|interest)",
    r"\bapplied\b", r"\bbewerbungsprozess", r"application process", r"interview (invitation|invite)",
    r"invit\w* (you )?(to|for) (an? |the )?(\w+ )?interview", r"\bbewerbung\b.{0,80}\b(erhalten|eingegangen)")
# Mailings that only look like it
NOT_YOURS = _rx(r"weitere (hochbezahlte )?jobs", r"schließ dich .{0,20}bewerbern an", r"beliebte[rn]? (job|stelle)",
                r"jobs f[üu]r dich", r"passende stellen", r"newsletter", r"üb das interview", r"practi[cs]e (your|the) interview")

# In a subject, enough on its own (an "Einladung" alone could be a party or a webinar)
STRONG_SUBJECT = _rx(r"vorstellungsgespr[äa]ch", r"bewerbungsgespr[äa]ch", r"kennenlerngespr[äa]ch", r"interview (invitation|invite)",
                     r"einladung zu[mr]? (\w+[ -])?(interview|gespr[äa]ch)", r"\babsage\b", r"online-?(test|assessment)",
                     r"coding (challenge|task|test)", r"testaufgabe")
REJECTION = _rx(r"\babsage\b", r"nicht (weiter )?ber[üu]cksichtig", r"nicht (in die )?(engere|n[äa]chste) (auswahl|runde)",
                r"(anderen?|andere[rn]?) (kandidat|bewerber)", r"nicht erfolgreich", r"anderweitig (besetzt|entschieden|vergeben)",
                r"leider (nicht|keine?n?|mitteilen|absagen|m[üu]ssen|haben wir|k[öo]nnen wir|entschieden|fiel)",
                r"gegen (ihre|deine) bewerbung entschieden", r"\bunfortunately\b", r"not (to )?(move|moving) forward",
                r"other candidates", r"not been successful", r"regret to inform", r"decided to (pursue|proceed|move forward) with",
                r"no longer (being )?consider", r"position has been filled")
LEIDER = _rx(r"\bleider\b")
INTERVIEW = _rx(r"vorstellungsgespr[äa]ch", r"bewerbungsgespr[äa]ch", r"kennenlern(gespr[äa]ch|termin|en)", r"\binterview",
                r"einladung (zu[mr]?|f[üu]r)", r"(laden|lade) (sie|dich|euch) .{0,60}\bein\b", r"(telefon|video)(gespr[äa]ch|call|termin)",
                r"pers[öo]nliche[sn]? gespr[äa]ch", r"\b(erste[sn]?|kurze[sn]?) gespr[äa]ch", r"terminvorschl[äa]g",
                r"invit\w* (you )?(to|for)", r"schedule (a|an|your) (call|meeting|interview)", r"calendly|teams-meeting|zoom\.us")
TEST = _rx(r"online-?(test|assessment)", r"\bassessment", r"testaufgabe", r"probeaufgabe", r"aufgabenstellung", r"fallstudie",
           r"case study", r"coding (challenge|task|test|exercise)", r"take-?home", r"hackerrank|codility|coderbyte|testgorilla|codesignal",
           r"\b(aufgabe|task) (zur|zu|for) (bearbeitung|you|complete)", r"programmieraufgabe", r"persönlichkeitstest|eignungstest")
CONFIRMATION = _rx(r"eingangsbest[äa]tigung", r"(ist|sind) (bei uns )?(gut )?(eingegangen|angekommen)", r"\b(haben|hat) .{0,60}\berhalten\b",
                   r"(erfolgreich )?(versendet|gesendet|übermittelt|abgeschickt)", r"\bist raus\b", r"im kasten",
                   r"(has been|was|been) (successfully )?(received|sent|submitted)", r"thank you for (applying|your application)",
                   r"(vielen )?dank f(ü|ue|u)r (ihre|deine|die) (bewerbung|zusendung)", r"we (have )?received your",
                   r"(bewerbung|unterlagen) (wurde |werden )?(weitergeleitet|pr(ü|ue)fen)", r"weitergeleitet")
# Account and portal mails: neither a reply nor a step forward
OTHER = _rx(r"passwort", r"password", r"zugangsdaten", r"access data", r"career-?portal", r"karriereportal",
            r"vervollst[äa]ndige", r"erg[äa]nze", r"angefangene bewerbung", r"bewerbung best[äa]tigen", r"confirm your (application|email)",
            r"was viewed", r"wurde angesehen", r"hat (ihre|deine) bewerbung angesehen", r"heben sie sich", r"stand out",
            r"anzeige .{0,80}(nicht mehr|abgelaufen|geschlossen)", r"wurde geschlossen", r"no longer accepting",
            r"finish (editing|your application)")


def rule_kind(email: dict) -> str | None:
    """confirmation / rejection / interview / test / other, None = the rules aren't sure (ask the AI),
    "" = not about one of your applications at all."""
    subject, text = email["subject"], f"{email['subject']} \n {email['body']}"
    if NOT_YOURS.search(subject) or not (ABOUT_APPLICATION.search(text) or STRONG_SUBJECT.search(subject)):
        return ""
    if NOT_YOURS.search(text[:600]) and not ABOUT_APPLICATION.search(subject):
        return ""
    # The subject alone is a strong sign ("Einladung zum Vorstellungsgespräch", "Absage")
    hits = {k for k, rx in (("rejection", REJECTION), ("interview", INTERVIEW), ("test", TEST)) if rx.search(subject)}
    if len(hits) == 1 and not (hits == {"interview"} and OTHER.search(subject)):
        return hits.pop()
    if hits:
        return None
    if OTHER.search(subject):
        return "other"
    rejection, interview, test = REJECTION.search(text), INTERVIEW.search(text), TEST.search(text)
    confirmation = CONFIRMATION.search(text)
    if rejection and not (interview or test):
        return "rejection"
    if LEIDER.search(text) and not (interview or test or confirmation):
        return "rejection"
    # A confirmation often says "if it fits, we will invite you to an interview": unsure
    if (interview or test or LEIDER.search(text)) and not rejection:
        if confirmation or (interview and test):
            return None
        return "interview" if interview else "test" if test else None
    if rejection:
        return None  # rejection AND interview words
    if confirmation:
        return "confirmation"
    if OTHER.search(text[:1500]):
        return "other"
    return None


AI_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "kind": {"type": "STRING", "enum": list(KINDS)},
            "company": {"type": "STRING"},
            "sure": {"type": "BOOLEAN"},
        },
        "required": ["id", "kind", "company", "sure"],
    },
}

AI_PROMPT = """You sort emails that a student received about their job applications (Werkstudent jobs).
For each email decide:
- kind:
  "confirmation"  the application was received / sent (also "thank you for applying", "we will get back to you")
  "rejection"     the employer says no (also "we decided for other candidates", "position filled")
  "interview"     an invitation to an interview, phone or video call or meeting with the employer,
                  or a request to pick a date for one
  "test"          an online test, coding task, case study or assessment the person has to do
  "other"         anything else: newsletters, job ads, account / password emails, reminders to finish
                  an application, "your application was viewed", general news
  A confirmation that only says an interview MAY follow later is a "confirmation".
- company: the employer the application is for, as written in the email ("" if unclear).
  Not the job portal or applicant system (not Indeed, LinkedIn, StepStone, Personio, softgarden, ...).
- sure: true only if the kind is clear from the email.

EMAILS (JSON)
{emails}
"""


def ai_kinds(emails: list[dict]) -> dict[str, dict]:
    """gmail_id -> {kind, company, sure}. Raises ai.RateLimited / Overloaded when no AI answers."""
    payload = [{"id": e["gmail_id"], "from": e["sender"], "subject": e["subject"], "text": e["body"][:2500]}
               for e in emails]
    answer = ai.ask_json(AI_PROMPT.format(emails=json.dumps(payload, ensure_ascii=False)), AI_SCHEMA,
                         temperature=0.1, priority="mail")
    out = {}
    for a in answer if isinstance(answer, list) else []:
        if isinstance(a, dict) and a.get("kind") in KINDS:
            out[str(a.get("id"))] = {"kind": a["kind"], "company": str(a.get("company") or "")[:120],
                                     "sure": bool(a.get("sure"))}
    return out


# ---- 3. which applied job ---------------------------------------------------------------

# Parts of sender addresses and names that say nothing about the employer
SENDER_NOISE = {
    "noreply", "no", "reply", "donotreply", "mail", "email", "e", "m", "info", "news", "jobs", "job", "karriere",
    "career", "careers", "recruiting", "recruitment", "recruiter", "bewerbung", "bewerbungen", "bewerber",
    "bewerberbetreuung", "talent", "team", "hr", "personal", "application", "applications", "notification",
    "notifications", "via", "von", "from", "de", "com", "net", "org", "io", "eu", "jobs", "apply", "status", "update",
    "personio", "softgarden", "workwise", "indeed", "indeedapply", "linkedin", "stepstone", "join", "onlyfy",
    "concludis", "hrlab", "beesite", "successfactors", "workday", "myworkdayjobs", "greenhouse", "lever",
    "smartrecruiters", "recruitee", "teamtailor", "dvinci", "rexx", "umantis", "jobvalley", "ihre", "deine", "your",
}


def _words(text: str) -> list[str]:
    from .discover import _fold
    return re.sub(r"[^a-z0-9]+", " ", _fold(text)).split()


def _keys(company: str) -> set[str]:
    """Ways the company's name can appear: 'DZ BANK AG' -> {'dzbank'}; 'Poly-clip System GmbH' -> {'polyclipsystem', 'polyclip'}."""
    from .companies import clean_name
    from .discover import _words as name_words
    company = re.sub(r"\([^)]*\)", " ", company).strip()  # "LocateRisk UG (haftungsbeschränkt)", "... GmbH (DTVG)"
    if re.fullmatch(r"[\w-]+(\.[\w-]+)*\.[a-z]{2,}", company.strip().lower()):  # "kfw.de" -> "kfw"
        company = re.sub(r"-(recruiting|jobs|karriere|careers?)$", "", company.strip().lower().split(".")[0])
    keys = set()
    for name in {company, clean_name(company)}:
        words = name_words(name)  # without GmbH, AG, Group, ...
        if words:
            keys.add("".join(words))
            if len(words) > 2 and len("".join(words[:2])) >= 6:
                keys.add("".join(words[:2]))
    keys = {k for k in keys if len(k) >= 3}
    # "KI Group" without "Group" would be "ki": too short, keep the whole name
    return keys or {k for k in ["".join(_words(company))] if len(k) >= 3}


def _mentions(words: list[str], key: str) -> bool:
    """Is the key one word, or a few words in a row ('dz' 'bank' = 'dzbank')?"""
    for i, w in enumerate(words):
        if not key.startswith(w):
            continue
        joined = w
        for nxt in words[i + 1:i + 4]:
            if joined == key:
                break
            joined += nxt
            if not key.startswith(joined):
                break
        if joined == key:
            return True
    return False


def _sender_words(sender: str) -> list[str]:
    name, address = parseaddr(sender)
    local, _, domain = address.lower().partition("@")
    name = re.sub(r"\(via [^)]*\)", " ", name, flags=re.I)
    words = _words(name) + ["|"] + _words(local) + ["|"] + _words(domain.replace(".", " . "))
    return [w for w in words if w not in SENDER_NOISE]


def _title_words(title: str) -> set[str]:
    title = re.sub(r"\((?:[mwfdx]\s*/\s*)+[mwfdx]\)|\b[mwfd]/[mwfd]/[mwfd]\b|\*in\b|:in\b", " ", title, flags=re.I)
    return {w for w in _words(title) if len(w) >= 3 and w not in {"werkstudent", "werkstudentin", "working", "student", "bereich", "und", "the", "for", "mit"}}


def match(email: dict, jobs: list[dict], ai_company: str = "") -> tuple[int | None, int | None, str]:
    """(job id, best guess, note). Job id only when the match is clear; otherwise the best guess
    (may be None) and why it needs review."""
    if not jobs:
        return None, None, "You have no jobs marked Applied yet"
    subject, sender = _words(email["subject"]), _sender_words(email["sender"])
    body = _words(email["body"])
    by_company: dict[str, list[dict]] = {}
    keys: dict[str, set[str]] = {}
    for j in jobs:
        by_company.setdefault(j["company_key"], []).append(j)
        keys.setdefault(j["company_key"], set()).update(j["keys"])

    def found_in(words):
        return {c for c in by_company if any(_mentions(words, k) for k in keys[c])}

    # Subject and sender (name, address, domain) first: a portal email's text often lists other employers too
    companies = found_in(subject) | found_in(sender)
    if not companies:
        companies = found_in(body)
    if not companies and ai_company:
        from .discover import _same
        ai_keys = _keys(ai_company)
        companies = {c for c in by_company if any(_same(a, k) for a in ai_keys for k in keys[c])}
    if not companies:
        # Some portals (StepStone) only name the job: a guess for you to confirm, never automatic
        text = set(subject) | set(body[:80])
        fits = [j for j in jobs if j["title_words"] and len(j["title_words"] & text) / len(j["title_words"]) >= 0.8]
        if len(fits) == 1:
            return None, fits[0]["id"], f"No company named; the job title fits {fits[0]['company']}"
        return None, None, "No job marked Applied matches this company"
    if len(companies) > 1:
        names = sorted({by_company[c][0]["company"] for c in companies})
        return None, None, f"Could be several companies: {', '.join(names[:4])}"
    candidates = by_company[companies.pop()]
    if len(candidates) == 1:
        return candidates[0]["id"], candidates[0]["id"], ""
    # Several applications at the same company: the job title decides
    text = set(subject) | set(body)
    scored = sorted(((len(j["title_words"] & text) / max(1, len(j["title_words"])), j) for j in candidates),
                    key=lambda x: -x[0])
    (best, job), (second, _) = scored[0], scored[1]
    if best >= 0.6 and best - second >= 0.25:
        return job["id"], job["id"], ""
    return None, job["id"], f"{len(candidates)} applied jobs at {job['company']}: which one?"


def applied_jobs() -> list[dict]:
    jobs = db.applied_jobs()
    for j in jobs:
        j["keys"] = _keys(j["company"] or "")
        j["company_key"] = min(j["keys"], key=len, default=f"#{j['id']}")
        j["title_words"] = _title_words(j["title"])
    return [j for j in jobs if j["keys"]]


# ---- the check ------------------------------------------------------------------------

_lock = threading.Lock()
_ai_failures: dict[str, int] = {}   # gmail id -> runs in which the AI didn't answer (review after 3)
_last: dict = {}                    # result of the last check, for the dashboard


def last_result() -> dict:
    return dict(_last)


def check() -> dict:
    """Read new emails in the label, sort and match them. Returns counts (also kept for the dashboard)."""
    if not enabled():
        return {"ok": False, "note": "Off: no Gmail login in .env, or applications: enabled: false"}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "note": "Already running"}
    try:
        result = _check()
    except gmail.GmailError as e:
        result = {"ok": False, "note": str(e)}
    except Exception as e:
        log.exception("Application emails: check failed")
        result = {"ok": False, "note": f"{type(e).__name__}: {str(e)[:150]}"}
    finally:
        _lock.release()
    _last.clear()
    _last.update(result, at=db.now())
    return result


def _check() -> dict:
    cfg = settings()
    imap = gmail.open_label(cfg["label"])
    emails = []
    try:
        ids = gmail.ids_since(imap, cfg["days_back"])
        known = db.known_app_emails(list(ids.values()))
        for uid, gid in [(u, g) for u, g in ids.items() if g not in known][-cfg["max_emails_per_check"]:]:
            raw = gmail.fetch(imap, uid)
            if raw is not None:
                emails.append({"gmail_id": gid, **parse(raw)})
    finally:
        gmail._logout(imap)

    counts = {"ok": True, "read": len(emails), "matched": 0, "review": 0, "ignored": 0}
    jobs = applied_jobs()
    unsure = []
    for e in emails:
        kind = "" if ignored_sender(e["sender"]) else rule_kind(e)
        if kind == "":
            db.save_app_email({**e, "body": "", "kind": None, "kind_from": "rules", "state": "ignored"})
            counts["ignored"] += 1
        elif kind is None:
            unsure.append(e)
        else:
            e.update(kind=kind, kind_from="rules")
            _file(e, counts, jobs)
    # Interview-looking emails first, so they get the AI before anything else
    unsure.sort(key=lambda e: not (INTERVIEW.search(e["subject"] + " " + e["body"]) or TEST.search(e["subject"])))
    for i in range(0, len(unsure), 8):
        batch = unsure[i:i + 8]
        answers, error = {}, None
        if ai.enabled():
            try:
                answers = ai_kinds(batch)
            except Exception as ex:  # rate limits, busy servers, a broken answer: try again next round
                error = ex
                log.warning("Application emails: AI did not answer (%s)", str(ex)[:150])
        for e in batch:
            a = answers.get(e["gmail_id"])
            if a is None and ai.enabled() and _ai_failures.get(e["gmail_id"], 0) < 2:
                _ai_failures[e["gmail_id"]] = _ai_failures.get(e["gmail_id"], 0) + 1
                continue  # not saved: read again next round
            _ai_failures.pop(e["gmail_id"], None)
            if a is None:
                e.update(kind="other", kind_from="rules",
                         note="Unclear, and the AI could not help" + (f" ({type(error).__name__})" if error else ""))
            else:
                e.update(kind=a["kind"], kind_from="ai", company=a["company"])
                if not a["sure"]:
                    e["note"] = f"The AI isn't sure it's \"{LABELS[a['kind']]}\""
            _file(e, counts, jobs)
    if counts["matched"]:
        refresh_statuses()
    notify()
    if emails:
        log.info("Application emails: %d new, %d matched to a job, %d need review, %d not about an application",
                 counts["read"], counts["matched"], counts["review"], counts["ignored"])
    return counts


def _file(e: dict, counts: dict, jobs: list[dict]) -> None:
    """Match one sorted email to a job and save it (matched, or Needs review)."""
    job_id, guess, why = match(e, jobs, e.get("company", ""))
    if e["kind"] == "other" and job_id is None and not e.get("note"):
        state, note = "ignored", None   # an unmatched portal / account email: nothing to review
    elif job_id is not None and not e.get("note"):
        state, note = "matched", None
    else:
        state, note = "review", e.get("note") or why
    db.save_app_email({**e, "job_id": job_id or guess, "state": state, "note": note})
    counts["matched" if state == "matched" else "review" if state == "review" else "ignored"] += 1


def rematch_review() -> int:
    """Emails in Needs review only because no job was marked Applied yet (the confirmation often
    comes before you click Applied): try them again. Returns how many found their job."""
    jobs = applied_jobs()
    done = 0
    for e in db.app_emails(state="review"):
        if e["kind_from"] == "you" or not (e["note"] or "").startswith(("No job marked", "You have no jobs")):
            continue
        job_id, _, _ = match(e, jobs, e.get("company") or "")
        if job_id is not None:
            db.update_app_email(e["gmail_id"], {"job_id": job_id, "state": "matched", "note": None})
            done += 1
    if done:
        refresh_statuses()
    return done


def refresh_statuses() -> None:
    """Every applied job's status = its furthest matched email (Confirmed < Test < Interview < Rejected)."""
    best: dict[int, str] = {}
    for e in db.app_emails(state="matched"):
        if e["kind"] in RANK and RANK[e["kind"]] > RANK.get(best.get(e["job_id"]), 0):
            best[e["job_id"]] = e["kind"]
    db.set_app_statuses(best)


# ---- what you do in the dashboard ---------------------------------------------------------

def assign(gmail_id: str, job_id: int | None, kind: str | None = None) -> bool:
    """Needs review -> this job (job_id None: dismiss it). Also used for "not this job"."""
    if not db.app_email(gmail_id):
        return False
    fields = {"state": "matched" if job_id else "dismissed", "note": None}
    if job_id:
        fields["job_id"] = job_id
    if kind:
        fields.update(kind=kind, kind_from="you")
    db.update_app_email(gmail_id, fields)
    refresh_statuses()
    notify()
    return True


def unassign(gmail_id: str) -> bool:
    if not db.app_email(gmail_id):
        return False
    db.update_app_email(gmail_id, {"state": "review", "note": "You said: not this job", "kind_from": "you"})
    refresh_statuses()
    return True


def overview() -> dict:
    """For the Applied page: per job its timeline, the stats (also per category) and Needs review."""
    from . import categories, must_apply
    by_key = {c["name_key"]: c for c in db.company_rows()}
    jobs = db.applied_jobs()
    timelines: dict[int, list[dict]] = {}
    for e in db.app_emails(state="matched"):
        timelines.setdefault(e["job_id"], []).append(
            {"gmail_id": e["gmail_id"], "at": e["received_at"], "kind": e["kind"], "subject": e["subject"],
             "sender": parseaddr(e["sender"])[0] or _address(e["sender"]), "by": e["kind_from"]})
    per_job, stats = {}, {}
    for j in jobs:
        company = must_apply._company(j, by_key)
        category = (company or {}).get("category") or "other"
        per_job[j["id"]] = {"category": category, "category_label": categories.LABELS.get(category, "Other"),
                            "timeline": sorted(timelines.get(j["id"], []), key=lambda t: t["at"] or "")}
        for key in ("all", category):
            s = stats.setdefault(key, {"applied": 0, "confirmation": 0, "rejection": 0, "interview": 0, "test": 0})
            s["applied"] += 1
            if j["app_status"] in s:
                s[j["app_status"]] += 1
    review = [{"gmail_id": e["gmail_id"], "at": e["received_at"], "kind": e["kind"], "subject": e["subject"],
               "sender": e["sender"], "note": e["note"], "guess": e["job_id"], "snippet": (e["body"] or "")[:300]}
              for e in db.app_emails(state="review")]
    return {"jobs": per_job, "stats": stats, "category_labels": categories.LABELS,
            "review": sorted(review, key=lambda r: r["at"] or "", reverse=True),
            "label": settings()["label"], "enabled": enabled(), "last": last_result()}


# ---- 5. Telegram ----------------------------------------------------------------------

def sender_name(sender: str) -> str:
    """'EHBS | Karriere' -> 'EHBS'; 'Poly-clip System GmbH (via softgarden)' -> 'Poly-clip System GmbH'."""
    name, address = parseaddr(sender)
    name = re.split(r"\s[|–-]\s", re.sub(r"\(via [^)]*\)", "", name, flags=re.I))[0]
    name = re.sub(r"\s+(karriere|recruiting|bewerbungen|bewerberbetreuung|talent team|hr|jobs|careers?)$", "", name.strip(), flags=re.I)
    return name.strip() or address


def message(e: dict) -> str:
    esc = lambda s: html.escape(s or "")
    company = e.get("job_company") or e.get("company") or sender_name(e["sender"])
    head = (f"🎉 <b>Interview invite from {esc(company)}</b>" if e["kind"] == "interview"
            else f"📝 <b>Test / task from {esc(company)}</b>")
    lines = [head, f"✉️ {esc(e['subject'])}"]
    if e.get("job_title"):
        lines.append(f"<a href=\"{esc(e['job_url'])}\">{esc(e['job_title'])}</a>")
    else:
        lines.append("<i>Not matched to a job yet: see Needs review on the Applied page</i>")
    return "\n".join(lines)


def notify() -> int:
    """🎉 / 📝 for new interview invites and tests (not for old emails read the first time)."""
    if not telegram.enabled():
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=settings()["telegram_max_age_days"])).isoformat(timespec="seconds")
    sent = 0
    for e in db.app_emails_to_notify(since):
        try:
            telegram.send(message(e))
        except telegram.TelegramError as ex:
            log.warning("Telegram: interview / test message failed: %s", ex)
            break
        db.update_app_email(e["gmail_id"], {"notified": 1})
        sent += 1
    return sent


def check_in_background() -> None:
    if enabled():
        threading.Thread(target=lambda: (rematch_review(), check()), daemon=True, name="applications").start()


def scheduled() -> None:
    rematch_review()
    check()
