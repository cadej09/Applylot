#!/usr/bin/env python3
"""Batch-add jobs YOU found, straight into the auto-apply queue. ZERO API.

Give it a file of job/application links (one per line) and it drops each one
into ApplyPilot as a job you want, marked fit_score = 10 so it skips scoring
AND the pre-filter. By DEFAULT each job then gets a tailored résumé + cover
letter (the good stuff), and `applypilot apply` submits it.

Line format (one job per line, '#' comments allowed):
    https://apply-link...
  or, to add detail (pipe-separated, any trailing fields optional):
    https://apply-link | Job Title | Company | Location | https://direct-apply-url

Usage:
    python3 add_jobs.py my_jobs.txt                # add -> then tailor each (default)
    caffeinate -i applypilot run enrich tailor cover pdf   # fetch JD + tailor (uses API)
    applypilot apply                              # auto-apply the tailored jobs

    python3 add_jobs.py my_jobs.txt --base-resume # skip tailoring: attach base résumé, apply now (no API)
    python3 add_jobs.py my_jobs.txt --dry-run     # preview, write nothing
    python3 add_jobs.py --url "https://..."       # add a single link

Tailoring needs the job description. add_jobs marks the jobs so `enrich` will
fetch it automatically for most ATS pages. For login-walled / heavy-JS pages
enrich can't read (e.g. some Handshake/portal links), paste the description via
the single-job helper so tailoring still works:
    python3 add_job.py "<url>" --title "Data Scientist" --company BECU --desc-file jd.txt
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))
DB_PATH = APP_DIR / "applypilot.db"
RESUME_PDF = APP_DIR / "resume.pdf"


def derive_company(url: str) -> str:
    """Best-effort company label from the URL host/path."""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = p.path or ""
        # ATS patterns where the company is in the path
        m = re.search(r"greenhouse\.io/([^/]+)", host + path) or \
            re.search(r"lever\.co/([^/]+)", host + path) or \
            re.search(r"rippling\.com/([^/]+)", host + path) or \
            re.search(r"ashbyhq\.com/([^/]+)", host + path) or \
            re.search(r"myworkdayjobs\.com/([^/]+)", host + path)
        if m and m.group(1) not in ("External", "en-US", "jobs"):
            return m.group(1).replace("careers", "").replace("-", " ").strip().title() or m.group(1)
        # otherwise use the subdomain/host root
        parts = [x for x in host.split(".") if x not in ("www", "com", "org", "io", "net", "co", "jobs", "careers", "career", "apply", "phf", "tbe", "taleo", "wd1", "wd3", "wd5", "wd10", "wd12", "wd501", "wd503", "ats", "boards", "job-boards", "icims", "eightfold")]
        if parts:
            return parts[0].replace("-", " ").title()
        return host or "(manual)"
    except Exception:
        return "(manual)"


_SKIP_SEG = {"job", "jobs", "search", "external", "en-us", "careers", "career",
             "position", "positions", "opening", "openings", "-"}


def _looks_like_id(tok: str) -> bool:
    """True for hex/id-ish tokens (no vowels, or mostly digits)."""
    t = tok.replace("-", "").replace("_", "")
    if not t:
        return True
    if sum(c.isdigit() for c in t) / len(t) > 0.4:
        return True
    if not re.search(r"[aeiouAEIOU]", t):  # e.g. 3EBD688E77...
        return True
    return False


def derive_title(url: str) -> str:
    """Best-effort job title from a URL slug like /Data-Scientist_R-13109 or
    /analyst-business-intelligence/job/<hex>."""
    try:
        segs = [s for s in urlparse(url).path.split("/") if s]
        best, best_score = "", 0
        for s in segs:
            if s.lower() in _SKIP_SEG or _looks_like_id(s):
                continue
            core = re.split(r"[_]|(?<=\D)-\d", s)[0]  # trim trailing id: Data-Scientist_R-13109
            words = [w for w in re.findall(r"[A-Za-z]{2,}", core)]
            # >= so a later qualifying segment (title usually follows location) wins ties
            if len(words) >= 2 and len(words) >= best_score:
                best, best_score = " ".join(w.capitalize() for w in words), len(words)
        if best:
            return best
    except Exception:
        pass
    return "(manually added)"


def parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    url = parts[0]
    if not url.lower().startswith("http"):
        return None
    return {
        "url": url,
        "title": parts[1] if len(parts) > 1 and parts[1] else derive_title(url),
        "company": parts[2] if len(parts) > 2 and parts[2] else derive_company(url),
        "location": parts[3] if len(parts) > 3 else "",
        "apply_url": parts[4] if len(parts) > 4 and parts[4] else url,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch-add self-found jobs to ApplyPilot's apply queue.")
    ap.add_argument("file", nargs="?", help="Text file of links (one per line).")
    ap.add_argument("--url", help="Add a single link instead of a file.")
    ap.add_argument("--base-resume", action="store_true",
                    help="Skip tailoring: attach base résumé so jobs can auto-apply immediately (no API). "
                         "Default is to tailor each job.")
    ap.add_argument("--resume", default=str(RESUME_PDF), help="Résumé PDF to attach (default: base résumé).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    # Gather jobs
    jobs: list[dict] = []
    if args.url:
        j = parse_line(args.url)
        if j:
            jobs.append(j)
    if args.file:
        p = Path(args.file)
        if not p.exists():
            raise SystemExit(f"File not found: {p}")
        for line in p.read_text(encoding="utf-8").splitlines():
            j = parse_line(line)
            if j:
                jobs.append(j)
    if not jobs:
        raise SystemExit("No links provided. Pass a file or --url. See --help.")

    resume = Path(args.resume)
    attach_resume = args.base_resume  # default False -> tailor each job
    if attach_resume and not resume.exists():
        print(f"[!] Base résumé not found at {resume} — jobs will be added but you'll need to")
        print("    run tailoring (or fix --resume) before they can auto-apply.")
        attach_resume = False

    db = Path(args.db)
    if not db.exists() and not args.dry_run:
        raise SystemExit(f"DB not found: {db}\nRun ApplyPilot at least once so the database exists.")

    print("\n" + "=" * 66)
    print(f"  BATCH ADD — {len(jobs)} link(s)  {'(DRY RUN)' if args.dry_run else ''}")
    print(f"  mode: {'base résumé -> apply now (no API)' if attach_resume else 'tailor each job (enrich + tailor, uses API)'}")
    print("=" * 66)
    for j in jobs:
        print(f"  • {j['title'][:34]:34} | {j['company'][:16]:16} | {j['url'][:40]}")

    if args.dry_run:
        print("\n(dry run) re-run without --dry-run to write.\n")
        return

    conn = sqlite3.connect(str(db))
    now = datetime.now(timezone.utc).isoformat()
    added = updated = 0
    resume_val = str(resume) if attach_resume else None

    for j in jobs:
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, application_url, fit_score, score_reasoning, scored_at, tailored_resume_path) "
                "VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?, 10, 'MANUAL: user-selected', ?, ?)",
                (j["url"], j["title"], None, None, j["location"], j["company"],
                 now, j["apply_url"], now, resume_val),
            )
            added += 1
        except sqlite3.IntegrityError:
            # Already in DB — promote it to a manual apply-ready job.
            conn.execute(
                # Note: do NOT reset applied_at here — re-adding a link must
                # never silently un-apply a job that was already submitted.
                "UPDATE jobs SET application_url = COALESCE(NULLIF(application_url,''), ?), "
                "fit_score = 10, score_reasoning = 'MANUAL: user-selected', scored_at = ?, "
                "tailored_resume_path = COALESCE(tailored_resume_path, ?) "
                "WHERE url = ?",
                (j["apply_url"], now, resume_val, j["url"]),
            )
            updated += 1

    conn.commit()

    # How many are now genuinely apply-ready?
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE strategy='manual' AND applied_at IS NULL "
        "AND application_url IS NOT NULL AND tailored_resume_path IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    print(f"\nAdded {added}, updated {updated}.")
    if attach_resume:
        print(f"Manual jobs ready to auto-apply now: {ready}")
        print("\nNext — auto-apply (opens browser workers):")
        print("  applypilot apply --dry-run     # preview the queue")
        print("  applypilot apply               # submit all ready jobs")
        print('  applypilot apply --url "<one link>"   # just one')
    else:
        print(f"Queued for tailoring: {added + updated}")
        print("\nNext — fetch each job description + tailor + cover (uses API), then apply:")
        print("  caffeinate -i applypilot run enrich tailor cover pdf")
        print("  applypilot apply --dry-run     # preview the tailored queue")
        print("  applypilot apply               # submit")
        print("\nIf a page couldn't be read during enrich (login-walled / heavy JS),")
        print("paste its description so tailoring works:")
        print('  python3 add_job.py "<url>" --title "..." --company "..." --desc-file jd.txt')
    print()


if __name__ == "__main__":
    main()
