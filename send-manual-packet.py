"""Email a manual-apply packet: jobs parked apply_status='manual' (agents hit
the attempt cap on 8+ scores) with their tailored resume + cover letter PDFs
attached, plus the current internship review list. Sent as
applications@example.com -> owner@example.com via the Gmail API using the
same OAuth creds as send-status-email.py (~/.applypilot/gmail-send/).

State: ~/.applypilot/manual_packet_state.json remembers which job URLs were
already sent (and the internship-list hash) so each run only mails what's new.

Usage:
  python send-manual-packet.py --dry-run     # show what would be sent
  python send-manual-packet.py               # send new items (default 14-day window)
  python send-manual-packet.py --days 60     # widen the window
  python send-manual-packet.py --resend-all  # ignore state, resend everything in window
"""
import argparse
import base64
import hashlib
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

APPLYPILOT = Path.home() / ".applypilot"
DB = APPLYPILOT / "applypilot.db"
STATE = APPLYPILOT / "manual_packet_state.json"
INTERNSHIPS = APPLYPILOT / "internship_review.md"
TO = "owner@example.com"
MAX_ATTACH_BYTES = 20 * 1024 * 1024  # stay under Gmail's 25 MB envelope


def find_creds_dir() -> Path:
    """Prefer ~/.gmail-mcp — the directory the `npx ... auth` flow actually writes.

    2026-08-15: re-authing refreshed ~/.gmail-mcp but left the hand-made copy in
    ~/.applypilot/gmail-send/ stale, so this script kept failing on a dead token
    for days AFTER the token had been fixed. Read the authoritative location
    first so a re-auth propagates automatically; keep gmail-send as a fallback
    for sandbox/mounted runs where ~/.gmail-mcp is absent.
    """
    for c in (Path.home() / ".gmail-mcp", APPLYPILOT / "gmail-send"):
        if (c / "credentials.json").exists() and (c / "gcp-oauth.keys.json").exists():
            return c
    sys.exit("ERROR: gmail credentials not found in ~/.gmail-mcp/ or "
             "~/.applypilot/gmail-send/ — run: "
             "npx @gongrzhe/server-gmail-autoauth-mcp auth")


def get_access_token(creds_dir: Path) -> str:
    keys = json.loads((creds_dir / "gcp-oauth.keys.json").read_text())
    client = keys.get("installed") or keys.get("web")
    creds = json.loads((creds_dir / "credentials.json").read_text())
    data = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"sent_urls": [], "internship_hash": ""}


def pdf_for(rel_path: str) -> Path | None:
    if not rel_path:
        return None
    p = Path(rel_path)
    if not p.is_absolute():
        p = APPLYPILOT / p
    pdf = p.with_suffix(".pdf")
    return pdf if pdf.exists() else None


def collect_jobs(days: int, skip_urls: set) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT url, title, company, fit_score, tailored_resume_path,
                  cover_letter_path, apply_error, last_attempted_at,
                  COALESCE(application_url, url) AS apply_link
           FROM jobs
           WHERE apply_status='manual' AND last_attempted_at >= ?
           ORDER BY fit_score DESC, last_attempted_at DESC""", (cutoff,)).fetchall()
    jobs = []
    for r in rows:
        if r["url"] in skip_urls:
            continue
        jobs.append({
            "url": r["url"],
            "title": r["title"] or "?",
            "company": r["company"] or "?",
            "score": r["fit_score"],
            "why": (r["apply_error"] or "?").split(":")[0][:60],
            "when": (r["last_attempted_at"] or "")[:10],
            "link": r["apply_link"],
            "resume_pdf": pdf_for(r["tailored_resume_path"]),
            "cover_pdf": pdf_for(r["cover_letter_path"]),
        })
    return jobs


def build_body(jobs: list[dict], internships_changed: bool) -> str:
    lines = ["MANUAL-APPLY PACKET", ""]
    lines.append("These scored 8+ but the agent burned all 3 attempts "
                 "(captcha/timeout/login walls). Tailored PDFs attached, "
                 "named <n>_resume.pdf / <n>_cover.pdf:")
    lines.append("")
    for i, j in enumerate(jobs, 1):
        lines.append(f"{i}. [{j['score']}] {j['title']} @ {j['company']}")
        lines.append(f"   blocked by: {j['why']} (last try {j['when']})")
        lines.append(f"   apply: {j['link']}")
        missing = [k for k in ("resume_pdf", "cover_pdf") if not j[k]]
        if missing:
            lines.append(f"   NOTE: {', '.join(m.split('_')[0] for m in missing)} PDF missing on disk")
        lines.append("")
    if internships_changed and INTERNSHIPS.exists():
        lines.append("=" * 60)
        lines.append("INTERNSHIP REVIEW LIST (updated since last packet)")
        lines.append("=" * 60)
        lines.append(INTERNSHIPS.read_text(encoding="utf-8"))
    return "\n".join(lines)


def build_message(jobs: list[dict], body: str) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["to"] = TO
    msg["subject"] = (f"ApplyPilot manual packet: {len(jobs)} job(s) need a human"
                      if jobs else "ApplyPilot: internship review list updated")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    total = 0
    for i, j in enumerate(jobs, 1):
        for kind, key in (("resume", "resume_pdf"), ("cover", "cover_pdf")):
            p = j[key]
            if not p:
                continue
            data = p.read_bytes()
            if total + len(data) > MAX_ATTACH_BYTES:
                print(f"  size cap: skipping attachments from job {i} on")
                return msg
            total += len(data)
            part = MIMEApplication(data, _subtype="pdf")
            part.add_header("Content-Disposition", "attachment",
                            filename=f"{i:02d}_{j['company'][:20].replace(' ', '_')}_{kind}.pdf")
            msg.attach(part)
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--resend-all", action="store_true")
    args = ap.parse_args()

    state = load_state()
    skip = set() if args.resend_all else set(state["sent_urls"])
    jobs = collect_jobs(args.days, skip)

    # Hash the list CONTENT only — the "_Generated <timestamp>" line changes
    # every cycle and would otherwise trigger an email every night.
    if INTERNSHIPS.exists():
        content = "\n".join(l for l in INTERNSHIPS.read_text(encoding="utf-8").splitlines()
                            if not l.startswith("_Generated"))
        ihash = hashlib.sha256(content.encode()).hexdigest()
    else:
        ihash = ""
    internships_changed = ihash != state.get("internship_hash", "")

    if not jobs and not internships_changed:
        print("nothing new to send")
        return

    body = build_body(jobs, internships_changed)
    if args.dry_run:
        print(f"would send {len(jobs)} job(s), internships_changed={internships_changed}")
        for j in jobs:
            att = sum(1 for k in ("resume_pdf", "cover_pdf") if j[k])
            print(f"  [{j['score']}] {j['title'][:40]} @ {j['company'][:25]} "
                  f"({att}/2 PDFs, {j['why']})")
        return

    msg = build_message(jobs, body)
    token = get_access_token(find_creds_dir())
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps({"raw": raw}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        print(f"sent: {json.loads(r.read()).get('id', '?')} "
              f"({len(jobs)} jobs, internships_changed={internships_changed})")

    state["sent_urls"] = sorted(set(state["sent_urls"]) | {j["url"] for j in jobs})
    state["internship_hash"] = ihash
    STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
