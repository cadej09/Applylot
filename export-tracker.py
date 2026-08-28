"""Export the job tracker to CSV for browsing in Excel.

Usage (from job-ops, venv active):
    python export-tracker.py

Writes to Desktop:
    job-tracker.csv      — every job you've engaged with (tailored/applied/failed)
    job-tracker-all.csv  — the full 5,000+ discovered dataset
"""
import csv
import sqlite3
from pathlib import Path

DB = Path.home() / ".applypilot" / "applypilot.db"
DESKTOP = Path.home() / "Desktop"

COLS = [
    "apply_status", "fit_score", "title", "site", "location",
    "applied_at", "tailored", "cover_letter", "apply_error",
    "url", "application_url", "tailored_resume_path", "cover_letter_path",
    "scored_at", "discovered_at",
]

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT apply_status, fit_score, title, site, location, applied_at,
           CASE WHEN tailored_resume_path IS NOT NULL THEN 'yes' ELSE '' END AS tailored,
           CASE WHEN cover_letter_path IS NOT NULL THEN 'yes' ELSE '' END AS cover_letter,
           apply_error, url, application_url,
           tailored_resume_path, cover_letter_path, scored_at, discovered_at
    FROM jobs
    ORDER BY
        CASE apply_status WHEN 'applied' THEN 0 ELSE 1 END,
        applied_at DESC,
        fit_score DESC
""").fetchall()

def write(path: Path, data) -> int:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in data:
            w.writerow([r[c] for c in COLS])
    return len(data)

engaged = [r for r in rows if r["tailored"] or (r["apply_status"] or "") != ""]
n1 = write(DESKTOP / "job-tracker.csv", engaged)
n2 = write(DESKTOP / "job-tracker-all.csv", rows)

# Manual-apply queue: jobs the bot could NOT finish (failed, phantom-rejected,
# CAPTCHA walls, login walls, manual-only ATS). Resume + cover letter PDFs are
# already generated — open the url, attach the files, submit by hand.
MANUAL_STATUSES = {"failed", "manual", "captcha", "login_issue"}
manual = sorted(
    (r for r in rows
     if (r["apply_status"] or "") in MANUAL_STATUSES and r["tailored"]),
    key=lambda r: -(r["fit_score"] or 0),
)
n3 = write(DESKTOP / "manual-apply.csv", manual)

applied = sum(1 for r in rows if r["apply_status"] == "applied")
ready = sum(1 for r in rows if r["tailored"] and r["cover_letter"] and not r["apply_status"])
print(f"job-tracker.csv      -> {n1} engaged jobs (applied: {applied}, ready to apply: {ready})")
print(f"job-tracker-all.csv  -> {n2} total discovered jobs")
print(f"manual-apply.csv     -> {n3} jobs for hand-application (best fit first; "
      f"resume/cover PDFs listed in the path columns)")
print(f"Saved to {DESKTOP}")
conn.close()
