"""Application tracker: Job Radar builds your list of applications from Gmail by itself.

Where the applications come from (nothing to import or copy):
- Sent: every email in your Sent folder that is an application ("Bewerbung", "Initiativbewerbung",
  "application" in the subject or the first lines). One application per email; a second email to the
  same company about the same thing (a reminder, a reply in the thread) is not counted again.
- Portals: you applied on a company's website, so there is no sent email. Its confirmation
  ("Eingangsbestätigung", "Deine Bewerbung als ...") or rejection that matches no application starts one.
- Jobs you marked Applied in Job Radar (linked to the email application when it is the same company).
Each one is a "Job ad" or an "Initiativ" application: Initiativ when the subject or text says
Initiativbewerbung / initiative application. They are counted separately.

The replies, read the same safe way as the job-alert emails (gmail.py): read-only, BODY.PEEK (emails stay
unread), never replied to, moved, labelled or deleted, each email read once (table app_emails). Searched in
All Mail: your applications' threads, the companies' email domains, the "Bewerbungen" label, and
"Bewerbung" / "application" in the subject.

For each reply:
1. What kind is it: confirmation (automatic "we received it", NOT an answer), reply (a person answered),
   rejection, interview (also a phone / Teams call), test, offer, other. Rules first, German + English;
   the free AI only when the rules aren't sure ("other" part of the AI budget, ai.py).
2. Which application: the Gmail thread first, then the sender's domain, then the company's name, the job
   title and a reference number. Only what stays unclear goes to "Needs review".
3. The application's status follows its replies; you can always set it yourself in the dropdown.
4. Telegram for interview invites, tests and offers.
"""
import html
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from . import ai, clock, db, gmail, telegram
from .config import CONFIG

log = logging.getLogger("jobradar")

KINDS = ("confirmation", "reply", "rejection", "interview", "test", "offer", "other")
LABELS = {"sent": "Sent", "confirmation": "Confirmed", "reply": "Answered", "rejection": "Rejected",
          "interview": "Interview", "test": "Test", "offer": "Offer", "other": "Other"}
STATUSES = ("sent", "confirmation", "reply", "test", "interview", "offer", "rejection")
# A real answer from the company (an automatic confirmation is not)
ANSWERS = {"reply", "test", "interview", "offer", "rejection"}
DECISIVE = {"test", "interview", "offer", "rejection"}
TYPES = {"job": "Job ad", "initiativ": "Initiativ"}


def settings() -> dict:
    return CONFIG["applications"]


def enabled() -> bool:
    return bool(settings().get("enabled", True)) and gmail.configured()


def start_date():
    """The tracker looks at emails from this day on: tracker_since (when your search started), at most
    tracker_days_back days ago."""
    start = clock.today() - timedelta(days=int(settings().get("tracker_days_back") or 365))
    since = str(settings().get("tracker_since") or "").strip()
    try:
        return max(start, datetime.strptime(since, "%Y-%m-%d").date()) if since else start
    except ValueError:
        return start


# ---- reading an email ----------------------------------------------------------

def _text(msg, prefer=("plain", "html")) -> str:
    body = msg.get_body(preferencelist=prefer)
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
    to = [a.lower() for _, a in getaddresses([str(msg.get("To", ""))]) if "@" in a]
    text = _text(msg)
    if len(text) < 400:  # some portals (Indeed) only name the company in the HTML part
        text = f"{text} {_text(msg, ('html',))}".strip()
    return {"sender": str(msg.get("From", ""))[:300], "subject": re.sub(r"\s+", " ", str(msg.get("Subject", "")))[:300],
            "body": text[:5000], "received_at": received, "to": to, "bulk": bool(msg.get("List-Unsubscribe"))}


def _address(sender: str) -> str:
    return parseaddr(sender)[1].lower()


def _me() -> str:
    return gmail.credentials()[0].lower()


def ignored_sender(sender: str) -> bool:
    address = _address(sender)
    domain = address.rpartition("@")[2]
    for entry in settings().get("ignore_senders") or []:
        entry = entry.lower().strip()
        if address == entry or domain == entry or domain.endswith("." + entry):
            return True
    return address == _me()  # your own emails


# Job-ad mailers: newsletters with "jobs for you", never an answer to an application
NEWSLETTER_DOMAINS = {"jobleads.com", "jobleads.de", "jooble.org", "joblift.de", "jobrapido.com", "talent.com",
                      "adzuna.de", "ziprecruiter.com", "whatjobs.com", "kimeta.de"}


def newsletter(e: dict, known: bool) -> bool:
    """A job-ad mailer, or a mass mail (it has an unsubscribe header) that is not about one of your applications.
    Not when it comes from a company you applied to, or when the subject says it is about your application."""
    if registrable(_address(e["sender"]).rpartition("@")[2] or "x.x") in NEWSLETTER_DOMAINS:
        return True
    return bool(e.get("bulk")) and not known and not (ABOUT_APPLICATION.search(e["subject"]) or STRONG_SUBJECT.search(e["subject"]))


# ---- the company's email domain ---------------------------------------------------

# Applicant systems and free mail: many companies share the domain, so the address's name part counts
SHARED_HOSTS = {
    "personio.de", "personio.com", "softgarden.io", "softgarden.de", "concludis.de", "successfactors.eu",
    "successfactors.com", "myworkday.com", "myworkdayjobs.com", "join.com", "onlyfy.jobs", "hrlab.de", "beesite.de",
    "workwise.io", "greenhouse.io", "lever.co", "smartrecruiters.com", "recruitee.com", "teamtailor.com",
    "teamtailor-mail.com", "recruitee-mail.com", "recruitee-mailbox.com", "hrworks.de", "dvinci.de", "dvinci-hr.com", "rexx-systems.com", "umantis.com", "jobylon.com",
    "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "outlook.com", "outlook.de", "hotmail.com",
    "hotmail.de", "yahoo.com", "yahoo.de", "t-online.de", "icloud.com", "posteo.de", "mailbox.org",
}
# Job portals: the email is about some company's job, the portal's domain says nothing about which
JOB_PORTALS = {"indeed.com", "indeed.de", "linkedin.com", "stepstone.de", "stepstone.com", "xing.com", "glassdoor.de",
               "glassdoor.com", "monster.de", "jobware.de", "kununu.com", "arbeitsagentur.de", "jobvalley.com"}
SUB_NOISE = {"m", "mail", "mails", "msg", "app", "email", "e", "careers", "career", "jobs", "recruitment", "notifications",
             "notification", "noreply", "no-reply", "www", "eu", "de", "news"}
LOCAL_NOISE = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|system|mailer|info|jobs?|careers?|karriere|"
                         r"recruiting|bewerbung(en)?|hr|talent|apply|application|reply|hello|hallo|kontakt|contact|office|"
                         r"status-?update|updates?)$")


def registrable(domain: str) -> str:
    """mail.careers.example-firma.com -> example-firma.com; foo.co.uk -> foo.co.uk."""
    labels = domain.lower().strip(".").split(".")
    n = 3 if len(labels) > 2 and labels[-2] in ("co", "com", "org", "ac", "gv") and len(labels[-1]) == 2 else 2
    return ".".join(labels[-n:])


