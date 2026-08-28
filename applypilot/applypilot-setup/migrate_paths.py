#!/usr/bin/env python3
"""Fix absolute file paths in applypilot.db after moving to a new machine
(e.g. Mac -> Windows). Run this ONCE on the new machine after copying
~/.applypilot into place.

The database stores absolute paths for each job's tailored résumé and cover
letter (e.g. /Users/cadejeong/.applypilot/tailored_resumes/X.pdf). Those point
at the old machine. This rewrites them to the current machine's .applypilot
folder, keeping the filenames.

Usage (Windows):   python migrate_paths.py
Usage (Mac/Linux): python3 migrate_paths.py
"""
from __future__ import annotations

import ntpath
import os
import sqlite3
from pathlib import Path

APP = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))
DB = APP / "applypilot.db"
TAILORED = APP / "tailored_resumes"
COVERS = APP / "cover_letters"


def _basename(p: str) -> str:
    # ntpath.basename splits on BOTH / and \, so it handles Mac paths on Windows.
    return ntpath.basename(p.replace("\\", "/"))


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"Database not found: {DB}\n"
                         "Copy your ~/.applypilot folder here first.")

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT url, tailored_resume_path, cover_letter_path FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL OR cover_letter_path IS NOT NULL"
    ).fetchall()

    fixed = 0
    for r in rows:
        updates, params = [], []
        trp = r["tailored_resume_path"]
        clp = r["cover_letter_path"]
        if trp:
            new = str(TAILORED / _basename(trp))
            if new != trp:
                updates.append("tailored_resume_path = ?"); params.append(new)
        if clp:
            new = str(COVERS / _basename(clp))
            if new != clp:
                updates.append("cover_letter_path = ?"); params.append(new)
        if updates:
            params.append(r["url"])
            conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE url = ?", params)
            fixed += 1

    conn.commit()

    # Sanity: how many of the referenced PDFs actually exist now?
    missing = 0
    for r in conn.execute(
        "SELECT tailored_resume_path FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ):
        pdf = str(r["tailored_resume_path"]).replace(".txt", ".pdf")
        if not Path(pdf).exists():
            missing += 1
    conn.close()

    print(f"Rewrote paths on {fixed} job(s) to: {APP}")
    if missing:
        print(f"[!] {missing} résumé PDF(s) referenced are missing — make sure you "
              f"copied {TAILORED} and {COVERS} too. You can also just re-run: "
              f"applypilot run tailor cover pdf")
    else:
        print("All referenced résumé PDFs found. You're good.")


if __name__ == "__main__":
    main()
