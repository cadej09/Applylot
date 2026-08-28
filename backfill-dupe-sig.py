"""Populate jobs.dupe_sig and report what it catches.

Run after pulling the dupe_sig column (database.ensure_columns adds it).
Safe to re-run: recomputes every row from the current description.
"""
import os
import sqlite3

from applypilot.database import (company_key, ensure_columns, get_connection,
                                 job_dupe_sig)

conn = get_connection()
added = ensure_columns(conn)
if added:
    print("schema: added", added)

rows = conn.execute("SELECT url, title, company, full_description FROM jobs").fetchall()
n = nk = 0
for r in rows:
    sig = job_dupe_sig(r["title"], r["full_description"])
    ckey = company_key(r["company"])
    conn.execute("UPDATE jobs SET dupe_sig=?, company_key=? WHERE url=?",
                 (sig, ckey, r["url"]))
    n += bool(sig)
    nk += bool(ckey)
conn.commit()
print(f"rows: {len(rows)}, with usable signature: {n}, with company_key: {nk}")

# What would the guard block right now?
blocked = conn.execute(
    "SELECT COUNT(*) FROM jobs j WHERE j.applied_at IS NULL AND j.dupe_sig IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM jobs a WHERE a.applied_at IS NOT NULL "
    "            AND a.dupe_sig = j.dupe_sig AND a.url != j.url)"
).fetchone()[0]
print(f"pending jobs that are re-lists of something already applied: {blocked}")

# Sanity: how many DISTINCT applied jobs share a signature (i.e. past duplicates)?
dupes = conn.execute(
    "SELECT dupe_sig, COUNT(*) n, GROUP_CONCAT(title, ' || ') titles FROM jobs "
    "WHERE applied_at IS NOT NULL AND dupe_sig IS NOT NULL "
    "GROUP BY dupe_sig HAVING COUNT(*) > 1 ORDER BY n DESC"
).fetchall()
print(f"\npast duplicate applications the signature identifies: "
      f"{sum(r['n'] - 1 for r in dupes)} redundant across {len(dupes)} groups")
for r in dupes[:10]:
    first = r["titles"].split(" || ")[0]
    print(f"   {r['n']}x  {first[:58]}")
