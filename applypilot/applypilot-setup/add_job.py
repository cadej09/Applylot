#!/usr/bin/env python3
"""
Add a single job to ApplyPilot by URL — for postings you find yourself
(Handshake, a company page, a referral link, anything not picked up by discovery).

After adding, run the pipeline to score + tailor it:
    python3 add_job.py "https://boards.greenhouse.io/acme/jobs/123"
    applypilot run enrich score tailor cover pdf

If you paste the description yourself, enrichment is skipped (faster, and works
even for pages the scraper can't read like login-walled Handshake postings):
    python3 add_job.py "https://app.joinhandshake.com/jobs/123" \
        --title "Data Analyst" --company "Acme" --location "Seattle, WA" \
        --desc-file ./jd.txt
    applypilot run score tailor cover pdf

Notes:
- The URL is the primary key, so re-adding the same URL is refused (no dupes).
- --desc-file takes a path; or paste inline with --desc "text".
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".applypilot" / "applypilot.db"


def main():
    ap = argparse.ArgumentParser(description="Add one job to ApplyPilot's database by URL.")
    ap.add_argument("url", help="The job posting URL (also used as the apply link)")
    ap.add_argument("--title", default="(manually added)", help="Job title")
    ap.add_argument("--company", default="", help="Company name (stored in the 'site' field)")
    ap.add_argument("--location", default="", help="Job location text")
    ap.add_argument("--salary", default="", help="Salary text, if known")
    ap.add_argument("--desc", default="", help="Paste the job description text inline")
    ap.add_argument("--desc-file", default="", help="Path to a file containing the description")
    ap.add_argument("--apply-url", default="", help="Direct application URL, if different from the posting URL")
    ap.add_argument("--db", default=str(DB_PATH), help="Path to applypilot.db")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        sys.exit(f"Database not found at {db_path}. Run ApplyPilot at least once first.")

    description = args.desc
    if args.desc_file:
        p = Path(args.desc_file).expanduser()
        if not p.exists():
            sys.exit(f"--desc-file not found: {p}")
        description = p.read_text(encoding="utf-8")
    description = description.strip()

    now = datetime.now(timezone.utc).isoformat()
    # If we have a real description, mark it enriched so scoring can run immediately.
    full_description = description if len(description) > 50 else None
    detail_scraped_at = now if full_description else None
    apply_url = args.apply_url.strip() or args.url

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO jobs "
            "(url, title, salary, description, location, site, strategy, discovered_at, "
            " full_description, application_url, detail_scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                args.url, args.title, args.salary or None, description or None,
                args.location or None, args.company or "manual", "manual", now,
                full_description, apply_url, detail_scraped_at,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Already in the DB (e.g. added via add_jobs.py or discovery). Update it
        # in place — mainly to attach a pasted description so tailoring can run.
        # Existing fit_score / tailored_resume_path are preserved.
        conn.execute(
            "UPDATE jobs SET "
            "  title = CASE WHEN ? != '(manually added)' THEN ? ELSE title END, "
            "  location = COALESCE(NULLIF(?, ''), location), "
            "  salary = COALESCE(?, salary), "
            "  description = COALESCE(?, description), "
            "  full_description = COALESCE(?, full_description), "
            "  detail_scraped_at = COALESCE(?, detail_scraped_at), "
            "  detail_error = NULL, "
            "  application_url = COALESCE(NULLIF(application_url, ''), ?) "
            "WHERE url = ?",
            (
                args.title, args.title, args.location, args.salary or None,
                description or None, full_description, detail_scraped_at,
                apply_url, args.url,
            ),
        )
        conn.commit()
        print(f"Updated existing job: {args.title}  @ {args.company or 'manual'}")
        print(f"  URL: {args.url}")
        if full_description:
            print("  Description attached. Next:  applypilot run tailor cover pdf")
        else:
            print("  Next:  applypilot run enrich tailor cover pdf")
        sys.exit(0)
    finally:
        conn.close()

    print(f"Added: {args.title}  @ {args.company or 'manual'}")
    print(f"  URL: {args.url}")
    if full_description:
        print("  Description saved — enrichment will be skipped.")
        print("  Next:  applypilot run score tailor cover pdf")
    else:
        print("  No description provided — it'll be scraped during enrichment.")
        print("  Next:  applypilot run enrich score tailor cover pdf")


if __name__ == "__main__":
    main()
