"""Print how many jobs the tailor stage could pick up right now (side-tailor gate)."""
import os
import sqlite3

conn = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"), timeout=30)
n = conn.execute(
    "SELECT COUNT(*) FROM jobs "
    "WHERE fit_score >= 6 AND full_description IS NOT NULL "
    "AND tailored_resume_path IS NULL "
    "AND COALESCE(tailor_attempts, 0) < 5 "
    "AND COALESCE(apply_status, '') != 'deferred'"
).fetchone()[0]
print(n)
