"""Build the human-review sheet: every good-fit job the bot could NOT submit.

User request 2026-08-14: "at the end of the pipeline I would like the full
dataset showing ones that failed but is a good fit along with the manual apply
ones and their resume + cover letter so that I can review them and apply for
those myself."

Why a file and not an email of ~260 attachments: the candidate set is ~130 jobs.
That exceeds Gmail's envelope, and an inbox full of PDFs is not reviewable. The
tailored PDFs already exist on this machine, so the sheet links to them directly
(file:// links open straight from the browser). The sheet is written every cycle
regardless of Gmail auth, so it keeps working when the OAuth token expires -
which is exactly when the emailed packet does not.

ASCII-only on purpose: this file gets edited from a Korean-locale PowerShell,
where a Set-Content round-trip silently replaced em-dashes and accents with "?"
and broke a string literal (2026-08-15). Keep it ASCII.

Output: ~/.applypilot/manual_review.html
"""
import html
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

APPLYPILOT = Path.home() / ".applypilot"
DB = APPLYPILOT / "applypilot.db"
OUT = APPLYPILOT / "manual_review.html"

# Reasons where a human cannot do better, or the job is gone / already handled.
SKIP_REASONS = (
    "expired", "not_eligible_location", "not_a_job_application", "work_auth",
    "already_applied", "duplicate", "no_job_url", "missing_job_url",
    "no_url_provided", "incomplete_job_info",
)

# What the bot hit -> what it means for a human doing it by hand.
MEANING = {
    "captcha": "CAPTCHA blocked the bot - trivial for you",
    "sso_required": "Needs SSO/social login - sign in and apply",
    "login_issue": "Account/login wall the bot could not pass",
    "unsafe_verification": "Wanted ID/biometric verification",
    "manual_ats": "ATS the bot cannot drive",
    "timeout": "Ran out of time - form may be long, not broken",
    "stuck": "Form validation defeated the bot (fields kept clearing)",
    "no_result_line": "Agent ended without reporting - outcome unknown",
    "unconfirmed": "May have submitted but could NOT confirm - CHECK BEFORE REAPPLYING",
    "page_error": "Page failed to load for the bot",
    "form_validation": "Form validation defeated the bot",
    "environment_limitation": "Bot environment could not complete it",
    "system_error": "Internal error during the attempt",
}


def meaning(err: str) -> str:
    e = (err or "").lower()
    for k, v in MEANING.items():
        if k in e:
            return v
    return (err or "unknown").split(":")[0][:60]


def pdf_for(rel: str | None) -> Path | None:
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = APPLYPILOT / p
    pdf = p.with_suffix(".pdf")
    return pdf if pdf.exists() else None


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def already_applied_index(con):
    """Index every applied job by description signature and title+employer.

    The sheet must never send you to re-apply where you already applied. The
    apply-stage dupe guard only fires at claim time, so a job that failed for an
    unrelated reason (captcha, timeout) can still be a re-listing of something
    already submitted - 5 of 130 were, on the first run (2026-08-15).
    """
    by_sig, by_tc = {}, {}
    for a in con.execute("""SELECT title, COALESCE(company,site) emp, company_key,
            dupe_sig, substr(applied_at,1,10) d FROM jobs
            WHERE apply_status='applied'"""):
        if a["dupe_sig"]:
            by_sig.setdefault(a["dupe_sig"], a)
        by_tc.setdefault((_norm(a["title"]), a["company_key"] or _norm(a["emp"])), a)
    return by_sig, by_tc


def main() -> None:
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    by_sig, by_tc = already_applied_index(con)
    rows = con.execute("""
        SELECT title, COALESCE(company, site) emp, fit_score, apply_status,
               company_key, dupe_sig, apply_error, tailored_resume_path,
               cover_letter_path, COALESCE(application_url, url) link,
               substr(COALESCE(last_attempted_at,''),1,10) tried
        FROM jobs
        WHERE applied_at IS NULL AND fit_score >= 6
          AND apply_status IN ('manual','failed','discarded')
        ORDER BY fit_score DESC, last_attempted_at DESC""").fetchall()

    items, dupes = [], 0
    for r in rows:
        if any(k in (r["apply_error"] or "").lower() for k in SKIP_REASONS):
            continue
        key = (_norm(r["title"]), r["company_key"] or _norm(r["emp"]))
        if (r["dupe_sig"] and r["dupe_sig"] in by_sig) or key in by_tc:
            dupes += 1
            continue
        resume = pdf_for(r["tailored_resume_path"])
        cover = pdf_for(r["cover_letter_path"])
        if not (resume and cover):
            continue                      # nothing to review without documents
        items.append((r, resume, cover))

    now = datetime.now(timezone.utc).astimezone()
    parts = [f"""<!doctype html><meta charset="utf-8">
<title>ApplyPilot - manual review ({len(items)} jobs)</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 -apple-system,Segoe UI,system-ui,sans-serif;
        margin: 2rem auto; max-width: 1100px; padding: 0 1rem; }}
 h1 {{ margin-bottom: .2rem; }} .sub {{ opacity:.7; margin-bottom:1.5rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th,td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #8883;
         vertical-align: top; }}
 th {{ position: sticky; top: 0; background: Canvas; }}
 .s {{ font-weight: 700; text-align: center; width: 2.5rem; }}
 .s9,.s10 {{ color:#0a0; }} .s8 {{ color:#3a0; }}
 .s7 {{ opacity:.85; }} .s6 {{ opacity:.65; }}
 .why {{ font-size: 13px; opacity: .8; }}
 .warn {{ color:#c60; font-weight:600; }}
 a.doc {{ font-size:12px; padding:.15rem .4rem; border:1px solid #8886;
          border-radius:4px; text-decoration:none; margin-right:.25rem;
          white-space:nowrap; }}
 tr:hover {{ background:#8881; }}
</style>
<h1>Jobs worth applying to by hand</h1>
<div class="sub">{len(items)} good-fit roles the pipeline could not submit
&middot; generated {now:%Y-%m-%d %H:%M} &middot; sorted by fit score
&middot; {dupes} already-applied duplicates excluded</div>
<table><thead><tr>
<th class="s">Fit</th><th>Role</th><th>Employer</th><th>Why it needs you</th>
<th>Documents</th><th>Tried</th></tr></thead><tbody>"""]

    for r, resume, cover in items:
        s = r["fit_score"]
        why = meaning(r["apply_error"])
        cls = "warn" if "CHECK BEFORE" in why else "why"
        parts.append(
            f'<tr><td class="s s{s}">{s}</td>'
            f'<td><a href="{html.escape(r["link"] or "")}" target="_blank">'
            f'{html.escape((r["title"] or "?")[:70])}</a></td>'
            f'<td>{html.escape((r["emp"] or "?")[:28])}</td>'
            f'<td class="{cls}">{html.escape(why)}</td>'
            f'<td><a class="doc" href="{resume.as_uri()}">resume</a>'
            f'<a class="doc" href="{cover.as_uri()}">cover</a></td>'
            f'<td class="why">{r["tried"] or "-"}</td></tr>')

    parts.append("</tbody></table>")
    OUT.write_text("\n".join(parts), encoding="utf-8")

    by_score: dict[int, int] = {}
    for r, _, _ in items:
        by_score[r["fit_score"]] = by_score.get(r["fit_score"], 0) + 1
    print(f"wrote {OUT}  ({len(items)} jobs, {dupes} already-applied excluded)")
    print("  " + "  ".join(f"score {k}: {v}"
                           for k, v in sorted(by_score.items(), reverse=True)))


if __name__ == "__main__":
    main()
