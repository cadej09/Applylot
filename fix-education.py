"""Retroactively demote queued jobs that hard-require a graduate degree.

The candidate has a Bachelor's. Safe to run while apply is active.
Usage (from job-ops, venv active):
    python fix-education.py
"""
import re
import sqlite3
from pathlib import Path

_GRAD_RX = re.compile(
    r"(master'?s?\s+degree|master'?s\b|ph\.?\s?d|m\.s\.|m\.b\.a\.|\bmba\b|msc\b|"
    r"doctorate|doctoral|graduate degree|graduate-level|advanced degree)"
    r"[^.\n]{0,60}?(is required|required|must have|must hold|minimum|is expected)",
    re.IGNORECASE,
)


def requires_grad(desc: str) -> bool:
    for sentence in re.split(r"[.\n]", desc or ""):
        low = sentence.lower()
        if "preferred" in low or "bachelor" in low or "or equivalent" in low:
            continue
        if _GRAD_RX.search(sentence):
            return True
    return False


c = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db", timeout=30)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT url, title, full_description FROM jobs "
    "WHERE fit_score >= 6 AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')"
).fetchall()

n = 0
for r in rows:
    if requires_grad(r["full_description"]):
        c.execute(
            "UPDATE jobs SET fit_score=5, score_reasoning=COALESCE(score_reasoning,'') "
            "|| ' [capped at 6: JD requires a graduate degree, candidate has a Bachelor''s]' "
            "WHERE url=?", (r["url"],))
        print(f"  demoted: {r['title'][:70]}")
        n += 1

c.commit()
left = c.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 6 "
                 "AND apply_status IS NULL").fetchone()[0]
print(f"\ndemoted {n} grad-degree-required jobs; {left} remain in the apply queue")
c.close()
