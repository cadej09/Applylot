"""One-shot queue fix: (1) reset filename-collision victims, (2) demote senior/junk titles.

Run in a SECOND PowerShell window while the cycle runs (it waits for DB locks):
    cd $HOME\job-ops ; .\.venv\Scripts\Activate.ps1 ; python fix-queue.py
"""
import re
import sqlite3
from pathlib import Path

DB = Path.home() / ".applypilot" / "applypilot.db"
conn = sqlite3.connect(DB, timeout=60)
conn.row_factory = sqlite3.Row

# ── Fix 1: filename collisions ────────────────────────────────────────────
# Multiple jobs sharing one tailored_resume_path: the file on disk matches only
# the NEWEST job (later writes overwrote earlier ones). Keep the newest, reset
# the stale ones so they get re-tailored with unique names next cycle.
groups = conn.execute("""
    SELECT tailored_resume_path AS p, COUNT(*) AS n FROM jobs
    WHERE tailored_resume_path IS NOT NULL AND tailored_resume_path != ''
    GROUP BY tailored_resume_path HAVING n > 1
""").fetchall()

stale_reset = 0
for g in groups:
    members = conn.execute(
        "SELECT url, title, apply_status FROM jobs "
        "WHERE tailored_resume_path=? ORDER BY tailored_at DESC",
        (g["p"],)).fetchall()
    keep = members[0]
    print(f"[COLLISION x{g['n']}] {g['p']}")
    print(f"   KEEP : {keep['title'][:60]}")
    for m in members[1:]:
        if m["apply_status"] == "applied":
            print(f"   SKIP (already applied): {m['title'][:60]}")
            continue
        conn.execute(
            "UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL, "
            "cover_letter_path=NULL, cover_letter_at=NULL WHERE url=?",
            (m["url"],))
        stale_reset += 1
        print(f"   RESET: {m['title'][:60]}")

# ── Fix 2: demote senior/junk titles out of the apply queue ──────────────
# The local 8B scorer let non-entry-level and irrelevant roles through at 7+.
# Demote to 6 so apply (>=7) skips them. Never touches already-applied jobs.
JUNK = re.compile(
    r"\b(distinguished|principal|staff|senior|sr\.?|lead|director|"
    r"post-?doc(toral)?|ph\.?d|fellow|"
    r"web scraper|zoho|interview engineer)\b",
    re.IGNORECASE)

candidates = conn.execute(
    "SELECT url, title, fit_score FROM jobs WHERE fit_score >= 6 "
    "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')"
).fetchall()

demoted = 0
for j in candidates:
    if JUNK.search(j["title"] or ""):
        conn.execute(
            "UPDATE jobs SET fit_score=6, "
            "score_reasoning=COALESCE(score_reasoning,'') || ' [demoted: senior/junk title]' "
            "WHERE url=?", (j["url"],))
        demoted += 1
        print(f"[DEMOTE {j['fit_score']}->6] {j['title'][:65]}")

conn.commit()

remaining = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE fit_score>=6 AND tailored_resume_path IS NOT NULL "
    "AND cover_letter_path IS NOT NULL AND apply_status IS NULL").fetchone()[0]
print(f"\nDone: {stale_reset} collision victims reset for re-tailor, {demoted} titles demoted.")
print(f"Ready-to-apply queue now: {remaining} jobs (all with correctly-matched files).")
conn.close()