def contact_key(address: str) -> str | None:
    """What identifies the company in an email address: its domain ("muster.de"), or for a shared applicant
    system / free mail the name part ("musterfirma@personio.de"). None = says nothing about the company."""
    local, _, domain = (address or "").lower().partition("@")
    if not domain:
        return None
    host = registrable(domain)
    if host in JOB_PORTALS:
        return None
    if host not in SHARED_HOSTS:
        return host
    # "jobs@muster-bank.beesite.de": the company is in the sub-domain
    subs = [s for s in domain[:-len(host)].strip(".").split(".") if s and s not in SUB_NOISE]
    if subs:
        return f"{subs[-1]}@{host}"
    if host.startswith("recruitee"):  # "job.ab12.cd34.mustergmbh@", "anna.mustergmbh@"
        local = local.split(".")[-1]
    slug = re.sub(r"^(no-?reply|do-?not-?reply|nicht-antworten)[-_.]", "", local)
    slug = re.sub(r"[-_.](jobs?|recruiting|karriere|careers?|bewerbung(en)?|hr|talent)$", "", slug)
    if not slug or LOCAL_NOISE.match(slug) or "." in slug or "+" in slug:  # "anna.schmidt": a person, not a company
        return None
    return f"{slug}@{host}"


def _search_term(key: str, contact: str) -> str:
    """For the All Mail search: the domain, or the whole address for a shared one."""
    return contact if "@" in key else key


# ---- 1. what kind of email ----------------------------------------------------------

def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I)

# About one of YOUR applications (not "join 40 other applicants" or "practise your interview")
ABOUT_APPLICATION = _rx(
    r"\b(deine[rm]?|ihre[rm]?|eure[rm]?|your)\s+(online-?)?(initiativ)?(bewerbung|application)", r"bewerbung\s+(als|auf|bei|für|um|über|vom)\b",
    r"eingangsbest[äa]tigung", r"vorstellungsgespr[äa]ch", r"bewerbungsgespr[äa]ch", r"kennenlern(gespr[äa]ch|termin)",
    r"application (was )?(sent|received|viewed)", r"thank you for (your )?(applying|application|interest)",
    r"\bapplied\b", r"\bbewerbungsprozess", r"application process", r"interview (invitation|invite)",
    r"invit\w* (you )?(to|for) (an? |the )?(\w+ )?interview", r"\bbewerbung\b.{0,80}\b(erhalten|eingegangen)",
    r"initiativbewerbung")
# Mailings that only look like it
NOT_YOURS = _rx(r"weitere (hochbezahlte )?jobs", r"lebenslauf-?check", r"cv-?check", r"schließ dich .{0,20}bewerbern an", r"beliebte[rn]? (job|stelle)",
                r"jobs f[üu]r dich", r"passende stellen", r"newsletter", r"üb das interview", r"practi[cs]e (your|the) interview")

# In a subject, enough on its own (an "Einladung" alone could be a party or a webinar)
STRONG_SUBJECT = _rx(r"vorstellungsgespr[äa]ch", r"bewerbungsgespr[äa]ch", r"kennenlerngespr[äa]ch", r"interview (invitation|invite)",
                     r"einladung zu[mr]? (\w+[ -])?(interview|gespr[äa]ch)", r"\babsage\b", r"online-?(test|assessment)",
                     r"coding (challenge|task|test)", r"testaufgabe", r"vertragsangebot", r"job offer", r"offer letter")
REJECTION = _rx(r"\babsage\b", r"nicht (weiter )?ber[üu]cksichtig", r"nicht (in die )?(engere|n[äa]chste) (auswahl|runde)",
                r"(anderen?|andere[rn]?) (kandidat|bewerber)", r"nicht erfolgreich", r"anderweitig (besetzt|entschieden|vergeben)",
                r"leider (nicht(?! immer)|keine?n?|mitteilen|absagen|m[üu]ssen|haben wir|k[öo]nnen wir|entschieden|fiel)",
                r"gegen (ihre|deine) bewerbung entschieden", r"\bunfortunately\b", r"not (to )?(move|moving) forward",
                r"other candidates", r"not been successful", r"regret to inform", r"decided to (pursue|proceed|move forward) with",
                r"no longer (being )?consider", r"position has been filled", r"keine (passende|offene|freie)n? (stelle|position|vakanz)",
                r"derzeit keine (stelle|position|vakanz|möglichkeit|bedarf)", r"kein(en)? bedarf")
LEIDER = _rx(r"\bleider\b")
INTERVIEW = _rx(r"vorstellungsgespr[äa]ch", r"bewerbungsgespr[äa]ch", r"kennenlern(gespr[äa]ch|termin|en)", r"\binterview",
                r"vorgespr[äa]ch", r"teams-?(call|meeting|termin|gespr[äa]ch)", r"video-?call",
                r"einladung (zu[mr]?|f[üu]r)", r"(laden|lade) (sie|dich|euch) .{0,60}\bein\b", r"(telefon|video)(gespr[äa]ch|call|termin|interview)",
                r"pers[öo]nliche[sn]? gespr[äa]ch", r"\b(erste[sn]?|kurze[sn]?) gespr[äa]ch", r"terminvorschl[äa]g",
                r"invit\w* (you )?(to|for)", r"schedule (a|an|your) (call|meeting|interview)",
                r"calendly|teams-meeting|microsoft teams|zoom\.us|google meet|meet\.google|webex")
TEST = _rx(r"online-?(test|assessment)", r"\bassessment", r"testaufgabe", r"probeaufgabe", r"aufgabenstellung", r"fallstudie",
           r"case study", r"coding (challenge|task|test|exercise)", r"take-?home", r"hackerrank|codility|coderbyte|testgorilla|codesignal",
           r"\b(aufgabe|task) (zur|zu|for) (bearbeitung|you|complete)", r"programmieraufgabe", r"persönlichkeitstest|eignungstest")
OFFER = _rx(r"vertragsangebot", r"arbeitsvertrag", r"\bzusage\b", r"(freuen uns|m[öo]chten) .{0,60}(ihnen|dir) .{0,40}(anbieten|zusagen)",
            r"angebot f[üu]r (eine|die) (stelle|position|t[äa]tigkeit|werkstudent)", r"job offer", r"offer letter",
            r"(pleased|happy|delighted) to offer", r"we would like to offer you", r"herzlich willkommen im team",
            r"welcome (to|on) (the|our) team")
CONFIRMATION = _rx(r"eingangsbest[äa]tigung", r"(ist|sind) (bei uns )?(gut )?(eingegangen|angekommen)", r"\b(haben|hat) .{0,60}\berhalten\b",
                   r"(erfolgreich )?(versendet|gesendet|übermittelt|abgeschickt)", r"\bist raus\b", r"im kasten",
                   r"(has been|was|been) (successfully )?(received|sent|submitted)", r"thank you for (applying|your application)",
                   r"(vielen )?dank f(ü|ue|u)r (ihre|deine|die) (initiativ)?(bewerbung|zusendung)", r"we (have )?received your",
                   r"(bewerbung|unterlagen) (wurde |werden )?(weitergeleitet|pr(ü|ue)fen)", r"weitergeleitet", r"received\b")
# Account and portal mails: neither a reply nor a step forward
OTHER = _rx(r"passwort", r"password", r"zugangsdaten", r"access data", r"career-?portal", r"karriereportal",
            r"vervollst[äa]ndige", r"erg[äa]nze", r"angefangene bewerbung", r"bewerbung best[äa]tigen", r"confirm your (application|email)",
            r"was viewed", r"wurde angesehen", r"hat (ihre|deine) bewerbung angesehen", r"heben sie sich", r"stand out",
            r"anzeige .{0,80}(nicht mehr|abgelaufen|geschlossen)", r"wurde geschlossen", r"no longer accepting",
            r"finish (editing|your application)", r"abwesend|out of office|automatische antwort|auto-?reply|abwesenheit")
# "If your profile fits, we will invite you": an interview that MAY come, said in a confirmation
CONDITIONAL = _rx(r"\b(sollte|sollten|falls|wenn|sobald)\b[^.]{0,140}(einladen|einladung|gespr[äa]ch|interview|melden|kontaktieren)",
                  r"(werden|würden) (uns|wir)[^.]{0,80}(melden|einladen|kontaktieren|in verbindung setzen)",
                  r"\b(if|once|should)\b[^.]{0,140}(invite|interview|contact you|get back|reach out)",
                  r"we will (be in touch|get back|contact you|reach out)", r"melden uns (zeitnah|schnellstm[öo]glich|bald|bei)")
