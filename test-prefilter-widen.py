"""Dry-test the widened prefilter role gate against real DB rows.

Checks BOTH directions:
  RESCUED  - previously stamped role-mismatch, now passes (the intended win)
  STILL OUT- previously rejected, still rejected (no over-permissiveness)
Writes nothing.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "applypilot" / "applypilot-setup"))
import prefilter  # noqa: E402

con = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db", timeout=180)
con.row_factory = sqlite3.Row

rows = con.execute(
    """SELECT title, location, full_description d, COALESCE(company,site) emp,
              score_reasoning r
       FROM jobs
       WHERE score_reasoning LIKE '%role-mismatch%'
         AND discovered_at >= datetime('now','-45 days')"""
).fetchall()

rescued, still_out = [], []
for r in rows:
    reasons = prefilter.evaluate(r["title"] or "", r["location"], r["d"])
    (still_out if "role-mismatch" in reasons else rescued).append((r, reasons))

print(f"previously role-mismatched in last 45d: {len(rows)}")
print(f"  RESCUED by the widened gate: {len(rescued)}")
print(f"  still rejected:              {len(still_out)}")

def why(reasons):
    return ", ".join(reasons) if reasons else "PASSES CLEAN"

print("\n--- sample of RESCUED (these now reach the scorer) ---")
for r, reasons in rescued[:18]:
    print(f"  {(r['title'] or '')[:52]:52s} | {(r['emp'] or '?')[:18]:18s} | {why(reasons)}")

print("\n--- sample of STILL REJECTED (sanity: should look genuinely off-lane) ---")
for r, reasons in still_out[:12]:
    print(f"  {(r['title'] or '')[:52]:52s} | {(r['emp'] or '?')[:18]:18s}")

# Internship-specific view: the population the user just became eligible for.
print("\n--- internships among the rescued ---")
n = 0
for r, reasons in rescued:
    t = (r["title"] or "").lower()
    if "intern" in t and "internal" not in t:
        n += 1
        if n <= 12:
            print(f"  {(r['title'] or '')[:52]:52s} | {(r['emp'] or '?')[:18]:18s} | {why(reasons)}")
print(f"  total internships rescued: {n}")
