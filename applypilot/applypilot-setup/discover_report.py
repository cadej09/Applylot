#!/usr/bin/env python3
"""Discovery-quality report for ApplyPilot — READ ONLY, ZERO Claude API spend.

Reads ~/.applypilot/applypilot.db and summarizes what the discover stage
produced, so you can tune job-board search *before* spending API credits on
scoring/tailoring. Nothing here calls an LLM.

Usage:
    python3 discover_report.py            # full report
    python3 discover_report.py --auto     # only list the auto-appliable (ATS) jobs
    python3 discover_report.py --db /path/to/applypilot.db
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")) / "applypilot.db"

# ATS domains whose apply URL can be auto-submitted by the bot.
ATS_DOMAINS = (
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "smartrecruiters.com", "workable.com", "jobvite.com", "icims.com",
    "breezy.hr", "bamboohr.com", "applytojob.com",
)
# Aggregators / boards that are NOT directly auto-appliable (need manual apply).
AGGREGATOR_DOMAINS = (
    "linkedin.com", "indeed.com", "google.com", "glassdoor.",
    "ziprecruiter.", "talent.com", "dice.com", "simplyhired.",
)

# Location buckets (substring match, lowercase).
WA_HINTS = ("seattle", "bellevue", "redmond", "kirkland", "bothell", "renton",
            "tacoma", "puget", "washington", ", wa", " wa ", "wa,", "auburn")
US_HINTS = ("united states", "usa", ", us", "u.s.", "remote us", "us remote",
            "nationwide", "anywhere in the u")
FOREIGN_HINTS = ("india", "canada", "united kingdom", "london", "philippines",
                 "brazil", "türkiye", "turkey", "singapore", "australia",
                 "germany", "france", "poland", "mexico", "ireland", "malaysia",
                 "south africa", "guyana", "denmark", "netherlands", "bulgaria")
REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed", "virtual")


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def apply_kind(application_url: str | None) -> str:
    if not application_url:
        return "no apply link"
    d = _domain(application_url)
    if any(a in d for a in ATS_DOMAINS):
        return "Auto (ATS)"
    if any(a in d for a in AGGREGATOR_DOMAINS):
        return "By hand (board)"
    return "By hand (other)"


def loc_bucket(loc: str | None) -> str:
    if not loc:
        return "unknown"
    l = loc.lower()
    if any(f in l for f in FOREIGN_HINTS):
        return "FOREIGN (should be ~0)"
    if any(w in l for w in WA_HINTS):
        return "WA / Seattle-area"
    if any(r in l for r in REMOTE_HINTS):
        return "Remote (US-assumed)"
    if any(u in l for u in US_HINTS):
        return "US (other)"
    return "other / unclear"


def bar(n: int, total: int, width: int = 30) -> str:
    if total <= 0:
        return ""
    filled = round(width * n / total)
    return "█" * filled + "·" * (width - filled)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--auto", action="store_true", help="Only list auto-appliable ATS jobs")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}\nRun discovery first: applypilot run discover -w 4")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT title, location, site, strategy, application_url, discovered_at "
        "FROM jobs"
    ).fetchall()
    total = len(rows)

    # Auto-only quick mode
    auto = [r for r in rows if apply_kind(r["application_url"]) == "Auto (ATS)"]
    if args.auto:
        print(f"\nAuto-appliable (ATS) jobs: {len(auto)}\n" + "-" * 70)
        for r in sorted(auto, key=lambda x: (x["site"] or "", x["title"] or "")):
            print(f"  [{r['site'] or '?':16}] {(r['title'] or '')[:46]:46} | {(r['location'] or '')[:22]}")
        return

    print("\n" + "=" * 70)
    print(f"  DISCOVERY REPORT  —  {total} jobs in DB")
    print(f"  {db}")
    print("=" * 70)

    # By source
    print("\nBy source (site):")
    for site, cnt in Counter((r["site"] or "?") for r in rows).most_common():
        print(f"  {site:22} {cnt:5}  {bar(cnt, total)}")

    # Apply method — the money question
    print("\nApply method (can the bot auto-submit?):")
    km = Counter(apply_kind(r["application_url"]) for r in rows)
    for kind, cnt in km.most_common():
        print(f"  {kind:22} {cnt:5}  {bar(cnt, total)}")
    print(f"\n  >> AUTO-APPLIABLE POOL: {km.get('Auto (ATS)', 0)} jobs "
          f"({100*km.get('Auto (ATS)',0)//max(total,1)}% of DB)")

    # Location buckets
    print("\nLocation mix:")
    lb = Counter(loc_bucket(r["location"]) for r in rows)
    for bucket, cnt in lb.most_common():
        flag = "  <-- filter leak!" if bucket.startswith("FOREIGN") and cnt else ""
        print(f"  {bucket:24} {cnt:5}  {bar(cnt, total)}{flag}")

    # Auto-appliable by employer
    if auto:
        print(f"\nAuto-appliable (ATS) by employer — {len(auto)} total:")
        for site, cnt in Counter((r["site"] or "?") for r in auto).most_common():
            print(f"  {site:22} {cnt:5}")
        print("\n  Sample auto-appliable roles:")
        for r in auto[:15]:
            print(f"    [{r['site'] or '?':14}] {(r['title'] or '')[:44]:44} | {(r['location'] or '')[:22]}")
    else:
        print("\n[!] Zero auto-appliable ATS jobs. Check the Workday stage in the discover log.")

    print("\n" + "=" * 70)
    print("Next: once this looks right, run scoring/tailoring (spends API):")
    print("  caffeinate -i applypilot run score tailor cover pdf -w 4")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
