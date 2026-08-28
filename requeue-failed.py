"""One-time migration: requeue stranded failed jobs under the new policy.

Older runs slammed apply_attempts=99 (terminal) and left retryable jobs stuck
at apply_status='failed'/'in_progress'. This resets every non-terminal-good job
that still has documents (tailored_resume_path) and a real fit_score back into
the apply queue -- EXCEPT jobs whose recorded apply_error is a HARD_DROP reason,
which are marked 'discarded' (out of queue) instead.

Safe to run while nothing else is using the DB.
Usage (from job-ops, venv active):
    python requeue-failed.py
"""
import sqlite3
from pathlib import Path

# Must match launcher.HARD_DROP_REASONS. Retrying/hand-applying these is
# pointless, so they are discarded rather than requeued.
HARD_DROP_REASONS = {
    "already_applied",
    "not_a_job_application",
    "not_eligible_location",
    "not_eligible_work_auth",
    "unsafe_verification",
    "expired",
}


def is_hard_drop(apply_error: str | None) -> bool:
    """True if the recorded error names a HARD_DROP reason."""
    low = (apply_error or "").lower()
    return any(reason in low for reason in HARD_DROP_REASONS)


c = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db", timeout=30)
c.row_factory = sqlite3.Row

# Non-terminal-good rows with documents and a real score.
rows = c.execute(
    "SELECT url, title, apply_error FROM jobs "
    "WHERE tailored_resume_path IS NOT NULL "
    "  AND fit_score >= 6 "
    "  AND (apply_status IN ('failed', 'in_progress') "
    "       OR COALESCE(apply_attempts, 0) >= 99)"
).fetchall()

requeued = 0
discarded = 0
for r in rows:
    if is_hard_drop(r["apply_error"]):
        c.execute(
            "UPDATE jobs SET apply_status='discarded', agent_id=NULL WHERE url=?",
            (r["url"],),
        )
        print(f"  discarded (hard-drop): {r['title'][:65]}")
        discarded += 1
    else:
        c.execute(
            "UPDATE jobs SET apply_status=NULL, agent_id=NULL, apply_attempts=0 "
            "WHERE url=?",
            (r["url"],),
        )
        requeued += 1

c.commit()
print(f"\nrequeued {requeued}, discarded {discarded}")
c.close()
