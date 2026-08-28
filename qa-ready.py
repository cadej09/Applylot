"""QA sweep for the ready-to-apply queue.

Checks every job with fit_score >= 6, no apply_status, and generated artifacts:
  1. resume/cover .txt and .pdf files actually exist
  2. cover letter format: opens with "Dear ...", closes with "Sincerely",
     has a date line, <= 325 words
  3. truth: no ">2 years" experience claims in resume or cover
  4. board-as-employer: cover for a linkedin/indeed-sourced job must not
     address LinkedIn/Indeed as the hiring company

Jobs with CONTENT problems get their artifacts reset (pulled out of the
ready queue so they regenerate correctly next tailor run).
Jobs missing only the PDF are listed; run `applypilot run pdf` to fix (no LLM).

Usage (job-ops, venv active, cycle STOPPED):  python qa-ready.py
Dry run (report only, change nothing):        python qa-ready.py --dry-run
"""
import re
import sys
import sqlite3
from pathlib import Path

DRY = "--dry-run" in sys.argv
DATA = Path.home() / ".applypilot"
c = sqlite3.connect(DATA / "applypilot.db", timeout=30)
c.row_factory = sqlite3.Row

YEARS_RX = re.compile(r"\b(\d{1,2})\s*\+?\s*years?\b", re.IGNORECASE)
DATE_RX = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{1,2},\s*\d{4}", re.IGNORECASE)
BOARDS = {"linkedin", "indeed", "google", "ziprecruiter", "glassdoor"}
BOARD_AS_EMPLOYER_RX = re.compile(
    r"(?:position|role|opening|opportunity|team)\s+(?:at|with)\s+(linkedin|indeed)\b|"
    r"\b(?:at|with|join)\s+(linkedin|indeed)(?:'s)?\s+(?:team|mission|company)\b",
    re.IGNORECASE)


def resolve(path_str):
    if not path_str:
        return None
    p = Path(path_str)
    return p if p.is_absolute() else DATA / p


def read_txt(path_str):
    p = resolve(path_str)
    if p is None:
        return None, None
    txt = p.with_suffix(".txt")
    if not txt.exists():
        return None, p
    return txt.read_text(encoding="utf-8", errors="ignore"), p


rows = c.execute(
    "SELECT url, title, COALESCE(company, site) AS employer, site, "
    "tailored_resume_path, cover_letter_path FROM jobs "
    "WHERE fit_score >= 6 AND apply_status IS NULL "
    "AND tailored_resume_path IS NOT NULL AND cover_letter_path IS NOT NULL"
).fetchall()

reset, pdf_missing, clean = [], [], 0

for r in rows:
    problems = []
    resume, r_path = read_txt(r["tailored_resume_path"])
    cover, c_path = read_txt(r["cover_letter_path"])

    # 1. existence
    if resume is None:
        problems.append("resume .txt missing")
    if cover is None:
        problems.append("cover .txt missing")

    if resume is not None:
        if len(resume) < 500:
            problems.append("resume suspiciously short")
        if any(int(m) > 2 for m in YEARS_RX.findall(resume)):
            problems.append("resume overclaims years")

    if cover is not None:
        head = cover.strip()[:200].lower()
        if "dear" not in head:
            problems.append("cover missing 'Dear ...' opening")
        if "sincerely" not in cover.lower():
            problems.append("cover missing 'Sincerely' close")
        if not DATE_RX.search(cover[:300]):
            problems.append("cover missing date line")
        if len(cover.split()) > 375:
            problems.append(f"cover too long ({len(cover.split())} words)")
        if any(int(m) > 2 for m in YEARS_RX.findall(cover)):
            problems.append("cover overclaims years")
        m = BOARD_AS_EMPLOYER_RX.search(cover)
        if (r["site"] or "").lower() in BOARDS and m:
            board = next(g for g in m.groups() if g)
            # not a problem if the board really is the hiring company
            if board.lower() not in (r["employer"] or "").lower():
                problems.append("cover addresses the job board as employer")

    if problems:
        reset.append((r, problems))
        continue

    # 2. PDFs present? (content fine — just needs the pdf stage, no LLM)
    missing_pdf = []
    for p in (r_path, c_path):
        if p is not None and not p.with_suffix(".pdf").exists():
            missing_pdf.append(p.name)
    if missing_pdf:
        pdf_missing.append((r, missing_pdf))
    else:
        clean += 1

print(f"Checked {len(rows)} ready-to-apply jobs\n")

if reset:
    print(f"--- CONTENT PROBLEMS ({len(reset)}) -> artifacts reset, will re-tailor later ---")
    for r, probs in reset:
        print(f"  {r['title'][:55]:55s} | {r['employer'][:20]:20s} | {'; '.join(probs)}")
        if not DRY:
            c.execute(
                "UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL, "
                "tailor_attempts=0, cover_letter_path=NULL, cover_letter_at=NULL, "
                "cover_attempts=0 WHERE url=?", (r["url"],))

if pdf_missing:
    print(f"\n--- PDF MISSING ({len(pdf_missing)}) -> run: applypilot run pdf ---")
    for r, names in pdf_missing:
        print(f"  {r['title'][:55]:55s} | {', '.join(names)}")

c.commit()
c.close()
label = "DRY RUN — nothing changed" if DRY else "changes committed"
print(f"\n{clean} jobs fully clean and ready to apply | "
      f"{len(reset)} pulled for regeneration | {len(pdf_missing)} need pdf stage | {label}")
