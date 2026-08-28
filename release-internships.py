"""Release internships that earlier crawls prefiltered out as "intern/gig".

Internships were excluded pipeline-wide until 2026-07-30, so every one ever
discovered sits at fit_score=0 with a PREFILTERED mark and is a dupe on
re-crawl — meaning enabling the search alone surfaces almost nothing. This
resets ONLY those rows (never ones killed for role-mismatch, seniority or
location, which are still correct) so the next scoring pass evaluates them.
"""
import os
import re
import sqlite3

RX = re.compile(r"\b(intern|internship|co-?op|apprentice|trainee)\b", re.I)
# Never resurrect: these are junk regardless of the internship setting.
JUNK = re.compile(r"stagiaire|stajyer|praktik|werkstudent|\btrainer\b|volunteer", re.I)

c = sqlite3.connect(os.path.expanduser("~/.applypilot/applypilot.db"), timeout=60)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT url, title, score_reasoning FROM jobs "
    "WHERE fit_score = 0 AND applied_at IS NULL "
    "AND score_reasoning LIKE '%PREFILTERED%' AND score_reasoning LIKE '%intern/gig%'"
).fetchall()
print(f"rows prefiltered as intern/gig: {len(rows)}")

released = skipped_junk = skipped_other = 0
for r in rows:
    t = r["title"] or ""
    if JUNK.search(t):
        skipped_junk += 1
        continue
    if not RX.search(t):
        skipped_other += 1
        continue
    # Only clear the mark when intern/gig was the ONLY reason — a posting also
    # flagged role-mismatch or too-senior stays dead.
    reasons = (r["score_reasoning"] or "").split("PREFILTERED:")[-1]
    others = [x.strip() for x in reasons.split(",") if x.strip() and "intern/gig" not in x]
    if others:
        skipped_other += 1
        continue
    c.execute("UPDATE jobs SET fit_score=NULL, score_reasoning=NULL, scored_at=NULL "
              "WHERE url=?", (r["url"],))
    released += 1
c.commit()

# Clearing fit_score is not enough. These rows were score-0 for months, so the
# prune passes replaced their descriptions with "[pruned: ...]" stubs — and the
# first release scored 77 of 85 internships against an 18-character stub,
# producing numbers that measured nothing. Reset the stubs so the enrich stage
# re-scrapes the real posting before scoring sees them.
restored = c.execute(
    "UPDATE jobs SET full_description=NULL, detail_scraped_at=NULL, "
    "detail_error=NULL, fit_score=NULL, score_reasoning=NULL, scored_at=NULL "
    "WHERE applied_at IS NULL "
    "AND (title LIKE '%ntern%' OR title LIKE '%o-op%' OR title LIKE '%pprentice%' "
    "     OR title LIKE '%rainee%') "
    "AND COALESCE(full_description,'') LIKE '[pruned%'"
).rowcount
c.commit()
print(f"  stub descriptions reset for re-enrichment: {restored}")
print(f"  released for scoring : {released}")
print(f"  left dead (junk)     : {skipped_junk}")
print(f"  left dead (other gate): {skipped_other}")

pend = c.execute(
    "SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL AND full_description IS NOT NULL"
).fetchone()[0]
print(f"pending score now: {pend}")
