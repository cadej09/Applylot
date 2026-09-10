#!/usr/bin/env python3
"""Hard pre-filter for ApplyPilot — runs BETWEEN discover and score. ZERO API.

Cade's non-negotiables, enforced deterministically so the LLM never wastes a
credit on a job that can't possibly fit:

  ROLE      title is a data / analytics / analyst family role
  SENIORITY entry / new-grad / associate / ~2 yrs  (drops senior, lead, staff,
            principal, manager, director, VP, intern, etc.)
  LOCATION  Washington State (onsite/hybrid) OR Remote-in-USA. Drops foreign and
            other-US-state onsite roles.

Non-matches are stamped fit_score = 0 with a PREFILTERED reason. Because the
scorer only touches jobs WHERE fit_score IS NULL and tailor only touches
fit_score >= 7, those jobs are skipped for free — but stay visible in the
tracker so you can see what was dropped and why.

Usage:
    python3 prefilter.py --dry-run     # preview counts, write nothing
    python3 prefilter.py               # apply the filter
    python3 prefilter.py --reset       # undo (clear PREFILTERED marks)
    python3 prefilter.py --db /path/to/applypilot.db
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")) / "applypilot.db"

# ── ROLE: title must contain one of these (case-insensitive substrings) ──────
ROLE_INCLUDE = (
    "analyst", "analytics", "data scien", "data engineer", "data engineering",
    "business intelligence", "machine learning", "statistician", "data science",
    "bi developer", "bi engineer", "decision scien",
    # AI/ML vocabulary — user 2026-08-28, admitted to a UPenn MSE in AI starting
    # Dec 2026, so AI/ML roles moved on-lane. Without these, real matches like
    # "AI Software Engineer Graduate Intern" (Intel) and "Applied Research
    # Intern (NLP/ML/GenAI)" (Thomson Reuters) were stamped role-mismatch by the
    # prefilter and never reached the scorer at all.
    # Matched as SUBSTRINGS (`k in title`), never as regex — a bare "ai " would
    # also match Dubai/Chennai/Mumbai, hence the padded/punctuated forms.
    "artificial intelligence", " ai ", "ai/", "/ai", "ai engineer",
    " ml ", "ml engineer", "ml/", "applied scien", "research scien",
    "deep learning", "mlops", "nlp", "natural language", "computer vision",
    "llm", "generative ai", "data quality", "data governance", "database",
    "reporting", "quantitative",
)

# ── SENIORITY / non-entry: drop if title matches any (word-aware) ────────────
SENIORITY_EXCLUDE = (
    r"\bsenior\b", r"\bsr\.?\b", r"\bstaff\b", r"\blead\b", r"\bleader\b",
    r"\bprincipal\b", r"\bmanager\b", r"\bmgr\b", r"\bdirector\b", r"\bvp\b",
    r"vice president", r"head of", r"\bchief\b", r"distinguished", r"\bfellow\b",
    r"architect", r"\biii\b", r"\biv\b", r"\bv\b",
    # "executive assistant" is an admin role, not an executive one — the old
    # bare r"executive" false-killed titles like "Business Analyst/Executive
    # Assistant" (2026-07-18 audit).
    r"executive(?!\s+assistant)", r"president",
    r"\bpartner\b", r"\bco-?founder\b", r"\bfounder\b", r"\bfounding\b",
    r"\bcpto\b", r"\bcto\b", r"\bceo\b", r"\bcoo\b", r"\bcfo\b",
)
# Always junk regardless of the internship setting: unpaid/volunteer work,
# "AI trainer" gig listings (a different job entirely), and foreign-language
# internship words that only ever appear on non-US postings.
INTERN_ALWAYS_EXCLUDE = (
    r"stagiaire", r"stajyer", r"praktik", r"werkstudent",
    r"\btrainer\b", r"volunteer",
)
# Real US internship/entry-pipeline titles.
#
# DEFAULT FLIPPED TO INCLUDED, 2026-09-10 (user: "internship scraping can be
# done simultaneously and from same sources as the regular job searching
# pipeline. Since I am most likely eligible for internships now with my MSE AI
# admission acceptance"). Internships are ordinary jobs now — the same sources,
# the same gates, the same apply path — so nothing should have to opt in.
#
# The old default of "0" meant any caller that forgot INCLUDE_INTERNSHIPS=1
# silently reinstated the block; only run-cycle.ps1 set it. Set
# INCLUDE_INTERNSHIPS=0 explicitly to restore the old exclusion.
#
# Kept as a separate tuple from INTERN_ALWAYS_EXCLUDE so that including
# internships can never also let volunteer/AI-trainer gig work back in.
INTERN_ROLE_TITLES = (
    r"\bintern\b", r"internship", r"co-?op\b", r"apprentice", r"\btrainee\b",
)
INCLUDE_INTERNSHIPS = os.environ.get("INCLUDE_INTERNSHIPS", "1") == "1"
INTERN_EXCLUDE = (
    INTERN_ALWAYS_EXCLUDE if INCLUDE_INTERNSHIPS
    else INTERN_ALWAYS_EXCLUDE + INTERN_ROLE_TITLES
)

# ── LOCATION ─────────────────────────────────────────────────────────────────
REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed", "virtual")
FOREIGN_HINTS = (
    "india", "canada", "united kingdom", " uk", "london", "philippines", "brazil",
    "türkiye", "turkey", "singapore", "australia", "germany", "france", "poland",
    "mexico", "ireland", "malaysia", "south africa", "guyana", "denmark",
    "netherlands", "bulgaria", "romania", "spain", "portugal", "italy", "japan",
    "china", "hong kong", "korea", "indonesia", "vietnam", "pakistan", "nigeria",
    "egypt", "peru", "colombia", "chile", "argentina", "malta", "cyprus", "guatemala",
)
WA_HINTS = (
    "seattle", "bellevue", "redmond", "kirkland", "bothell", "renton", "tacoma",
    "everett", "Seattle", "puget", "olympia", "spokane", "vancouver, wa",
)
# Tier-2 relocation metros (2026-07-18): pass the prefilter so they get SCORED;
# fix-gates enforces the 8+ apply bar for them post-score.
METRO_HINTS = (
    "san francisco", "san jose", "oakland", "palo alto", "mountain view",
    "sunnyvale", "santa clara", "san diego", "los angeles", "santa monica",
    "irvine", "new york", "nyc", "manhattan", "brooklyn", "jersey city",
    "chicago", "boston", "cambridge, ma", "somerville",
)
DC_MARKERS = ("washington, dc", "washington dc", "washington, d.c", "d.c.", "district of columbia")
_WA_TOKEN = re.compile(r"\bwa\b|\bwashington\b", re.I)


# Description signals, consulted only to RESCUE a title ROLE_INCLUDE missed.
# User 2026-08-28: "not too much like title based but also job description and
# looking at like matches that way."
#
# Split into CORE (what the job IS) and TOOL (what it uses) on purpose. A first
# cut accepted any two signals of either kind and passed 42% of all rows —
# nearly every software posting names SQL and Python, so tooling alone proves
# nothing. A role is rescued only on real role evidence.
_CORE_SIGNALS = (
    r"\bdata analy", r"\banalytics\b", r"\bbusiness intelligence\b",
    r"\bdata scien", r"\bdata engineer", r"\bdata pipeline",
    r"\bdata warehouse", r"\bdata model", r"\bdata quality\b",
    r"\bdata governance\b", r"\bmachine learning\b", r"\bdeep learning\b",
    r"\bartificial intelligence\b", r"\bnatural language processing\b",
    r"\bcomputer vision\b", r"\bstatistical analysis\b", r"\bdashboards?\b",
    r"\bA/B test", r"\bexperimentation\b", r"\bforecasting\b",
    r"\bpredictive model", r"\bapplied scien", r"\bbusiness intelligence\b",
)
_TOOL_SIGNALS = (
    r"\bsql\b", r"\bpython\b", r"\btableau\b", r"\bpower ?bi\b", r"\blooker\b",
    r"\bdbt\b", r"\bsnowflake\b", r"\bredshift\b", r"\bbigquery\b", r"\betl\b",
    r"\bpytorch\b", r"\btensorflow\b", r"\bscikit\b", r"\bpandas\b",
    r"\bspark\b", r"\bairflow\b", r"\bregression\b",
)
_CORE_RX = [re.compile(p, re.I) for p in _CORE_SIGNALS]
_TOOL_RX = [re.compile(p, re.I) for p in _TOOL_SIGNALS]


def description_matches_role(description: str | None) -> bool:
    """True when a description describes data/AI WORK, not merely data/AI tools.

    Two distinct CORE phrases, or one CORE phrase backed by a tool from the
    stack. Note this can only help where a description actually exists —
    prune-db stubs descriptions for scored-low rows, so on historical rows the
    field is usually empty and the title remains the only evidence.
    """
    if not description:
        return False
    core = {rx.pattern for rx in _CORE_RX if rx.search(description)}
    if len(core) >= 2:
        return True
    return bool(core) and any(rx.search(description) for rx in _TOOL_RX)


def title_reasons(title: str, description: str | None = None) -> list[str]:
    """Return list of failure reasons for a title (empty = passes).

    `description` is consulted only to rescue a title that missed ROLE_INCLUDE.
    It can never ADD a reason, so the seniority and gig gates are unaffected.
    """
    t = (title or "").lower()
    reasons = []
    if not any(k in t for k in ROLE_INCLUDE):
        if not description_matches_role(description):
            reasons.append("role-mismatch")
    if any(re.search(p, t) for p in SENIORITY_EXCLUDE):
        reasons.append("too-senior")
    if any(re.search(p, t) for p in INTERN_EXCLUDE):
        reasons.append("intern/gig")
    return reasons


def location_reason(loc: str | None) -> str | None:
    """Return a failure reason for a location, or None if it passes."""
    if not loc or not loc.strip():
        return None  # unknown -> keep, let scorer/you decide
    l = loc.lower()
    if any(f in l for f in FOREIGN_HINTS):
        return "foreign"
    if any(r in l for r in REMOTE_HINTS):
        return None  # US remote (foreign already excluded)
    is_dc = any(m in l for m in DC_MARKERS)
    if _WA_TOKEN.search(l) and not is_dc:
        return None  # WA onsite/hybrid
    if any(w in l for w in WA_HINTS):
        return None
    if any(m in l for m in METRO_HINTS):
        return None  # tier-2 metro: keep for scoring; 8+ bar enforced post-score
    return "not-WA/metro/remote"  # other US state onsite, or ambiguous non-remote US


def evaluate(title: str, loc: str | None, description: str | None = None) -> list[str]:
    reasons = title_reasons(title, description)
    lr = location_reason(loc)
    if lr:
        reasons.append(lr)
    return reasons


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}\nRun discovery first: applypilot run discover -w 4")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    if args.reset:
        n = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE score_reasoning LIKE 'PREFILTERED%'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE jobs SET fit_score = NULL, score_reasoning = NULL, scored_at = NULL "
            "WHERE score_reasoning LIKE 'PREFILTERED%'"
        )
        conn.commit()
        print(f"Reset {n} prefiltered jobs back to unscored.")
        return

    # Only consider jobs not yet scored by the LLM (fit_score IS NULL) OR already
    # prefiltered (so re-running updates cleanly). Never overwrite real LLM scores.
    rows = conn.execute(
        "SELECT url, title, location, full_description FROM jobs "
        "WHERE fit_score IS NULL OR score_reasoning LIKE 'PREFILTERED%'"
    ).fetchall()

    kept, dropped = [], []
    reason_counts: Counter = Counter()
    for r in rows:
        reasons = evaluate(r["title"] or "", r["location"], r["full_description"])
        if reasons:
            dropped.append((r["url"], reasons))
            for x in reasons:
                reason_counts[x] += 1
        else:
            kept.append(r)

    total = len(rows)
    print("\n" + "=" * 64)
    print(f"  PRE-FILTER  ({'DRY RUN — no writes' if args.dry_run else 'applying'})")
    print(f"  candidates considered: {total}")
    print("=" * 64)
    print(f"  KEEP  (go to scorer): {len(kept)}")
    print(f"  DROP  (skip, free):   {len(dropped)}")
    print("\n  drop reasons (a job can have several):")
    for reason, cnt in reason_counts.most_common():
        print(f"    {reason:16} {cnt}")
    print("\n  sample kept roles:")
    for r in kept[:15]:
        print(f"    + {(r['title'] or '')[:46]:46} | {(r['location'] or '')[:22]}")

    if args.dry_run:
        print("\n(dry run) re-run without --dry-run to apply.\n")
        return

    now = datetime.now(timezone.utc).isoformat()
    for url, reasons in dropped:
        conn.execute(
            "UPDATE jobs SET fit_score = 0, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (f"PREFILTERED: {', '.join(reasons)}", now, url),
        )
    conn.commit()
    print(f"\nApplied. {len(dropped)} jobs marked (fit_score=0) and will be skipped by the")
    print(f"scorer/tailor. {len(kept)} jobs remain for LLM scoring.\n")
    print("Next (spends API, only on the survivors):")
    print("  caffeinate -i applypilot run score tailor cover pdf -w 4\n")


if __name__ == "__main__":
    main()