IF_IT_FITS = _rx(r"\b(sollte|sollten|falls|wenn|sobald)\b[^.]{0,140}(einladen|einladung|gespr[äa]ch|interview)",
                 r"\b(if|once|should)\b[^.]{0,140}(invite|interview)")
# A concrete invitation, even inside a "thank you for your application" mail ("Gerne möchten wir dich zu einem
# Teams-Call einladen. Wie schaut es bei dir am Montag aus", "möchten gerne einen Termin vereinbaren")
INVITE = _rx(r"(m[öo]chten|w[üu]rden|wollen) (wir )?(sie|dich|euch)? ?(gerne |gern )?(zu|für) .{0,60}einladen",
             r"gerne (m[öo]chten|w[üu]rden) wir (sie|dich|euch) .{0,60}einladen",
             r"(m[öo]chten|w[üu]rden) (gerne |gern )?(mit (ihnen|dir) )?(einen|ein) termin .{0,60}vereinbaren",
             r"(laden|lade) (sie|dich|euch) (herzlich |gerne )?(zu|für) .{0,60}\bein\b",
             r"(wie|wann) (schaut|sieht) es bei (ihnen|dir) .{0,40}aus", r"termin (buchen|ausw[äa]hlen|vereinbaren)",
             r"(we would like|we'd like|we want) to invite you", r"schedule (a|an|your) (call|meeting|interview)")
# "We only take applications through our careers portal": an answer (you have to do something), not a no
PORTAL_ONLY = _rx(r"(ausschlie(ß|ss)lich|nur) (über|via|per) (unser|unsere|das|die)[^.]{0,30}(portal|formular|website|webseite)",
                  r"bewirb dich[^.]{0,80}(portal|formular)", r"only (accept|take) applications (via|through)")
INITIATIV = _rx(r"initiativ", r"unsolicited application", r"speculative application", r"open application")


def rule_kind(email: dict, known: bool = False) -> str | None:
    """confirmation / rejection / interview / test / offer / other, None = the rules aren't sure (ask the AI),
    "" = not about one of your applications at all. known: the email is in an application's thread or from
    its company's domain, so it doesn't have to say "your application"."""
    subject, text = email["subject"], f"{email['subject']} \n {email['body']}"
    if not known:
        if NOT_YOURS.search(subject) or not (ABOUT_APPLICATION.search(text) or STRONG_SUBJECT.search(subject)):
            return ""
        if NOT_YOURS.search(text[:600]) and not ABOUT_APPLICATION.search(subject):
            return ""
    # Out-of-office and account mails first: an auto-reply quotes your email ("Initiativbewerbung ...")
    if OTHER.search(subject):
        return "other"
    # The subject alone is a strong sign ("Einladung zum Vorstellungsgespräch", "Absage")
    hits = {k for k, rx in (("rejection", REJECTION), ("interview", INTERVIEW), ("test", TEST), ("offer", OFFER))
            if rx.search(subject)}
    if len(hits) == 1:
        return hits.pop()
    if hits:
        return None
    rejection, interview, test = REJECTION.search(text), INTERVIEW.search(text), TEST.search(text)
    offer, confirmation = OFFER.search(text), CONFIRMATION.search(text)
    if offer and not (rejection or LEIDER.search(text)):
        return None  # a "Zusage" is rare and important: the AI reads it
    if PORTAL_ONLY.search(text[:1500]) and not (interview or offer):
        return "reply"  # "apply through our careers portal": they answered, you have to act
    # A concrete invitation wins over "thank you for your application" around it; not "IF it fits, we invite you"
    if INVITE.search(text[:2500]) and not rejection and not IF_IT_FITS.search(text[:2500]):
        return "interview"
    if rejection and not (interview or test):
        return "rejection"
    if LEIDER.search(text) and not (interview or test or confirmation):
        return "rejection"
    # A confirmation often says "if it fits, we will invite you to an interview": unsure
    if (interview or test or LEIDER.search(text)) and not rejection:
        if confirmation and not test and CONDITIONAL.search(text) and not INTERVIEW.search(subject):
            return "confirmation"
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
  "confirmation"  an automatic "we received your application" (also "thank you for applying",
                  "we will get back to you", "forwarded to the department")
  "reply"         a person from the company answered, but it is none of the kinds below
                  (a question, a request for documents or for the start date / hours, "we will call you")
  "rejection"     the employer says no (also "we decided for other candidates", "position filled",
                  "no open position at the moment")
  "interview"     an invitation to an interview, phone, Teams or video call or meeting with the employer,
                  or a request to pick a date for one
  "test"          an online test, coding task, case study or assessment the person has to do
  "offer"         a job offer or contract ("Zusage", "Vertragsangebot", "we would like to offer you")
  "other"         anything else: newsletters, job ads, account / password emails, out-of-office replies,
                  reminders to finish an application, "your application was viewed", general news
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
                         temperature=0.1, purpose="other")
    out = {}
    for a in answer if isinstance(answer, list) else []:
        if isinstance(a, dict) and a.get("kind") in KINDS:
            out[str(a.get("id"))] = {"kind": a["kind"], "company": str(a.get("company") or "")[:120],
                                     "sure": bool(a.get("sure"))}
    return out


# ---- 2. names, titles, reference numbers -------------------------------------------------

# Parts of sender addresses and names that say nothing about the employer
SENDER_NOISE = {
    "noreply", "no", "reply", "donotreply", "mail", "email", "e", "m", "info", "news", "jobs", "job", "karriere",
    "career", "careers", "recruiting", "recruitment", "recruiter", "bewerbung", "bewerbungen", "bewerber",
    "bewerberbetreuung", "talent", "team", "hr", "personal", "application", "applications", "notification",
    "notifications", "via", "von", "from", "de", "com", "net", "org", "io", "eu", "jobs", "apply", "status", "update",
    "personio", "softgarden", "workwise", "indeed", "indeedapply", "linkedin", "stepstone", "join", "onlyfy",
    "concludis", "hrlab", "beesite", "successfactors", "workday", "myworkdayjobs", "greenhouse", "lever",
    "smartrecruiters", "recruitee", "teamtailor", "dvinci", "rexx", "umantis", "jobvalley", "ihre", "deine", "your",
    "system", "hrsystem", "yourcareer", "welcome", "hallo", "hello", "kontakt", "contact", "office", "msg", "app",
}
REPLY_PREFIX = re.compile(r"^\s*((re|aw|wg|fwd?|antw|wtr|tr)\s*:\s*)+", re.I)
TITLE_START = [re.compile(p, re.I) for p in (
    r"(?:initiativ)?bewerbung\s+(?:als|f(?:ü|ue)r|um|auf)\s+(?:die\s+(?:stelle|position)\s*(?:als\s+)?|eine?n?\s+|den\s+)?(.+)",
    r"application\s+(?:as|for)\s+(?:the\s+(?:position|role)\s+(?:of\s+|as\s+)?|an?\s+)?(.+)",
    r"((?:werkstudent|working student|praktikant|studentische|student)\w*\b.+)")]
REF = re.compile(r"(?:stellen-?id|job-?id|kennziffer|referenz(?:nummer)?|ref(?:\.|erence)?(?:-?nr\.?)?|ausschreibungs-?"
                 r"(?:nr\.?|nummer)|req(?:uisition)?(?:\s*id)?|crm)\s*[:#]?\s*([a-z]{0,3}-?\d[\w-]{2,})", re.I)
