"""Send an email as applications@example.com via the Gmail API,
using the OAuth credentials from the gmail-mcp setup (copied into
~/.applypilot/gmail-send/, which is gitignored).

Works both on the Windows host and inside the Cowork Linux sandbox
(auto-discovers the mounted .applypilot path).

Usage:
  python send-status-email.py --to owner@example.com \
      --subject "Job Search Daily" --body-file /tmp/body.txt
  echo "body text" | python send-status-email.py --to x@y.com --subject "Hi"
"""
import argparse
import base64
import glob
import json
import sys
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path


def find_creds_dir() -> Path:
    candidates = [
        # ~/.gmail-mcp FIRST: it is what `npx ... auth` rewrites, so a re-auth
        # propagates here automatically. A stale hand-copy in gmail-send caused
        # days of false "token expired" failures after the token was fixed
        # (2026-08-15).
        Path.home() / ".gmail-mcp",
        Path.home() / ".applypilot" / "gmail-send",              # Windows host
        *[Path(p) for p in glob.glob("/sessions/*/mnt/.applypilot/gmail-send")],
        # Mounted job-ops fallback: works in Cowork sessions where only the
        # job-ops folder is mounted (home dir / top-level .applypilot absent).
        # Place gcp-oauth.keys.json + credentials.json here to enable sending.
        Path(__file__).resolve().parent / "applypilot" / ".applypilot" / "gmail-send",
        *[Path(p) for p in glob.glob("/sessions/*/mnt/job-ops/applypilot/.applypilot/gmail-send")],
    ]
    for c in candidates:
        if (c / "credentials.json").exists() and (c / "gcp-oauth.keys.json").exists():
            return c
    sys.exit("ERROR: gmail-send credentials not found. Place gcp-oauth.keys.json and "
             "credentials.json in ~/.applypilot/gmail-send/ (host) or "
             "job-ops/applypilot/.applypilot/gmail-send/ (mounted, persists across sessions).")


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


def send(token: str, to: str, subject: str, body: str) -> str:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps({"raw": raw}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("id", "?")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402 (used in get_access_token)
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", default=None)
    args = ap.parse_args()
    body = (Path(args.body_file).read_text(encoding="utf-8")
            if args.body_file else sys.stdin.read())
    creds_dir = find_creds_dir()
    msg_id = send(get_access_token(creds_dir), args.to, args.subject, body)
    print(f"sent: {msg_id}")
