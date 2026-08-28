#!/usr/bin/env python3
"""Fast preview of the auto-apply queue — READ ONLY, no browser, no API.

`applypilot apply --dry-run` actually opens Chrome and runs the full agent on
each job (it just skips the final Submit click), so it's slow. When you only
want to SEE what would be submitted and which résumé/cover goes with each job,
use this instead — it prints instantly.

A job is "ready to apply" when it has: a tailored résumé, an application URL,
fit_score >= min, and hasn't been applied yet.

Usage:
    python3 apply_queue.py                 # show the ready queue
    python3 apply_queue.py --all           # also show applied / not-ready
    python3 apply_queue.py --min-score 7
    python3 apply_queue.py --db /path/to/applypilot.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")) / "applypilot.db"


def base(p: str | None) -> str:
    return os.path.basename(p).rsplit(".", 1)[0] if p else "—"


# ATS type -> how well the auto-apply agent handles it (from real results).
_AUTO = ("greenhouse.io", "lever.co", "ashbyhq.com", "ashby_jid", "workable.com",
         "smartrecruiters.com", "breezy.hr", "applytojob.com", "jobvite.com")
_HARD = ("myworkdayjobs.com", "icims.com", "taleo.net", "taleo.com",
         "careers.microsoft", "lifeattiktok", "tiktok.com", "deloitte.com",
         "workday", "pse.com", "zs.com", "rippling.com")


def confidence(url: str | None) -> str:
    """AUTO = single-page ATS the bot finishes reliably; HAND = multi-page/login
    ATS that usually needs you; CHECK = unknown."""
    u = (url or "").lower()
    if any(d in u for d in _AUTO):
        return "AUTO"
    if any(d in u for d in _HARD):
        return "HAND"
    return "CHECK"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--min-score", type=int, default=7)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    ready = conn.execute(
        "SELECT title, site, fit_score, application_url, tailored_resume_path, "
        "cover_letter_path, location FROM jobs "
        "WHERE applied_at IS NULL AND application_url IS NOT NULL "
        "AND tailored_resume_path IS NOT NULL AND fit_score >= ? "
        # match what `applypilot apply` will actually pick: skip parked/expired/in-progress
        "AND (apply_status IS NULL OR apply_status = 'failed') "
        "ORDER BY fit_score DESC, site",
        (args.min_score,),
    ).fetchall()

    print("\n" + "=" * 78)
    print(f"  APPLY QUEUE — {len(ready)} job(s) ready to submit (fit_score >= {args.min_score})")
    print("=" * 78)
    print(f"  {'how':5} {'score':>5}  {'company':16} {'title':28} {'résumé used':20} cover")
    print("  " + "-" * 76)
    auto_jobs = []
    for r in ready:
        tag = confidence(r["application_url"])
        if tag == "AUTO":
            auto_jobs.append(r)
        print(f"  {tag:5} {str(r['fit_score'] or ''):>5}  {(r['site'] or '')[:16]:16} "
              f"{(r['title'] or '')[:28]:28} {base(r['tailored_resume_path'])[:20]:20} "
              f"{'yes' if r['cover_letter_path'] else '—'}")
        print(f"         {(r['application_url'] or '')[:100]}")
    print("\n  AUTO = bot finishes reliably (Greenhouse/Lever/Ashby)")
    print("  HAND = multi-page/login ATS (Workday/iCIMS/Taleo/Microsoft) — do yourself")
    print("  CHECK = unknown — try one and see")
    print(f"\n  -> {len(auto_jobs)} AUTO-friendly job(s). Let the bot do just those:")
    print("     applypilot apply -w 2      (it'll still try all; Ctrl+C the HAND ones,")
    print("     or apply one AUTO job at a time with:  applypilot apply --url \"<link>\")")

    # Not-ready diagnostics
    applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0]
    no_resume = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NULL AND fit_score >= ? "
        "AND application_url IS NOT NULL AND tailored_resume_path IS NULL", (args.min_score,)
    ).fetchone()[0]
    no_url = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NULL AND fit_score >= ? "
        "AND application_url IS NULL", (args.min_score,)
    ).fetchone()[0]

    print("\n  not-ready:")
    print(f"    already applied ......... {applied}")
    print(f"    score>= but no résumé ... {no_resume}  (run: applypilot run tailor cover pdf)")
    print(f"    score>= but no apply URL  {no_url}  (LinkedIn/Indeed etc. — apply by hand)")

    if args.all and applied:
        print("\n  already applied:")
        for r in conn.execute(
            "SELECT title, site, applied_at, tailored_resume_path FROM jobs "
            "WHERE applied_at IS NOT NULL ORDER BY applied_at DESC LIMIT 30"
        ):
            print(f"    {(r['applied_at'] or '')[:10]}  {(r['site'] or '')[:16]:16} "
                  f"{(r['title'] or '')[:30]:30} résumé: {base(r['tailored_resume_path'])}")

    print("\n  to actually submit:  applypilot apply        (add -w 2 to parallelize)")
    print("  one job only:        applypilot apply --url \"<link>\"\n")


if __name__ == "__main__":
    main()