COMPANY_AT = re.compile(r"\b(?:bei|at)\s+(?:der\s+|die\s+|dem\s+|the\s+)?([A-ZÄÖÜa-zäöü0-9][^|:()\n,/]{1,60}?)"
                        r"(?=\s*(?:\||:|\(|,|/|\s[–-]\s|$|\s+received|\s+eingegangen|\s+erhalten))")
NOT_A_COMPANY = re.compile(r"^((uns|ihnen|dir|euch|you|us)\b.*|ihrem unternehmen|your company|frankfurt|darmstadt|.{0,2})$",
                           re.I)  # "bei uns in guten Händen"


def _words(text: str) -> list[str]:
    from .discover import _fold
    return re.sub(r"[^a-z0-9]+", " ", _fold(text)).split()


# First words that are no company on their own (a job title in the company field, a common word)
FIRST_WORD_SKIP = {"working", "student", "studentische", "werkstudent", "werkstudentin", "praktikant", "praktikum",
                   "junior", "senior", "institut", "institute", "hochschule", "universitaet", "university", "stadt",
                   "jobportal", "hans", "neue", "new", "first", "global", "german", "europa", "european", "rhein",
                   "main", "frankfurt", "darmstadt", "hessen", "hessische", "allgemeine", "vereinigte", "info"}


def _same_company(a: set[str], b: set[str]) -> bool:
    """Two companies' keys: the same name, or one starting with the other ('muster' / 'musterhardundsoftware')."""
    from .discover import _same
    return bool(a & b) or any(_same(x, y) for x in a for y in b)


def _keys(company: str) -> set[str]:
    """Ways the company's name can appear: 'MB BANK AG' -> {'mbbank'}; 'Muster-Clip System GmbH' -> {'musterclipsystem', 'musterclip'}."""
    from .companies import clean_name
    from .discover import GENERIC_FIRST_WORDS, GENERIC_SLUGS, _words as name_words
    company = re.sub(r"\([^)]*\)", " ", company or "").strip()  # "LocateRisk UG (haftungsbeschränkt)", "... GmbH (DTVG)"
    if re.fullmatch(r"[\w-]+(\.[\w-]+)*\.[a-z]{2,}", company.strip().lower()):  # "kfw.de" -> "kfw"
        company = re.sub(r"-(recruiting|jobs|karriere|careers?)$", "", company.strip().lower().split(".")[0])
    keys = set()
    for name in {company, clean_name(company)}:
        words = name_words(name)  # without GmbH, AG, Group, ...
        if words:
            keys.add("".join(words))
            if len(words) > 2 and len("".join(words[:2])) >= 6:
                keys.add("".join(words[:2]))
        # "PwC Deutschland – Frankfurt" is also just "PwC" in an email; not "Deutsche ..." or "IT ..."
        if len(words) > 1 and len(words[0]) >= 3 and words[0] not in GENERIC_FIRST_WORDS | GENERIC_SLUGS | FIRST_WORD_SKIP:
            keys.add(words[0])
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
    title = re.sub(r"\((?:[mwfdx]\s*/\s*)+[mwfdx]\)|\b[mwfd]/[mwfd]/[mwfd]\b|\*in\b|:in\b", " ", title or "", flags=re.I)
    return {w for w in _words(title) if len(w) >= 3 and w not in {
        "werkstudent", "werkstudentin", "working", "student", "bereich", "und", "the", "for", "mit", "all", "gender",
        "genders", "initiativbewerbung", "praktikant", "studentische", "aushilfe"}}


def title_from_subject(subject: str, my_name: str = "") -> str:
    """'Bewerbung als Werkstudent Softwareentwicklung – Max Mustermann' -> 'Werkstudent Softwareentwicklung'."""
    s = REPLY_PREFIX.sub("", subject or "")
    s = re.sub(r"\[[^\]]*\]", " ", s)  # [EXTERN]
    s = re.split(r"\s*/\s*your application", s, flags=re.I)[0]  # "... / Your application as ..." (the same in English)
    mine = set(_words(my_name))
    parts = [p.strip() for p in re.split(r"\s+[–|]\s+|\s+-\s+|\s*\|\s*", s)
             if p.strip() and not (mine and set(_words(p)) <= mine)]
    s = " – ".join(parts)
    title = ""
    for rx in TITLE_START:
        if m := rx.search(s):
            title = m[1]
            break
    title = re.split(r"(?:^|\s+)(?:bei|at)\s+(?:der\s+|die\s+|dem\s+|the\s+)?[A-ZÄÖÜa-z]", title)[0]  # "... bei Muster AG"
    title = re.split(r"\s+(?:erhalten|eingegangen|received|wurde|ist)\b", title)[0]
    title = REF.sub(" ", title)
    title = re.sub(r"\(\s*,?\s*\)|:\d{5,}\b", " ", title)
    return re.sub(r"\s+", " ", title).strip(" -–|:,.")[:150]


def ref_of(text: str) -> str | None:
    m = REF.search(text or "")
    return m[1].lower() if m else None


def company_from_subject(subject: str) -> str:
    # "... Offenbach bei Frankfurt Ihre Bewerbung bei Muster AG": the last "bei" names the company
    found = [re.split(r"\s(?:bei|at)\s+(?:der\s+|die\s+|dem\s+|the\s+)?", m[1].strip())[-1]
             for m in COMPANY_AT.finditer(REPLY_PREFIX.sub("", subject or ""))]
    found = [c for c in found if not NOT_A_COMPANY.match(c) and not re.match(r"(?i)(werkstudent|bewerbung)", c)]
    return found[-1] if found else ""


def company_from_body(body: str) -> str:
    """The first 'XY GmbH' in the text: 'Damen und Herren der XY Solutions GmbH' -> 'XY Solutions GmbH'."""
    from .initiativ_mail import LEGAL_FORM
    text = (body or "")[:1500]
    for m in LEGAL_FORM.finditer(text):
        before = re.findall(r"[\w&.\-]+", text[max(0, m.start() - 80):m.start()])
        name = []
        for w in reversed(before[-4:]):
            if not (w[:1].isupper() or w[:1].isdigit()):
                break
            name.insert(0, w)
        if name:
            return " ".join(name + [m[0]])
    return ""


def sender_name(sender: str) -> str:
    """'ABC | Karriere' -> 'ABC'; 'Muster System GmbH (via softgarden)' -> 'Muster System GmbH'."""
    name, address = parseaddr(sender)
    name = re.split(r"\s[|–-]\s", re.sub(r"\(via [^)]*\)", "", name, flags=re.I))[0]
    name = re.sub(r"\s+(karriere|recruiting|bewerbungen|bewerberbetreuung|talent team|hr|jobs|careers?)$", "", name.strip(), flags=re.I)
    return name.strip() or address


def _name_from_key(key: str | None) -> str:
    """'musterfirma.de' -> 'Musterfirma'; 'beispiel@personio.de' -> 'Beispiel'."""
    if not key:
        return ""
    label = key.split("@")[0] if "@" in key else key.split(".")[0]
    label = re.sub(r"-(jobs?|recruiting|karriere|careers?|gmbh|ag)$", "", label)
    return label.replace("-", " ").title() if len(label) > 4 else label.upper()


def _fits(name: str, key: str | None) -> bool:
    """Does a name fit the email domain? 'Max Müller GmbH' and maxmueller.de: yes."""
    if not name or not key:
        return bool(name)
    label = "".join(_words(key.split("@")[0] if "@" in key else key.split(".")[0]))
    return any(len(w) >= 3 and w in label for w in _words(name))


