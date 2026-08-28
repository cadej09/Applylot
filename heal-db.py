"""Heal DB: reset error-scores, unstick in_progress, retire stale, dedupe reposts.

Extracted from run-cycle.ps1's stage-2 here-string (2026-07-16): piping the
here-string into `python -` picked up a BOM (U+FEFF) on some consoles and the
whole heal stage silently SyntaxError'd. A real file sidesteps the encoding
entirely (golden rule 5).
"""
import hashlib
import re
import sqlite3

c = sqlite3.connect(r"C:\Users\you\.applypilot\applypilot.db")
c.row_factory = sqlite3.Row

# Reset only genuine scoring ERRORS (fit_score=0) back to unscored. Rows the
# zero-API prefilter stamped fit_score=0 with a 'PREFILTERED' reason are
# deliberate junk marks — preserve them so the scorer keeps skipping them for
# free instead of re-scoring every cycle.
n1 = c.execute(
    "UPDATE jobs SET fit_score=NULL "
    "WHERE fit_score=0 AND COALESCE(score_reasoning,'') NOT LIKE '%PREFILTER%'"
).rowcount
n2 = c.execute(
    "UPDATE jobs SET apply_status=NULL, agent_id=NULL WHERE apply_status='in_progress'"
).rowcount

# Idempotent schema upgrades (response tracking + real employer name + dupe cols)
for col in ("response_status TEXT", "company TEXT", "company_key TEXT", "dupe_sig TEXT"):
    try:
        c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass  # already exists

# Stale-job retirement: unapplied postings older than 30 days are usually
# filled or buried; retire them so the queue stays fresh.
n4 = c.execute(
    "UPDATE jobs SET apply_status='expired', apply_error='stale: posted >30 days ago' "
    "WHERE fit_score >= 6 AND apply_status IS NULL "
    "AND discovered_at < datetime('now', '-30 days')"
).rowcount

