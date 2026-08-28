"""Repair the ready-to-apply cover letters split into two groups:

  PATCH  — proper Harvard-format letters that are only missing the date line
           (Gemini skips it): prepend today's date to the .txt, delete the
           stale .pdf so the pdf stage re-renders it. Zero LLM calls.
  RESET  — old-format letters (no 'Sincerely' close) or bloated ones
           (> 375 words): clear ONLY the cover fields so `applypilot run
           cover pdf` regenerates them. Tailored resumes are kept.

Usage (job-ops, venv active, cycle stopped):  python fix-covers.py
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DATA = Path.home() / ".applypilot"
c = sqlite3.connect(DATA / "applypilot.db", timeout=30)
c.row_factory = sqlite3.Row

DATE_RX = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{1,2},\s*\d{4}", re.IGNORECASE)
_n = datetime.now()
TODAY = f"{_n.strftime('%B')} {_n.day}, {_n.year}"


def resolve(path_str):
    p = Path(path_str)
    return p if p.is_absolute() else DATA / p


rows = c.execute(
    "SELECT url, title, cover_letter_path FROM jobs "
    "WHERE fit_score >= 6 AND apply_status IS NULL "
    "AND cover_letter_path IS NOT NULL"
).fetchall()

patched, reset, ok, gone = 0, 0, 0, 0
for r in rows:
    p = resolve(r["cover_letter_path"])
    txt = p.with_suffix(".txt")
    if not txt.exists():
        gone += 1
        c.execute("UPDATE jobs SET cover_letter_path=NULL, cover_letter_at=NULL, "
                  "cover_attempts=0 WHERE url=?", (r["url"],))
        continue

    body = txt.read_text(encoding="utf-8", errors="ignore")
    words = len(body.split())

    if "sincerely" not in body.lower() or words > 375:
        reset += 1
        c.execute("UPDATE jobs SET cover_letter_path=NULL, cover_letter_at=NULL, "
                  "cover_attempts=0 WHERE url=?", (r["url"],))
        continue

    if not DATE_RX.search(body[:300]):
        txt.write_text(f"{TODAY}\n\n{body.lstrip()}", encoding="utf-8")
        pdf = p.with_suffix(".pdf")
        if pdf.exists():
            pdf.unlink()
        patched += 1
    else:
        ok += 1

c.commit()
c.close()
print(f"patched date into {patched} letters (pdfs deleted for re-render)")
print(f"reset {reset} old-format/overlong covers for regeneration (resumes kept)")
print(f"{ok} already perfect, {gone} had missing files (cover reset)")
print("\nNext:  applypilot run cover pdf   (Vertex regenerates the reset ones, "
      "pdf re-renders the patched ones)")