def company_of_email(e: dict, ai_company: str = "", lookup: dict | None = None) -> str:
    """The employer a portal email is about, or "" when it can't be told. The sender's name only when it fits
    the email domain ("Anna Schmidt <schmidt@musterfirma.de>" is a person: "Musterfirma")."""
    address = _address(e["sender"])
    key = contact_key(address)
    name = company_from_subject(e["subject"]) or ai_company
    if not name and key and "@" not in key and lookup:
        name = lookup.get(key, "")
    if not name and registrable(address.rpartition("@")[2] or "x.x") not in JOB_PORTALS:
        sender = re.split(r"\s+(?:von|from|for|für|at)\s+", sender_name(e["sender"]), flags=re.I)[-1]
        sender = re.split(r"\s*[/(]", sender)[0]  # "Muster GmbH/Werkstudent (m/w/d) ..."
        kept = " ".join(w for w in sender.split() if not (_words(w) and set(_words(w)) <= SENDER_NOISE))
        shared = not key  # no company in the address (softgarden "notifications@"): the sender name is the company
        if kept and "@" not in sender and (shared or _fits(kept, key)):
            name = kept
        elif key:
            name = _name_from_key(key)
    if not name and (not key or "@" in key):  # "deine Bewerbung bei Muster Consulting GmbH" (Workwise, Indeed)
        name = company_from_body(e["body"])
    return name.strip()[:120]


# ---- 3. your sent emails -> applications ---------------------------------------------------

APPLICATION_SUBJECT = _rx(r"bewerbung", r"\bapplication\b", r"\bbewerbe", r"\bcandidature\b", r"\bapplying\b")
APPLICATION_TEXT = _rx(r"bewerbe mich", r"(initiativ)?bewerbung (als|um|für|auf)", r"initiativbewerbung",
                       r"i am applying", r"i('d| would) like to apply", r"apply for the (position|role)",
                       r"meine bewerbungsunterlagen", r"lebenslauf (und|sowie|im anhang|anbei)")


def is_application(e: dict) -> bool:
    """A sent email that is an application (not a reply, a forward, a test draft or an email to yourself)."""
    if REPLY_PREFIX.match(e["subject"]) or e["subject"].startswith("[TEST"):
        return False
    to = [a for a in e["to"] if a != _me()]
    if not to:
        return False
    return bool(APPLICATION_SUBJECT.search(e["subject"]))  # only when the subject says so: no guessing from the text


def maybe_application(e: dict) -> bool:
    """Left out, but the text sounds like an application (shown in the dry run, never counted)."""
    return not is_application(e) and not REPLY_PREFIX.match(e["subject"]) and bool(APPLICATION_TEXT.search(e["body"][:1500]))


def _company_lookup() -> dict[str, str]:
    """Email domain -> company name, from the Companies list (website and Impressum email)."""
    from urllib.parse import urlparse
    out = {}
    try:
        rows = db.company_rows()
    except Exception:  # an old database without the Companies list
        return out
    for c in rows:
        for url in (c.get("website"), c.get("career_url")):
            host = urlparse(url if "//" in (url or "") else f"https://{url or ''}").hostname
            if host and registrable(host) not in SHARED_HOSTS:
                out.setdefault(registrable(host), c["name"])
        if c.get("contact_email") and "@" in c["contact_email"]:
            out.setdefault(registrable(c["contact_email"].split("@")[1]), c["name"])
    return out


_lookup: dict = {}


def _lookup_cache() -> dict[str, str]:
    """_company_lookup, read once a minute at most."""
    if time.time() - _lookup.get("at", 0) > 60:
        _lookup.update(at=time.time(), map=_company_lookup())
    return _lookup["map"]


def portal_on() -> bool:
    """config applications: portal_from_emails. On: a confirmation / rejection from a company you never emailed
    starts an application by itself. Off (default): website applications come from the Applied button."""
    return bool(settings().get("portal_from_emails", False))


def portal_without_company(e: dict) -> bool:
    host = registrable(_address(e["sender"]).rpartition("@")[2] or "x.x")
    return host in JOB_PORTALS and not company_from_subject(e["subject"])


def _initiativ_names() -> dict[str, str]:
    """Gmail thread id -> company, for the Initiativ drafts Job Radar made."""
    try:
        names = {c["id"]: c["name"] for c in db.company_rows()}
        return {d["gmail_thrid"]: names.get(d["company_id"], "") for d in db.initiativ_drafts() if d["gmail_thrid"]}
    except Exception:
        return {}


def application_from_sent(e: dict, lookup: dict[str, str], drafts: dict[str, str]) -> dict:
    to = next(a for a in e["to"] if a != _me())
    key = contact_key(to)
    my_name = parseaddr(e["sender"])[0]
    kind = "initiativ" if INITIATIV.search(e["subject"] + " " + e["body"][:2000]) else "job"
    host = registrable(to.split("@")[1])
    company = drafts.get(e["thread_id"]) or lookup.get(host) or company_from_subject(e["subject"])
    if not company:  # "XY GmbH" from the text only when it fits the address ("IT-Service GmbH" for beispiel.de does not)
        company = company_from_body(e["body"])
        if not _fits(company, key):
            company = _name_from_key(key)
    return {"kind": kind, "source": "sent", "origin": e["gmail_id"], "company": company,
            "title": title_from_subject(e["subject"], my_name), "sent_at": e["received_at"], "contact": to,
            "contact_key": key, "thread_id": e["thread_id"], "ref": ref_of(e["subject"])}


def _same_application(a: dict, new: dict) -> bool:
    """A second email to the same company about the same thing (a reminder): not a new application."""
    if not new["contact_key"] or a["contact_key"] != new["contact_key"] or a["kind"] != new["kind"]:
        return False
    if new["kind"] == "initiativ":
        return True
    old, now_ = _title_words(a["title"]), _title_words(new["title"])
    return not old or not now_ or len(old & now_) / min(len(old), len(now_)) >= 0.6


def sync_sent() -> dict:
    """Reads new emails in your Sent folder; each application becomes one in the tracker."""
    since = start_date()
    counts = {"sent_read": 0, "applications": 0}
    imap = gmail.open_folder("Sent")
    try:
        uids = gmail.search(imap, f"after:{since:%Y/%m/%d} {{bewerbung initiativbewerbung application lebenslauf "
                                  f"bewerbe bewerben resume}}")
        ids = gmail.ids_of(imap, sorted(uids, key=int))  # oldest first: the first email to a company counts
        known = db.known_sent([g for g, _ in ids.values()])
        new = [(u, g, t) for u, (g, t) in ids.items() if g not in known]
        if not new:
            return counts
        lookup, drafts = _company_lookup(), _initiativ_names()
        apps = db.applications() + db.applications(hidden=True)
        for uid, gid, tid in new[:200]:
            raw = gmail.fetch(imap, uid)
            if not raw:
                continue
            e = {"gmail_id": gid, "thread_id": tid, **parse(raw)}
            counts["sent_read"] += 1
            if not is_application(e):
                db.save_sent(gid, tid, "not")
                if maybe_application(e):
                    counts.setdefault("left_out", []).append(f"{e['received_at'][:10]} {', '.join(e['to'])}: {e['subject']}")
                continue
            new_app = application_from_sent(e, lookup, drafts)
            same = next((a for a in apps if (tid and a["thread_id"] == tid) or _same_application(a, new_app)), None)
            if same:
                db.save_sent(gid, tid, "follow-up")
                continue
            new_app["id"] = db.add_application(new_app)
            apps.append(new_app)
            db.save_sent(gid, tid, "application")
            counts["applications"] += 1
    finally:
        gmail._logout(imap)
    return counts


