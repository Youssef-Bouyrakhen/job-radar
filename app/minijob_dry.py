"""Dry run of the Minijob section: reads the sources and prints what the free filter drops and why.

No AI, no Telegram, nothing saved. Run on a test copy of the database (JOBRADAR_DB):
    .venv\\Scripts\\python -m app.minijob_dry --days 7 --max-read 60
"""
import argparse
import re
import sys
from collections import Counter

from dotenv import load_dotenv

from .config import CONFIG, ROOT

load_dotenv(ROOT / ".env")

from . import arbeitsagentur, dedup, minijob, minijob_sources  # noqa: E402  (after load_dotenv)

LABELS = {"not_minijob": "not a Minijob", "not_remote": "not 100% remote", "phone": "phone word",
          "pay": "pay above limit", "commission": "commission only", "old": "older than 30 days", "scam": "scam sign", "duplicate": "duplicate"}
HOME = re.compile(r"home[\s-]?office|remote|zu\s?hause|daheim|mobil\w*\s+arbeit|heimarbeit", re.I)


def snippet(job: dict) -> str:
    """The first home-office mention in the ad, to judge a 'not 100% remote' drop."""
    text = f"{job['title']} | {job['description']}"
    parts, last = [], -1
    for m in HOME.finditer(text):  # up to 3 mentions, so an ad that says both "possible" and "fully" shows both
        if m.start() > last and len(parts) < 3:
            parts.append(re.sub(r"\s+", " ", text[max(0, m.start() - 60):m.end() + 80]))
            last = m.end() + 80
    return " ... ".join(parts) if parts else "(no home office words at all)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--pages", type=int, default=CONFIG["minijob"]["pages"])
    ap.add_argument("--max-read", type=int, default=60, help="most Arbeitsagentur ads read in full")
    ap.add_argument("--no-remotely", dest="remotely", action="store_false", help="skip remotely.de (it reads ~30 pages slowly)")
    ap.add_argument("--examples", type=int, default=10, help="examples of 'not 100%% remote' to show with a snippet")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    candidates = []
    for name, reader in (("Arbeitsagentur", lambda: minijob.collect_arbeitsagentur(args.days, args.pages)),
                         ("Arbeitnow", lambda: minijob.collect_arbeitnow(args.days)),
                         ("Remotely", minijob_sources.list_ads) if args.remotely else ("Remotely (skipped)", list)):
        try:
            got = reader()
        except Exception as e:
            print(f"!! {name} could not be read: {type(e).__name__}: {e}")
            continue
        print(f"{name}: {len(got)} ads collected")
        candidates += got

    counts, passed, kept, not_remote, reads = Counter(), [], [], [], 0
    print("\n--- DROPPED ADS ---")
    for job in candidates:
        if job["source"] == minijob.SOURCE_BA:
            if reads >= args.max_read:
                counts["(not read: over --max-read)"] += 1
                continue
            reads += 1
            try:
                job["description"] = arbeitsagentur.fetch_description(job["url"])
            except Exception as e:
                counts[f"(could not read: {type(e).__name__})"] += 1
                continue
        elif job["source"] == minijob.SOURCE_RM:
            try:
                job.update({k: v for k, v in minijob_sources.read_ad(job["url"]).items() if v and k != "facts"})
            except Exception as e:
                counts[f"(could not read: {type(e).__name__})"] += 1
                continue
        v = minijob.evaluate(job)
        code, words = v.code, v.words
        if v.passed and dedup.find_duplicate(job, kept):
            code, words = "duplicate", "same title + company as an ad above"
        elif v.passed:
            kept.append({k: job[k] for k in ("title", "company", "location")})
            passed.append(job)
            continue
        counts[LABELS[code]] += 1
        print(f"[{LABELS[code]}] {job['source'][:9]} | {job['title'][:70]} | {job['company'][:25]} | words: {words}")
        if code == "not_remote":
            not_remote.append(job)

    print("\n--- COUNTS ---")
    for reason, n in counts.most_common():
        print(f"{n:4d}  {reason}")
    print(f"{len(passed):4d}  PASSED the filter")
    by_word = Counter(j.get("found_by", j["source"]) for j in passed)
    print("   passed per source / search word:", ", ".join(f"{w} {n}" for w, n in by_word.most_common()))
    for job in passed:
        print(f"   passed: {job['title'][:70]} | {job['company'][:25]} | {job['source'][:9]}")
    print(f"\n--- {min(args.examples, len(not_remote))} EXAMPLES of 'not 100% remote' (what the ad says about home office) ---")
    for job in not_remote[:args.examples]:
        print(f"* {job['title'][:80]} | {job['company'][:25]}\n    ...{snippet(job)}...")


if __name__ == "__main__":
    main()