# Company backfill for mega-employers hiding in the aggregator bucket: rows
# with no extracted company fall back to site (linkedin/indeed) and were both
# EXEMPT from the per-company cap and invisible to dedup. Stamp the obvious
# ones so the cap can see them. (2026-07-19 — Amazon mass-apply fix.)
_EMPLOYER_RX = (
    ("Amazon.com", re.compile(r"\bamazon\b|\baws\b|amazon\.jobs", re.I)),
    ("TikTok", re.compile(r"\btiktok\b|\bbytedance\b", re.I)),
    ("Google", re.compile(r"\bgoogle\b|\balphabet\b", re.I)),
    ("Microsoft", re.compile(r"\bmicrosoft\b", re.I)),
    ("Meta", re.compile(r"\bmeta\b.{0,20}(platforms|careers)|facebook careers", re.I)),
)
n5 = 0
for r in c.execute(
        "SELECT url, title, substr(COALESCE(full_description,''),1,400) d FROM jobs "
        "WHERE company IS NULL AND fit_score >= 6 "
        "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall():
    text = f"{r['title']} {r['d']}"
    for name, rx in _EMPLOYER_RX:
        if rx.search(text):
            c.execute("UPDATE jobs SET company=? WHERE url=?", (name, r["url"]))
            n5 += 1
            break

# Normalize employer-name VARIANTS so the per-company cap can't be split
# across spellings ('Amazon' vs 'Amazon.com' vs 'Amazon Web Services (AWS)'
# each got an independent 3-per-week budget). Applied rows included on
# purpose: the cap counts them.
_CANON = (
    ("Amazon.com", re.compile(r"^amazon|^aws\b|amazon web services", re.I)),
    ("TikTok", re.compile(r"^tiktok|^bytedance", re.I)),
    ("Google", re.compile(r"^google|^alphabet", re.I)),
    ("Microsoft", re.compile(r"^microsoft", re.I)),
)
for canon, rx in _CANON:
    for (name,) in c.execute(
            "SELECT DISTINCT company FROM jobs WHERE company IS NOT NULL").fetchall():
        if name != canon and rx.match(name or ""):
            n5 += c.execute("UPDATE jobs SET company=? WHERE company=?",
                            (canon, name)).rowcount

# Refresh company_key from the CURRENT name + alias map. Enrichment stamps the
# key with COALESCE (never overwrites), so heal-backfilled names never got a
# key and alias-map changes (AWS -> amazon, 2026-08-08) left stale keys behind.
from applypilot.database import company_key as _company_key
for (name,) in c.execute(
        "SELECT DISTINCT company FROM jobs WHERE company IS NOT NULL").fetchall():
    c.execute("UPDATE jobs SET company_key=? WHERE company=?",
              (_company_key(name), name))

# Dedupe reposts: same normalized title + same description = same job under
# a different URL. Keep the newest, demote the rest out of the queue. Applied
# rows are included in the grouping (read-only) so a re-discovered copy of a
# job ALREADY APPLIED TO gets demoted instead of re-applied (portals were
# bouncing these with "already applied"/90-day-wait errors).
applied_rows = c.execute(
    "SELECT url, title, full_description, discovered_at, apply_status FROM jobs "
    "WHERE apply_status = 'applied'"
).fetchall()
rows = c.execute(
    "SELECT url, title, full_description, discovered_at FROM jobs "
    "WHERE fit_score >= 6 AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')"
).fetchall()
def _dupe_key(r):
    return (
        re.sub(r"\W+", "", (r["title"] or "").lower()),
        hashlib.md5(re.sub(r"\s+", " ", (r["full_description"] or "")[:2000]).encode()).hexdigest(),
    )


applied_keys = {_dupe_key(r) for r in applied_rows}

groups = {}
for r in rows:
    groups.setdefault(_dupe_key(r), []).append(r)

n3 = n_adup = 0
for key, g in groups.items():
    if key in applied_keys:
        # Copy of a job already applied to — demote every unapplied twin.
        for r in g:
            c.execute(
                "UPDATE jobs SET fit_score=3, "
                "score_reasoning=COALESCE(score_reasoning,'') || ' [duplicate of applied posting]' "
                "WHERE url=?", (r["url"],))
            n_adup += 1
        continue
    if len(g) < 2:
        continue
    g.sort(key=lambda r: r["discovered_at"] or "", reverse=True)
    for r in g[1:]:
        c.execute(
            "UPDATE jobs SET fit_score=5, "
            "score_reasoning=COALESCE(score_reasoning,'') || ' [duplicate posting]' "
            "WHERE url=?", (r["url"],))
        n3 += 1
c.commit()
# Cross-board duplicates (user 2026-07-27). The title+description-hash key above
# misses the SAME job listed on Indeed AND LinkedIn, because each board
# reformats the description — so we applied to Amazon's Ads Science role twice
# in 19 minutes, 8 redundant applications in total. Employer + title is the
# reliable signal the boards can't scramble. Only when the employer is actually
# known: aggregator rows (company NULL) can share a generic title like
# "Business Analyst" across genuinely different companies.
n_xdup = c.execute(
    "UPDATE jobs SET apply_status='discarded', "
    "apply_error='duplicate: same title+employer already applied', "
    "score_reasoning=COALESCE(score_reasoning,'') || ' [cross-board duplicate of applied job]' "
    "WHERE applied_at IS NULL "
    "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress','discarded') "
    "AND COALESCE(company,'') != '' "
    "AND EXISTS (SELECT 1 FROM jobs a "
    "            WHERE a.applied_at IS NOT NULL "
    "              AND COALESCE(a.company,'') != '' "
    # company_key catches umbrella-brand variants the raw compare misses
    # ("Amazon.com" vs "Amazon Web Services (AWS)", 2026-08-08).
    "              AND (LOWER(TRIM(a.company)) = LOWER(TRIM(jobs.company)) "
    "                   OR (COALESCE(a.company_key,'') != '' "
    "                       AND a.company_key = jobs.company_key)) "
    "              AND LOWER(TRIM(a.title))   = LOWER(TRIM(jobs.title)) "
    "              AND a.url != jobs.url)"
).rowcount
conn_commit = c.commit()

print(f"reset {n1} error-scores, unstuck {n2} jobs, demoted {n3} duplicate reposts "
      f"+ {n_adup} duplicates of applied jobs, discarded {n_xdup} cross-board duplicates, "
      f"backfilled {n5} employer names, retired {n4} stale")