def link_jobs() -> int:
    """Jobs you marked Applied: linked to their email application (same company), else one of their own."""
    apps = db.applications() + db.applications(hidden=True)
    linked = {a["job_id"] for a in apps if a["job_id"]}
    added = 0
    for j in db.applied_jobs():
        if j["id"] in linked:
            continue
        keys, words = _keys(j["company"] or ""), _title_words(j["title"])
        fits = [a for a in apps if a["kind"] == "job" and not a["job_id"] and keys and _same_company(_keys(a["company"] or ""), keys)]
        if len(fits) > 1 and words:
            fits = [a for a in fits if len(words & _title_words(a["title"])) / len(words) >= 0.5] or fits
        if fits:
            a = max(fits, key=lambda a: len(words & _title_words(a["title"])))
            db.update_application(a["id"], {"job_id": j["id"]})
            a["job_id"] = j["id"]
            continue
        db.add_application({"kind": "job", "source": "jobradar", "origin": f"job:{j['id']}", "company": j["company"],
                            "title": j["title"], "sent_at": j["applied_at"], "job_id": j["id"]})
        added += 1
    return added


# ---- 4. which application ---------------------------------------------------------------

def _prepared(apps: list[dict]) -> list[dict]:
    for a in apps:
        a["keys"] = _keys(a["company"] or "")
        a["title_words"] = _title_words(a["title"])
    return apps


def _title_score(a: dict, words: set[str], ref: str | None) -> float:
    score = len(a["title_words"] & words) / len(a["title_words"]) if a["title_words"] else 0
    return score + (1 if ref and a["ref"] == ref else 0)


def _before(a: dict, e: dict) -> bool:
    """Can the email be a reply to this application? Not when it came before you emailed it (a day's slack for time
    zones). Applications from the Applied button have no exact date: always possible."""
    if a["source"] != "sent" or not a["sent_at"] or not e.get("received_at"):
        return True
    sent = datetime.fromisoformat(a["sent_at"]) - timedelta(days=1)
    return datetime.fromisoformat(e["received_at"]) >= sent


def _is_indeed_apply(e: dict) -> bool:
    return _address(e["sender"]).endswith("@indeed.com") and _address(e["sender"]).startswith("indeedapply")


def find(e: dict, apps: list[dict], ai_company: str = "") -> tuple[int | None, int | None, str]:
    """(application id, best guess, note). The id only when the match is clear; otherwise the best guess
    (may be None) and why it needs review. Thread, then the sender's domain, then the company's name."""
    apps = [a for a in apps if _before(a, e)]
    if e.get("thread_id"):
        same = [a for a in apps if a["thread_id"] == e["thread_id"]]
        if same:
            return same[0]["id"], same[0]["id"], ""
    key = contact_key(_address(e["sender"]))
    candidates = [a for a in apps if key and a["contact_key"] == key]
    if not candidates:
        subject, sender, body = _words(e["subject"]), _sender_words(e["sender"]), _words(e["body"])

        def named(words):
            return [a for a in apps if any(_mentions(words, k) for k in a["keys"])]

        # Subject and sender first: a portal email's text often lists other employers too
        candidates = named(subject) + [a for a in named(sender) if a not in named(subject)]
        if not candidates:
            candidates = named(body[:400])
        if not candidates and ai_company:
            from .discover import _same
            ai_keys = _keys(ai_company)
            candidates = [a for a in apps if any(_same(x, k) for x in ai_keys for k in a["keys"])]
    if not candidates:
        # A job portal's confirmation (Indeed), or a job you marked Applied without a company name: the one
        # application with that title
        title = _title_words(title_from_subject(e["subject"]))
        if len(title) >= 2:
            fits = [a for a in apps if a["title_words"] and len(title & a["title_words"]) / len(title | a["title_words"]) >= 0.75]
            if len(fits) == 1:
                return fits[0]["id"], fits[0]["id"], ""
        if title and _is_indeed_apply(e):  # Indeed cuts long titles ("Developer" for "Developer & Cloud")
            fits = [a for a in apps if a["title_words"] and title <= a["title_words"]]
            if len(fits) == 1:
                return fits[0]["id"], fits[0]["id"], ""
    if not candidates:
        return None, None, NOT_FOUND
    text = set(_words(e["subject"])) | set(_words(e["body"][:600]))
    title = _title_words(title_from_subject(e["subject"]))
    ref = ref_of(e["subject"] + " " + e["body"][:600])
    initiativ = bool(INITIATIV.search(e["subject"] + " " + e["body"][:1500]))
    if len({a["kind"] for a in candidates}) > 1:  # an Initiativ and a job ad at the same company
        candidates = [a for a in candidates if (a["kind"] == "initiativ") == initiativ] or candidates
    if len(candidates) == 1:
        a = candidates[0]
        # Another reference number ("CRM:0100001" / "CRM:0100002"): another job at the same company
        if e.get("kind") in ("confirmation", "rejection") and ref and a["ref"] and ref != a["ref"]:
            return None, None, "new job at this company"
        # A confirmation for a different job at the same company: a new application, not this one
        if (e.get("kind") in ("confirmation", "rejection") and a["kind"] == "job" and not initiativ and title and a["title_words"]
                and len(title & a["title_words"]) / min(len(title), len(a["title_words"])) < 0.34):
            return None, None, "new job at this company"
        return a["id"], a["id"], ""
    if (e.get("kind") in ("confirmation", "rejection") and ref and all(a["ref"] for a in candidates)
            and not any(a["ref"] == ref for a in candidates)):
        return None, None, "new job at this company"  # a reference number none of them has
    scored = sorted(((_title_score(a, text, ref), a) for a in candidates), key=lambda x: -x[0])
    (best, a), (second, _) = scored[0], scored[1]
    if best >= 0.6 and best - second >= 0.25:
        return a["id"], a["id"], ""
    if e.get("kind") in ("confirmation", "rejection") and title and best < 0.34:
        return None, None, "new job at this company"
    if e.get("kind") == "confirmation":
        # A confirmation changes no status, so it doesn't have to be exact: the job named most closely, else the newest
        a = max(candidates, key=lambda x: (_title_score(x, text, ref), len(x["title_words"] & text), x["sent_at"] or "", x["id"]))
        return a["id"], a["id"], ""
    return None, a["id"], f"{len(candidates)} applications at {a['company']}: which one?"


# ---- the check ------------------------------------------------------------------------

_lock = threading.Lock()
_ai_failures: dict[str, int] = {}   # gmail id -> runs in which the AI didn't answer (review after 3)
_last: dict = {}                    # result of the last check, for the dashboard
RETRY_SECONDS = 30                  # after a Gmail timeout
MAX_PER_CHECK = 400                 # new emails read per check (the first check reads the whole time since tracker_since)
NOT_FOUND = "No application to this company found"
NOT_FOUND_HINT = ("No application found. Applied on their website? Click Applied on the job, "
                  "or pick \"New application from this email\"")
RULE_VERSION = "2"                  # settings "tracker_rule": older emails are read again once with the new rules


def last_result() -> dict:
    return dict(_last)


def check() -> dict:
    """Reads Sent and the replies, sorts and matches them. Returns counts (also kept for the dashboard)."""
    if not enabled():
        return {"ok": False, "note": "Off: no Gmail login in .env, or applications: enabled: false"}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "note": "Already running"}
    try:
        try:
            result = _check()
        except TimeoutError:  # Gmail didn't answer in time (happens now and then): once more
            log.warning("Applications: Gmail timed out, trying once more in %d s", RETRY_SECONDS)
            time.sleep(RETRY_SECONDS)
            result = _check()
    except gmail.GmailError as e:
        result = {"ok": False, "note": str(e)}
    except Exception as e:
        log.exception("Applications: check failed")
        result = {"ok": False, "note": f"{type(e).__name__}: {str(e)[:150]}"}
    finally:
        _lock.release()
    _last.clear()
    _last.update(result, at=db.now())
    return result


