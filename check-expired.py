"""Free pre-flight liveness check: retire dead postings BEFORE spawning agents.

Why this exists (measured 2026-08-13): of 248 apply-agent sessions over 14 days,
85 (34%) were spent discovering a posting had expired. Each one launches Chrome
plus a full Claude agent, loads the page, reads "no longer accepting
applications", and exits — a third of the plan's apply budget buying nothing.
Expired postings had a median age of 13.2 days vs 2.3 days for successful
applies, so they are both predictable and cheap to detect: one HTTP GET, $0, no
LLM, no browser.

CONSERVATIVE BY DESIGN — a false positive here silently deletes a real job, which
is far worse than wasting one agent session. A posting is retired ONLY on an
unambiguous signal:
  * HTTP 404 / 410 from the employer's own ATS
  * an explicit expiry phrase in the page text
Everything else is treated as ALIVE: timeouts, connection errors, 403/429
(LinkedIn and Indeed block datacenter IPs routinely — a block is not an expiry),
5xx, redirects, empty bodies, and any page we cannot parse. When in doubt the
job stays in the queue and the agent decides, exactly as before.

Aggregator hosts (linkedin/indeed/google) are SKIPPED entirely: they serve
soft-404 pages and bot walls that look identical to expiry, and their listing
pages outlive the underlying requisition. Only direct ATS URLs are checked.

Usage:
  python check-expired.py --dry-run      # report only, change nothing
  python check-expired.py                # retire confirmed-dead postings
  python check-expired.py --limit 50     # bound the pass
"""
import argparse
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DB = Path.home() / ".applypilot" / "applypilot.db"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Aggregators: soft-404s and bot walls are indistinguishable from expiry here.
SKIP_HOSTS = ("linkedin.com", "indeed.com", "google.com", "glassdoor",
              "ziprecruiter", "simplyhired", "jobright", "jobgether")

# Phrases that unambiguously mean "this requisition is closed". Deliberately
# narrow: anything that could appear on a LIVE posting is excluded. In
# particular "position filled" style wording only counts with the explicit
# no-longer/closed framing below.
EXPIRED_RX = re.compile(
    r"(no longer accepting applications"
    r"|this (job|position|posting|requisition) (is )?(no longer|has been) "
    r"(available|open|posted|active|accepting)"
    r"|position (has been|is) (filled|closed)"
    r"|posting (has )?(expired|closed)"
    r"|this (job|posting) (has )?expired"
    r"|applications? (are )?(now )?closed"
    r"|we are no longer accepting"
    r"|job posting not found"
    r"|requisition (is )?closed"
    r"|the job you.{0,20}looking for.{0,30}(no longer|not) (available|exists))",
    re.IGNORECASE)

# Pages that are alive but say something expiry-adjacent — never retire on these.
ALIVE_OVERRIDE_RX = re.compile(
    r"(apply now|apply for this|submit (your )?application|start your application"
    r"|application form|upload (your )?resume)", re.IGNORECASE)


def classify(url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (verdict, detail). verdict is 'dead' | 'alive' | 'unknown'.

    Only 'dead' ever retires a job. 'unknown' means we could not tell and the
    job is left completely untouched.
    """
    # Some rows carry a malformed application_url (including the literal
    # string "None"). Anything that isn't a real http(s) URL is left alone.
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return "unknown", "no usable url"
    low = url.lower()
    if any(h in low for h in SKIP_HOSTS):
        return "unknown", "aggregator host (skipped)"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400_000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        # 404/410 from a real ATS is the one status we trust as expiry.
        # 403/429/5xx are bot-blocks or outages — NOT expiry.
        if e.code in (404, 410):
            return "dead", f"HTTP {e.code}"
        return "unknown", f"HTTP {e.code}"
    except Exception as e:                       # timeout, DNS, TLS, reset...
        return "unknown", type(e).__name__

    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", text)
    m = EXPIRED_RX.search(text)
    if m:
        # An apply form on the same page means the phrase was boilerplate
        # (e.g. a "closed roles" sidebar) — keep the job.
        if ALIVE_OVERRIDE_RX.search(text):
            return "unknown", f"expiry phrase + live apply form ({m.group(0)[:40]!r})"
        return "dead", f"page says {m.group(0)[:60]!r}"
    return "alive", "no expiry signal"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--min-age-days", type=float, default=3.0,
                    help="only check postings older than this (fresh ones are "
                         "almost never dead; measured median age of an expired "
                         "posting is 13 days vs 2.3 for a successful apply)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT url, COALESCE(application_url, url) au, substr(title,1,42) t,
               COALESCE(company, site) emp, fit_score,
               round(julianday('now') - julianday(discovered_at), 1) age
        FROM jobs
        WHERE fit_score >= 6
          AND tailored_resume_path IS NOT NULL
          AND applied_at IS NULL
          AND COALESCE(apply_status,'') IN ('', 'failed')
          AND COALESCE(apply_attempts, 0) < 3
          AND julianday('now') - julianday(discovered_at) >= ?
        ORDER BY julianday('now') - julianday(discovered_at) DESC
        LIMIT ?""", (args.min_age_days, args.limit)).fetchall()

    print(f"checking {len(rows)} claimable postings older than "
          f"{args.min_age_days}d (aggregator URLs are skipped)\n")
    if not rows:
        return 0

    def work(r):
        verdict, detail = classify(r["au"])
        time.sleep(0.15)                          # be polite to employer ATSs
        return r, verdict, detail

    dead, alive, unknown = [], 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r, verdict, detail in ex.map(work, rows):
            if verdict == "dead":
                dead.append((r, detail))
                print(f"  DEAD  [{r['fit_score']}] {r['age']:5.1f}d  "
                      f"{r['t']:42s} | {(r['emp'] or '?')[:20]:20s} | {detail}")
            elif verdict == "alive":
                alive += 1
            else:
                unknown += 1

    print(f"\n  dead: {len(dead)}   alive: {alive}   "
          f"unknown/skipped (left alone): {unknown}")

    if args.dry_run:
        print("\n[dry-run] nothing changed")
        return 0
    if not dead:
        return 0

    for r, detail in dead:
        con.execute(
            "UPDATE jobs SET apply_status='discarded', "
            "apply_error='expired (pre-check: ' || ? || ')', "
            "last_attempted_at=datetime('now') WHERE url=?",
            (detail[:60], r["url"]))
    con.commit()
    print(f"\nretired {len(dead)} dead postings — that many agent sessions "
          f"NOT spent (~${len(dead) * 0.55:.2f} of apply budget preserved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
