"""Is there any real work for a cycle right now?

Prints the three queue depths and exits 0 (work) / 1 (idle) so the hourly
scheduler can skip pointless runs (each run otherwise warms Ollama, spins
every stage, launches Chrome and pushes git for nothing).

"Claimable" mirrors launcher.acquire_job's predicate -- including the
per-company weekly throttle and the fit_score>=8 exemption -- because the
simpler ready-count over-reports jobs the worker can never actually claim
(throttled employers), which would make the scheduler run every hour forever.
"""
import os
import sqlite3
import sys

MIN_SCORE = int(os.environ.get("APPLY_MIN_SCORE", "7"))
MAX_ATTEMPTS = 3

conn = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"), timeout=30)

pending_score = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL AND full_description IS NOT NULL"
).fetchone()[0]

pending_tailor = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? AND full_description IS NOT NULL "
    "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5 "
    "AND COALESCE(apply_status, '') != 'deferred'",
    (MIN_SCORE,),
).fetchone()[0]

# Jobs still needing enrichment feed scoring, so they count as work too.
pending_enrich = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND fit_score IS NULL"
).fetchone()[0]

claimable = conn.execute(
    """
    SELECT COUNT(*) FROM jobs
    WHERE tailored_resume_path IS NOT NULL
      AND applied_at IS NULL
      AND (apply_status IS NULL
           OR (apply_status = 'failed'
               AND (last_attempted_at IS NULL
                    OR last_attempted_at < datetime('now', '-1 day'))))
      AND (apply_attempts IS NULL OR apply_attempts < ?)
      AND fit_score >= ?
      AND (COALESCE(company, site) IN ('linkedin', 'indeed', 'google')
           OR fit_score >= 8
           OR (COALESCE(company, site) NOT IN (
                   SELECT COALESCE(company, site) FROM jobs
                   WHERE apply_status = 'applied'
                     AND applied_at > datetime('now', '-7 days')
                   GROUP BY COALESCE(company, site)
                   HAVING COUNT(*) >= 3)
               AND (fit_score >= 8
                    OR COALESCE(company, site) NOT IN (
                        SELECT COALESCE(company, site) FROM jobs
                        WHERE apply_status = 'applied'
                          AND applied_at > datetime('now', '-7 days')))))
    """,
    (MAX_ATTEMPTS, MIN_SCORE),
).fetchone()[0]

total = pending_score + pending_tailor + pending_enrich + claimable
print(f"score={pending_score} tailor={pending_tailor} "
      f"enrich={pending_enrich} claimable={claimable} total={total}")
sys.exit(0 if total > 0 else 1)
