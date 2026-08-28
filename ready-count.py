"""Print the number of jobs the apply stage could claim right now (side-apply gate)."""
import os
import sqlite3

conn = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"), timeout=30)
n = conn.execute(
    "SELECT COUNT(*) FROM jobs "
    "WHERE tailored_resume_path IS NOT NULL AND fit_score >= 6 "
    "AND applied_at IS NULL "
    "AND (apply_status IS NULL "
    "     OR (apply_status = 'failed' "
    "         AND (last_attempted_at IS NULL "
    "              OR last_attempted_at < datetime('now', '-1 day')))) "
    "AND COALESCE(apply_attempts, 0) < 3"
).fetchone()[0]
print(n)
