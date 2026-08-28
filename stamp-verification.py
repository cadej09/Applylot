"""Stamp Gmail-audit results into the DB (audit run 2026-07-08 via Cowork).

Usage (from job-ops, venv active):
    python stamp-verification.py

Sets per job:
  verification_confidence  'email_confirmed' | 'no_email'
  response_status          'rejected' where a rejection arrived
  apply_status -> 'manual' for the two broken cases (TikTok delivery
                  failure, Veeam incomplete) so they land in manual-apply.csv
"""
import sqlite3
from pathlib import Path

DB = Path.home() / ".applypilot" / "applypilot.db"
c = sqlite3.connect(DB, timeout=30)

for col in ("verification_confidence TEXT", "response_status TEXT"):
    try:
        c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass

# ── Confirmation email found in inbox ────────────────────────────────
CONFIRMED = [
    "Workforce Insights Strategist%",          # Amazon 7/7 13:22
    "Data Engineer, Amazon Ads%",              # Amazon 7/7 13:28 (later rejected)
    "Business Intelligence Engineer, FIO%",    # Amazon 7/7 13:56 (later rejected)
    "ECH Application Specialist%",             # Motorola Workday 7/7 19:36
    "Business Systems Analyst ERP%",           # Dayforce 7/8 09:04
    "Finance Associate",                       # CBRE 7/8 09:12
    "Business Intel Engineer, SCOT-AIM%",      # Amazon 7/8 09:22
    "Financial Business Partner III%",         # Blue Origin Workday 7/8 10:02
    "Transportation Analyst, STREAM BI%",      # Amazon 7/8 10:06
]
n_conf = 0
for pat in CONFIRMED:
    n_conf += c.execute(
        "UPDATE jobs SET verification_confidence='email_confirmed' "
        "WHERE apply_status='applied' AND title LIKE ?", (pat,)).rowcount

# ShopLTK manual add (LTK Greenhouse confirmation 7/8 21:44)
n_conf += c.execute(
    "UPDATE jobs SET verification_confidence='email_confirmed' "
    "WHERE apply_status='applied' AND site LIKE '%ShopLTK%'").rowcount

# ── Rejections received ──────────────────────────────────────────────
REJECTED = [
    "Data Engineer, Amazon Ads%",              # Amazon 7/9 01:28
    "Business Intelligence Engineer, FIO%",    # Amazon 7/9 01:56
    "%Data Science, Earnix%",                  # Liberty Mutual 7/7
    "Data Scientist 1%",                       # Waystar: position filled 7/7
    "%Business Intelligence Analyst%",         # Anaconda (Rippling) 7/8
]
n_rej = 0
for pat in REJECTED:
    n_rej += c.execute(
        "UPDATE jobs SET response_status='rejected' WHERE title LIKE ?",
        (pat,)).rowcount

# ── Broken: send to manual queue ─────────────────────────────────────
# TikTok x2: LinkedIn confirmed 'sent' then 'Problem with your job
# application delivery' hours later — the employer never got them.
n_fix = c.execute(
    "UPDATE jobs SET apply_status='manual', verification_confidence='no_email', "
    "apply_error='LinkedIn delivery FAILED (7/7 22:00 bounce email) — re-apply on company site' "
    "WHERE apply_status='applied' AND (title LIKE 'TikTok Shop%' "
    "OR title LIKE 'Data Engineer, E-Commerce%')").rowcount

# Veeam: only Greenhouse security-code emails, never a confirmation —
# the email-verification step was never completed.
n_fix += c.execute(
    "UPDATE jobs SET apply_status='manual', verification_confidence='no_email', "
    "apply_error='Greenhouse security code never entered — application NOT submitted; redo by hand' "
    "WHERE site LIKE '%Veeam%'").rowcount

# ── Everything else applied but silent inbox ─────────────────────────
n_uv = c.execute(
    "UPDATE jobs SET verification_confidence='no_email' "
    "WHERE apply_status='applied' AND verification_confidence IS NULL "
    "AND applied_at >= '2026-07-06'").rowcount

c.commit()
print(f"confirmed {n_conf} | rejected {n_rej} | moved to manual {n_fix} | unverified {n_uv}")

print("\n-- current verification picture --")
for r in c.execute(
    "SELECT COALESCE(verification_confidence,'-'), COALESCE(response_status,'-'), "
    "apply_status, title FROM jobs WHERE applied_at >= '2026-07-06' "
    "OR response_status IS NOT NULL ORDER BY apply_status, title"):
    print(f"{r[0]:<16} {r[1]:<9} {r[2] or '-':<8} {r[3][:55]}")
c.close()