def _upgrade() -> None:
    """Once, with new rules: emails that were skipped as "not about an application" are read again (they may be
    replies from a company you emailed), and emails you matched to a job by hand move to that job's application."""
    if db.get_setting("tracker_rule") == RULE_VERSION:
        return
    db.backup("before-tracker")
    with db.connect() as conn:
        conn.execute("DELETE FROM app_emails WHERE state = 'ignored'")
        # Your own choices stay: an email you matched to a job goes to that job's application
        conn.execute("""UPDATE app_emails SET app_id = (SELECT a.id FROM applications a WHERE a.job_id = app_emails.job_id)
                        WHERE job_id IS NOT NULL AND app_id IS NULL AND state = 'matched' AND kind_from = 'you'""")
        # Matched by the old rules (they matched by name only, sometimes wrongly): matched again with the new ones
        conn.execute("UPDATE app_emails SET state = 'review', app_id = NULL, note = ? "
                     "WHERE state = 'matched' AND kind_from != 'you'", (NOT_FOUND,))
    db.set_setting("tracker_rule", RULE_VERSION)


def _incoming(apps: list[dict]) -> list[dict]:
    """New emails that could be replies: in your applications' threads, from their domains, in the
    "Bewerbungen" label, or with "Bewerbung" / "application" in the subject. Oldest first."""
    since = start_date()  # also portal applications from before your first emailed one
    base = f"-in:sent -in:drafts -in:chats after:{since:%Y/%m/%d}"
    label = str(settings()["label"]).strip().replace(" ", "-")
    queries = [f"{base} {{label:{label} subject:bewerbung subject:initiativbewerbung subject:application "
               f"subject:bewerbungsprozess subject:absage subject:interview}}"]
    terms = sorted({_search_term(a["contact_key"], a["contact"] or "") for a in apps if a["contact_key"]} - {""})
    for i in range(0, len(terms), 20):
        queries.append(f"{base} {{{' '.join('from:' + t for t in terms[i:i + 20])}}}")
    known_sent = db.known_sent  # your own emails in the threads are not fetched
    imap = gmail.open_folder("All")
    emails = []
    try:
        uids = set()
        for q in queries:
            uids.update(gmail.search(imap, q))
        for a in apps:
            if a["thread_id"] and a["source"] == "sent":
                uids.update(gmail.thread_uids(imap, a["thread_id"]))
        ids = gmail.ids_of(imap, sorted(uids, key=int))
        all_ids = [g for g, _ in ids.values()]
        skip = db.known_app_emails(all_ids) | known_sent(all_ids)
        new = [(u, g, t) for u, (g, t) in ids.items() if g not in skip]
        for uid, gid, tid in new[:max(int(settings()["max_emails_per_check"]), MAX_PER_CHECK)]:
            raw = gmail.fetch(imap, uid)
            if raw is not None:
                emails.append({"gmail_id": gid, "thread_id": tid, **parse(raw)})
    finally:
        gmail._logout(imap)
    return sorted(emails, key=lambda e: e["received_at"] or "")


def _check() -> dict:
    counts = {"ok": True, "read": 0, "matched": 0, "new_applications": 0, "review": 0, "ignored": 0}
    counts.update(sync_sent())
    counts["new_applications"] = counts.pop("applications", 0) + link_jobs()
    _upgrade()
    apps = _prepared(db.applications())
    emails = _incoming(apps)
    counts["read"] = len(emails)
    threads = {a["thread_id"] for a in apps if a["thread_id"]}
    keys = {a["contact_key"] for a in apps if a["contact_key"]}

    def known(e):
        return e["thread_id"] in threads or contact_key(_address(e["sender"])) in keys

    unsure = []
    for e in emails:
        if ignored_sender(e["sender"]) or newsletter(e, known(e)):
            kind = ""
        else:
            kind = rule_kind(e, known(e))
        if kind == "":
            db.save_app_email({**e, "body": "", "kind": None, "kind_from": "rules", "state": "ignored"})
            counts["ignored"] += 1
        elif kind is None or (portal_without_company(e) and ai.enabled()):
            unsure.append(e)  # a job portal's email without the company's name: the AI reads it from the text
        else:
            e.update(kind=kind, kind_from="rules")
            _file(e, counts, apps)
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
                log.warning("Applications: AI did not answer (%s)", str(ex)[:150])
        for e in batch:
            a = answers.get(e["gmail_id"])
            if a is None and isinstance(error, (ai.RateLimited, ai.Overloaded)):
                continue  # every AI is at its limit: the email waits (read again next round)
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
            _file(e, counts, apps)
    counts["matched"] += rematch_review(apps)
    refresh_statuses()
    notify()
    if emails or counts["new_applications"]:
        log.info("Applications: %d new in Sent, %d new emails, %d matched, %d need review, %d not about an application",
                 counts["new_applications"], counts["read"], counts["matched"], counts["review"], counts["ignored"])
    return counts


def _new_from_email(e: dict, apps: list[dict], by_you: bool = False) -> int | None:
    """An application you made on a portal (no sent email): started from its confirmation or rejection
    (by_you: from any email, you chose "New application" in Needs review)."""
    if not by_you and not portal_on():
        return None  # off: you add website applications with the Applied button (or "New application" in Needs review)
    company = company_of_email(e, e.get("company") or "", _lookup_cache()) or (_address(e["sender"]) if by_you else "")
    if not company or (e["kind"] not in ("confirmation", "rejection") and not by_you):
        return None
    initiativ = bool(INITIATIV.search(e["subject"] + " " + e["body"][:1500]))
    sender = _address(e["sender"])
    a = {"kind": "initiativ" if initiativ else "job", "source": "portal", "origin": e["gmail_id"], "company": company,
         "title": title_from_subject(e["subject"]), "sent_at": e["received_at"], "contact": sender,
         "contact_key": contact_key(sender), "thread_id": e.get("thread_id"),
         "ref": ref_of(e["subject"] + " " + e["body"][:600])}
    a["id"] = db.add_application(a)
    apps.extend(_prepared([dict(a, job_id=None)]))
    return a["id"]


def _file(e: dict, counts: dict, apps: list[dict]) -> None:
    """Match one sorted email to an application and save it (matched, new application, or Needs review)."""
    app_id, guess, why = find(e, apps, e.get("company", ""))
    if app_id is None and e["kind"] != "other" and not e.get("note") and (guess is None):
        app_id = _new_from_email(e, apps)
        if app_id:
            counts["new_applications"] += 1
            guess = app_id
    if e["kind"] == "other" and not e.get("note"):
        state, note = "ignored", None   # a portal / account / out-of-office email: nothing to review
    elif app_id is None and e["kind"] == "confirmation" and _is_indeed_apply(e):
        state, note = "ignored", None   # Indeed's "your application was forwarded": no job of ours to attach it to
    elif app_id is not None and not e.get("note"):
        state, note = "matched", None
    else:
        state, note = "review", e.get("note") or ("Which application is it?" if why == "new job at this company" else why)
        if note == NOT_FOUND and not portal_on():
            note = NOT_FOUND_HINT
    db.save_app_email({**e, "app_id": app_id or guess, "state": state, "note": note})
    a = next((x for x in apps if x["id"] == app_id), None) if state == "matched" else None
    if a is not None and not (a["company"] or "").strip():  # a job you marked Applied had no company name
        name = company_of_email(e, e.get("company") or "", _lookup_cache())
        if name:
            db.update_application(a["id"], {"company": name})
            a.update(company=name, keys=_keys(name))
    counts["matched" if state == "matched" else "review" if state == "review" else "ignored"] += 1


