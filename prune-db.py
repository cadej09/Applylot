"""Shrink applypilot.db before it hits GitHub's 100MB hard limit.

Replaces full_description with a stub for jobs that are definitively out:
scored 1-4 (well below the 7+ apply threshold) and never applied. Titles,
URLs, scores, and reasoning are kept so dedupe and history still work.
Uses a stub (not NULL) so the enrich stage doesn't re-scrape them.
Ends with VACUUM to actually reclaim the space.

Usage (job-ops, venv active, NO cycle running):  python prune-db.py
"""
import os
import sqlite3

DB = os.path.expanduser("~/.applypilot/applypilot.db")
before = os.path.getsize(DB) / 1e6

c = sqlite3.connect(DB, timeout=60)
# Internships are exempt (2026-07-30). They score structurally low — location
# caps, "current student" requirements — so a score-based prune wipes exactly
# the ones kept for human review. That already happened once: 77 of 85
# internships were re-scored against an 18-char "[pruned]" stub, and the
# original postings could not be re-scraped afterwards (76 unrecoverable).
NOT_INTERNSHIP = (
    "AND title NOT LIKE '%ntern%' AND title NOT LIKE '%o-op%' "
    "AND title NOT LIKE '%pprentice%' AND title NOT LIKE '%rainee%' "
)
n = c.execute(
    "UPDATE jobs SET full_description='[pruned: low score]' "
    "WHERE fit_score BETWEEN 1 AND 4 "
    "AND apply_status IS NULL "
    + NOT_INTERNSHIP +
    "AND LENGTH(COALESCE(full_description,'')) > 100"
).rowcount
# The legacy `description` column (raw board snippet, uncapped on the jobspy
# path) was 73 MB / 80% of the file on 2026-07-24. It is only read at discovery
# time; scoring/tailoring/dedup use full_description. Clear it for non-applied
# rows every prune so it can never dominate the file again.
n2 = c.execute(
    "UPDATE jobs SET description='' "
    "WHERE applied_at IS NULL AND LENGTH(COALESCE(description,'')) > 0"
).rowcount
# Same exemption for the score-0 sweep: prefiltered internships must keep their
# descriptions so the review list can be rebuilt without a re-scrape.
n3 = c.execute(
    "UPDATE jobs SET full_description='[pruned: rejected]' "
    "WHERE fit_score = 0 AND apply_status IS NULL AND applied_at IS NULL "
    + NOT_INTERNSHIP +
    "AND LENGTH(COALESCE(full_description,'')) > 100"
).rowcount
print(f"pruned {n3} score-0 descriptions (internships exempt)")
print(f"cleared {n2} legacy description snippets")
c.commit()
c.execute("VACUUM")
c.close()

after = os.path.getsize(DB) / 1e6
print(f"pruned {n} descriptions: {before:.1f} MB -> {after:.1f} MB")
