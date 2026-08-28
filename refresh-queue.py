"""One-time queue refresh after the email switch (2026-07-08).

Everything tailored before today has owner@example.com baked into the
resume header and (for aggregator jobs) cover letters addressed to the
wrong company. This wipes those artifacts for UNAPPLIED jobs so the next
cycle regenerates them with applications@example.com + real employer
names. Applied jobs keep their history.

Usage (from job-ops, venv active):
    python refresh-queue.py
"""
import sqlite3
from pathlib import Path

DB = Path.home() / ".applypilot" / "applypilot.db"
c = sqlite3.connect(DB, timeout=30)

try:
    c.execute("ALTER TABLE jobs ADD COLUMN company TEXT")
    print("added company column")
except sqlite3.OperationalError:
    print("company column already present")

n = c.execute(
    "UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL, tailor_attempts=0, "
    "cover_letter_path=NULL, cover_letter_at=NULL, cover_attempts=0 "
    "WHERE apply_status IS NULL AND fit_score >= 6 "
    "AND tailored_resume_path IS NOT NULL"
).rowcount
c.commit()

ready = c.execute(
    "SELECT COUNT(*) FROM jobs WHERE apply_status IS NULL AND fit_score >= 6"
).fetchone()[0]
print(f"reset artifacts for {n} unapplied jobs; {ready} in the 7+ queue "
      f"will be re-tailored with the new email + real company names next cycle")
c.close()
