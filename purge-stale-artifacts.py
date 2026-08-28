"""Reset ALL artifacts on unapplied 7+ jobs so the next cycle regenerates
them under the current quality rules (Harvard cover format, enforced date
line, truth caps, hard word ceiling, NIM-tailor / Claude-CLI-cover split).

Applied jobs are untouched. Old files stay on disk/git as history; the DB
pointers are cleared so tailor/cover stages rebuild everything.

Usage (job-ops, venv active, no cycle running):  python purge-stale-artifacts.py
"""
import sqlite3

c = sqlite3.connect(r"C:\Users\you\.applypilot\applypilot.db", timeout=30)

n = c.execute(
    "UPDATE jobs SET "
    "  tailored_resume_path=NULL, tailored_at=NULL, tailor_attempts=0, "
    "  cover_letter_path=NULL, cover_letter_at=NULL, cover_attempts=0 "
    "WHERE fit_score >= 6 "
    "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress') "
    "AND (tailored_resume_path IS NOT NULL OR cover_letter_path IS NOT NULL)"
).rowcount
c.commit()

left = c.execute(
    "SELECT COUNT(*) FROM jobs WHERE fit_score >= 6 "
    "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchone()[0]
c.close()
print(f"purged artifacts on {n} unapplied jobs; {left} jobs in the 7+ queue "
      f"will be freshly tailored + covered next cycle")