def rematch_review(apps: list[dict] | None = None) -> int:
    """Emails in Needs review because no application was found: tried again (a new sent email or a click on
    Applied can be their application). Returns how many found theirs."""
    apps = apps if apps is not None else _prepared(db.applications())
    done = 0
    threads = {a["thread_id"] for a in apps if a["thread_id"]}
    keys = {a["contact_key"] for a in apps if a["contact_key"]}
    for e in db.app_emails(state="review"):
        if e["kind_from"] == "you":
            continue  # you sorted it yourself
        if newsletter(e, e["thread_id"] in threads or contact_key(_address(e["sender"])) in keys):
            db.update_app_email(e["gmail_id"], {"state": "ignored", "note": None})
            continue
        if (e["note"] or "").startswith(("The AI isn't sure", "Unclear")):
            continue
        if e["kind_from"] == "rules" and e["body"]:  # sorted by older rules: sorted again with today's
            kind = rule_kind(e, e["thread_id"] in threads or contact_key(_address(e["sender"])) in keys)
            if kind == "" or kind == "other":
                db.update_app_email(e["gmail_id"], {"state": "ignored", "kind": kind or None, "note": None})
                continue
            if kind and kind != e["kind"]:
                db.update_app_email(e["gmail_id"], {"kind": kind})
                e["kind"] = kind
        app_id, guess, why = find(e, apps, e.get("company") or "")
        if app_id is None and guess is None and e["kind"] in ("confirmation", "rejection"):
            app_id = _new_from_email(e, apps)
        if app_id is not None:
            db.update_app_email(e["gmail_id"], {"app_id": app_id, "state": "matched", "note": None})
            done += 1
        elif e["kind"] == "confirmation" and _is_indeed_apply(e):
            db.update_app_email(e["gmail_id"], {"app_id": None, "state": "ignored", "note": None})
            continue
        elif why == NOT_FOUND and not portal_on():
            why = NOT_FOUND_HINT
        if app_id is None and (guess != e["app_id"] or why != e["note"]):
            db.update_app_email(e["gmail_id"], {"app_id": guess, "note": why})
    return done


def _status_of(emails: list[dict]) -> str:
    """The latest decisive email (rejection, offer, interview, test), else a person's reply, else a confirmation."""
    decisive = [e for e in emails if e["kind"] in DECISIVE]
    if decisive:
        return max(decisive, key=lambda e: e["received_at"] or "")["kind"]
    for kind in ("reply", "confirmation"):
        if any(e["kind"] == kind for e in emails):
            return kind
    return "sent"


def refresh_statuses() -> None:
    """Every application's status from its matched emails; the jobs you marked Applied follow theirs."""
    by_app: dict[int, list[dict]] = {}
    for e in db.app_emails(state="matched"):
        if e["app_id"]:
            by_app.setdefault(e["app_id"], []).append(e)
    job_status = {}
    for a in db.applications():
        status = _status_of(by_app.get(a["id"], []))
        if status != a["status"]:
            db.update_application(a["id"], {"status": status})
        shown = a["status_you"] or status
        if a["job_id"] and shown != "sent":
            job_status[a["job_id"]] = shown
    db.set_app_statuses(job_status)


# ---- what you do in the dashboard ---------------------------------------------------------

def assign(gmail_id: str, app_id: int | None, kind: str | None = None) -> bool:
    """Needs review -> this application (None: dismiss it, -1: a new application from this email)."""
    e = db.app_email(gmail_id)
    if not e:
        return False
    if kind:
        e.update(kind=kind)
    if app_id == -1:
        app_id = _new_from_email(e, [], by_you=True)
    fields = {"state": "matched" if app_id else "dismissed", "note": None}
    if app_id:
        fields["app_id"] = app_id
    if kind:
        fields.update(kind=kind, kind_from="you")
    db.update_app_email(gmail_id, fields)
    refresh_statuses()
    notify()
    return True


def unassign(gmail_id: str) -> bool:
    if not db.app_email(gmail_id):
        return False
    db.update_app_email(gmail_id, {"state": "review", "note": "You said: not this application", "kind_from": "you"})
    refresh_statuses()
    return True


def set_status(app_id: int, status: str | None) -> bool:
    """Your choice in the dropdown ("" / None: back to what the emails say)."""
    if not db.application(app_id):
        return False
    db.update_application(app_id, {"status_you": status or None})
    refresh_statuses()
    return True


def set_hidden(app_id: int, hidden: bool) -> bool:
    if not db.application(app_id):
        return False
    db.update_application(app_id, {"hidden": int(hidden)})
    refresh_statuses()
    return True


def _stats(apps: list[dict]) -> dict:
    sent = len(apps)
    answered = sum(1 for a in apps if a["answered"])
    return {"sent": sent, "answered": answered, "rate": round(100 * answered / sent) if sent else 0,
            "rejected": sum(1 for a in apps if a["shown"] == "rejection"),
            "interviews": sum(1 for a in apps if a["had_interview"]),
            "offers": sum(1 for a in apps if a["shown"] == "offer"),
            "waiting": sum(1 for a in apps if not a["answered"])}


def overview() -> dict:
    """For the Applications page: every application with its replies, the counts per type and Needs review."""
    emails: dict[int, list[dict]] = {}
    for e in db.app_emails(state="matched"):
        if e["app_id"]:
            emails.setdefault(e["app_id"], []).append(
                {"gmail_id": e["gmail_id"], "at": e["received_at"], "kind": e["kind"], "subject": e["subject"],
                 "sender": parseaddr(e["sender"])[0] or _address(e["sender"]), "by": e["kind_from"]})
    apps, jobs = [], {}
    for a in db.applications():
        replies = sorted(emails.get(a["id"], []), key=lambda t: t["at"] or "")
        shown = a["status_you"] or a["status"]
        row = {k: a[k] for k in ("id", "kind", "source", "company", "title", "sent_at", "contact", "job_id",
                                 "status", "status_you", "thread_id")}
        row.update(shown=shown, replies=replies,
                   answered=shown in ANSWERS or any(r["kind"] in ANSWERS for r in replies),
                   had_interview=shown in ("interview", "offer") or any(r["kind"] == "interview" for r in replies))
        apps.append(row)
        if a["job_id"]:
            jobs[a["job_id"]] = {"timeline": replies, "app_id": a["id"]}
    stats = {"all": _stats(apps), **{t: _stats([a for a in apps if a["kind"] == t]) for t in TYPES}}
    review = [{"gmail_id": e["gmail_id"], "at": e["received_at"], "kind": e["kind"], "subject": e["subject"],
               "sender": e["sender"], "note": e["note"], "guess": e["app_id"], "snippet": (e["body"] or "")[:300]}
              for e in db.app_emails(state="review")]
    return {"applications": sorted(apps, key=lambda a: a["sent_at"] or "", reverse=True), "stats": stats,
            "types": TYPES, "labels": LABELS, "statuses": STATUSES, "jobs": jobs,
            "review": sorted(review, key=lambda r: r["at"] or "", reverse=True),
            "label": settings()["label"], "enabled": enabled(), "last": last_result(),
            "today": clock.today().isoformat()}  # German date: the page's time filter counts days from it


# ---- Telegram ----------------------------------------------------------------------

def message(e: dict) -> str:
    esc = lambda s: html.escape(s or "")
    company = e.get("job_company") or e.get("company") or sender_name(e["sender"])
    head = {"interview": f"🎉 <b>Interview invite from {esc(company)}</b>",
            "offer": f"🏆 <b>Offer from {esc(company)}</b>"}.get(e["kind"], f"📝 <b>Test / task from {esc(company)}</b>")
    lines = [head, f"✉️ {esc(e['subject'])}"]
    if e.get("job_title") and e.get("job_url"):
        lines.append(f"<a href=\"{esc(e['job_url'])}\">{esc(e['job_title'])}</a>")
    elif e.get("job_title"):
        lines.append(esc(e["job_title"]))
    else:
        lines.append("<i>Not matched to an application yet: see Needs review on the Applications page</i>")
    return "\n".join(lines)


def notify() -> int:
    """🎉 / 📝 / 🏆 for new interview invites, tests and offers (not for old emails read the first time)."""
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
        threading.Thread(target=check, daemon=True, name="applications").start()


def scheduled() -> None:
    check()
