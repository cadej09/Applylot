"""Retroactive sweep for the location gate + experience-overclaim fixes.

1. Demotes queued 7+ jobs that are onsite/hybrid outside Washington (not remote).
2. Scans generated tailored resumes + cover letters for ">2 years" experience
   claims and resets those jobs' artifacts so they regenerate truthfully.

Safe to run while apply is active.
Usage (from job-ops, venv active):  python fix-gates.py
"""
import re
import sqlite3
from pathlib import Path

DATA = Path.home() / ".applypilot"
c = sqlite3.connect(DATA / "applypilot.db", timeout=30)
c.row_factory = sqlite3.Row

# ── 1. Location demotion ─────────────────────────────────────────────
WA = ("seattle", "bellevue", "redmond", "kirkland", "bothell", "renton",
      "Seattle", "everett", "tacoma", "olympia", "spokane", "puget sound",
      "washington")
# Tier-2 relocation metros (2026-07-18, user request): onsite/hybrid here is
# acceptable ONLY at fit_score >= 8 — relocation has to be worth it. Enforced
# by a second sweep below after the base location gate passes them.
METROS = ("san francisco", "san jose", "oakland", "palo alto", "mountain view",
          "sunnyvale", "santa clara", "san diego", "los angeles", "santa monica",
          "irvine", "new york", "nyc", "manhattan", "brooklyn", "jersey city",
          "chicago", "boston", "cambridge, ma", "somerville")
STATE_RX = re.compile(r",\s*([A-Za-z]{2})(?:\b|$)")
US_RX = re.compile(
    r"united states|u\.s\.|\busa\b|\bus[- ]based\b|within the us\b|\bus only\b|"
    r"remote \(us\)|remote - us\b|us remote|"
    r"authorized to work in the (us|united states)|us work authorization|"
    # Proper "City, ST" shape, matched CASE-SENSITIVELY via a scoped (?-i:)
    # so the two-letter state codes can't collide with English words like
    # "or"/"in"/"me"/"hi"/"ok" the way the old lowercase alternation did.
    r"(?-i:[A-Z][A-Za-z.]+,\s*(?:WA|OR|CA|TX|NY|IL|CO|GA|NC|VA|AZ|MA|PA|FL|OH|MI|MN|UT|TN|MO|MD|NJ|WI|IN|SC|AL|KY|OK|CT|IA|NV|AR|KS|MS|NM|NE|ID|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WV|WY)\b)",
    re.IGNORECASE)
FOREIGN_RX = re.compile(
    r"canada|united kingdom|\buk\b|ireland|india|philippines|pakistan|mexico|"
    r"brazil|argentina|colombia|europe|\bemea\b|\bapac\b|\blatam\b|australia|"
    r"new zealand|germany|france|spain|poland|portugal|romania|netherlands|"
    r"singapore|japan|korea|vietnam|nigeria|egypt|turkey|türkiye|ukraine|"
    r"worldwide|global[- ]remote", re.IGNORECASE)


def location_verdict(loc: str, desc: str) -> str | None:
    """None = fine; otherwise a reason string for demotion."""
    low = (loc or "").lower()
    if not low:
        if US_RX.search((desc or "")[:5000]):
            return None
        return "no location and no US evidence"
    if "remote" in low or "anywhere" in low:
        if FOREIGN_RX.search(low):
            return "remote but foreign region"
        if not US_RX.search(f"{loc} {(desc or '')[:5000]}"):
            return "remote with no verifiable US eligibility"
        return None
    dc = "washington, dc" in low or "washington dc" in low or ", dc" in low or "d.c" in low
    def _tok(t):
        return bool(re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low)) if len(t) <= 3 else t in low
    if not dc and any(_tok(t) for t in WA):
        return None
    if any(m in low for m in METROS):
        return None  # tier-2 metro: allowed; 8+ bar enforced in the sweep below
    return "onsite/hybrid outside WA/metros, not remote"


def is_metro(loc: str) -> bool:
    low = (loc or "").lower()
    return (any(m in low for m in METROS)
            and not any(t in low for t in WA)
            and "remote" not in low)


n_loc = 0
for r in c.execute("SELECT url, title, location, full_description FROM jobs "
                   "WHERE fit_score >= 6 "
                   "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall():
    why = location_verdict(r["location"], r["full_description"])
    if why:
        c.execute("UPDATE jobs SET fit_score=4, score_reasoning=COALESCE(score_reasoning,'') "
                  "|| ' [demoted: ' || ? || ']' WHERE url=?", (why, r["url"]))
        print(f"  location demoted ({why}): {r['title'][:50]} | {(r['location'] or '')[:35]}")
        n_loc += 1

# ── 1a2. Tier-2 metro relocation bar: metros need fit_score >= 8 ─────
n_metro = 0
for r in c.execute("SELECT url, title, location, fit_score FROM jobs "
                   "WHERE fit_score IN (6, 7) "
                   "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall():
    if is_metro(r["location"]):
        c.execute("UPDATE jobs SET fit_score=4, score_reasoning=COALESCE(score_reasoning,'') "
                  "|| ' [demoted: relocation metro below the 8+ bar]' WHERE url=?", (r["url"],))
        print(f"  metro demoted (<8): {r['title'][:50]} | {(r['location'] or '')[:35]}")
        n_metro += 1

# ── 1b. Leadership + domain-mismatch title demotion ──────────────────
from applypilot.scoring.scorer import (
    _DOMAIN_MISMATCH_RX, _SENIOR_TITLE_RX, _TARGET_FAMILY_RX,
    _SWE_RX, _SWE_EXEMPT_RX, _LANE_CORE_RX, _LANE_OFF_RX, is_internship)

# ── 1a2. Internships: score them, but never auto-apply ───────────────────
# User is pursuing a Master's but is not yet admitted, so he is not currently
# enrolled — which most internships require. Park them as 'deferred' (a status
# the tailor and apply queues already skip) so they stay visible and scored for
# human review instead of burning applications on an eligibility screen the bot
# would have to answer honestly and fail. `internship-report.py` lists them.
n_intern = 0
for r in c.execute(
    "SELECT url, title FROM jobs WHERE fit_score >= 6 "
    "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress','deferred')"
).fetchall():
    if is_internship(r["title"] or ""):
        c.execute(
            "UPDATE jobs SET apply_status='deferred', "
            "apply_error='internship — needs enrollment check, review manually' "
            "WHERE url=?", (r["url"],))
        n_intern += 1
if n_intern:
    print(f"  internships parked for review (not auto-applied): {n_intern}")

n_title = 0
for r in c.execute("SELECT url, title FROM jobs WHERE fit_score >= 6 "
                   "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall():
    t = r["title"] or ""
    if _SENIOR_TITLE_RX.search(t):
        # Cap 5: gate rejects must live below the apply threshold.
        why, cap = "senior/leadership title", 5
    elif _DOMAIN_MISMATCH_RX.search(t) and not _TARGET_FAMILY_RX.search(t):
        # data/analyst-family titles: a "clinical"/"pharma" keyword is just the
        # subject area of a data role, not an out-of-field role.
        why, cap = "domain mismatch vs resume", 5
    elif _SWE_RX.search(t) and not _SWE_EXEMPT_RX.search(t):
        # Pure software/infra engineering (not data/ML) — user decision
        # 2026-07-23: not pursuing SWE roles.
        why, cap = "software-engineering role, not data/analytics", 5
    else:
        continue
    c.execute("UPDATE jobs SET fit_score=?, score_reasoning=COALESCE(score_reasoning,'') "
              "|| ' [demoted: ' || ? || ']' WHERE url=?", (cap, why, r["url"]))
    print(f"  title demoted ({why}): {t[:60]}")
    n_title += 1

# ── 1b2. Off-lane role-family demotion (user decision 2026-08-10) ────
# Retro sweep for the scorer's _cap_off_lane. When the apply threshold moved
# 7 -> 6, a funnel audit found 116 of 195 unclaimed 6s were "analyst" roles in
# other fields (Security Operations, Incident Response, Pricing, Underwriting,
# Program, Contract/Billing) that scored 6 on SQL/Excel/dashboard overlap alone.
# Only bites at EXACTLY 6: the 7+ band has its own evidence bar and 41
# applications at 7 produced zero rejections.
n_lane = 0
for r in c.execute("SELECT url, title FROM jobs WHERE fit_score = 6 "
                   "AND COALESCE(apply_status,'') NOT IN "
                   "('applied','in_progress','deferred')").fetchall():
    t = r["title"] or ""
    if _LANE_CORE_RX.search(t) or not _LANE_OFF_RX.search(t):
        continue
    c.execute("UPDATE jobs SET fit_score=5, score_reasoning=COALESCE(score_reasoning,'') "
              "|| ' [demoted: off-lane role family — not data/analytics work]' "
              "WHERE url=?", (r["url"],))
    print(f"  off-lane demoted: {t[:60]}")
    n_lane += 1

# ── 1c. Spam-lister demotion ─────────────────────────────────────────
# Recruiting content mills that mass-repost generic "remote" bait listings.
# Applying to these wastes worker time and never reaches a real employer.
SPAM_LISTERS = (
    "hire feed", "quik hire", "crossing hurdles", "remotehunter",
    "jobright.ai", "helic & co", "codevertex", "why hiring", "haystack",
    "7seventy", "talentpop", "great value hiring", "prime hiring",
    "nexvora", "agilegrid", "dataweav", "conquer ai", "hired",
    "goodspace ai", "redpapr", "meridial", "rilith", "internmo",
    "yo it consulting", "whitepapers launch", "pinnacle method",
)
SPAM_TITLE_RX = re.compile(
    r"^(content evaluator|technical specialist|research specialist|"
    r"content reviewer|data annotator|video annotator|excel analyst|"
    r"university researcher)\b.*remote", re.IGNORECASE)

n_spam = 0
for r in c.execute("SELECT url, title, COALESCE(company, site) AS emp FROM jobs "
                   "WHERE fit_score >= 6 "
                   "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall():
    emp = (r["emp"] or "").lower()
    if any(s in emp for s in SPAM_LISTERS) or SPAM_TITLE_RX.search(r["title"] or ""):
        c.execute("UPDATE jobs SET fit_score=5, score_reasoning=COALESCE(score_reasoning,'') "
                  "|| ' [demoted: spam-lister/bait listing]' WHERE url=?", (r["url"],))
        print(f"  spam demoted: {r['title'][:50]} | {r['emp'][:25]}")
        n_spam += 1

# ── 1d. Experience-requirement demotion ──────────────────────────────
# Candidate is entry-level (~0-2 yrs). Drop jobs whose JD hard-requires more.
# Catches "5+ years", "3-5 years", "5 to 7 years", "minimum of 4 years" — takes
# the LOWER bound of any range and demotes when it exceeds the cap.
EXP_CAP = 2
_EXP_RX = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–]\s*\d{1,2}\s*|to\s+\d{1,2}\s*)?years?"
    r"[^.\n]{0,40}?(?:experience|exp\b|professional|industry|hands-on|relevant|"
    r"in\s+the\s+field|of\s+work|working)",
    re.IGNORECASE,
)


# A years figure inside a clause with any of these words is a SOFT preference,
# not a hard requirement — mirror fix-education.py's "preferred" skip so a JD
# saying only "3-5 years preferred" doesn't demote an entry-level role.
_EXP_SOFT_RX = re.compile(r"preferred|nice to have|a plus|ideally", re.IGNORECASE)


def required_years(desc: str) -> int | None:
    """Lowest explicitly-HARD-required years-of-experience found, or None.

    Clauses containing "preferred"/"nice to have"/"a plus"/"ideally" are
    treated as soft preferences and skipped.
    """
    mins: list[int] = []
    for clause in re.split(r"[.\n;]", desc or ""):
        if _EXP_SOFT_RX.search(clause):
            continue
        mins += [int(m.group(1)) for m in _EXP_RX.finditer(clause)]
    return min(mins) if mins else None


n_exp = 0
for r in c.execute("SELECT url, title, full_description FROM jobs "
                   "WHERE fit_score >= 6 "
                   "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall():
    yrs = required_years(r["full_description"])
    if yrs is not None and yrs > EXP_CAP:
        c.execute("UPDATE jobs SET fit_score=5, score_reasoning=COALESCE(score_reasoning,'') "
                  "|| ' [demoted: JD requires ' || ? || '+ yrs experience, candidate entry-level]' "
                  "WHERE url=?", (yrs, r["url"]))
        print(f"  experience demoted ({yrs}+ yrs): {(r['title'] or '')[:50]}")
        n_exp += 1

# ── 2. Overclaim artifact reset ──────────────────────────────────────
OVER_RX = re.compile(r"\b(\d{1,2})\s*\+?\s*years?\b", re.IGNORECASE)

def overclaims(path_str: str) -> bool:
    if not path_str:
        return False
    p = Path(path_str)
    if not p.is_absolute():
        p = DATA / p
    txt = p.with_suffix(".txt")
    if not txt.exists():
        return False
    body = txt.read_text(encoding="utf-8", errors="ignore").lower()
    return any(int(m) > 2 for m in OVER_RX.findall(body))

n_over = 0
for r in c.execute("SELECT url, title, tailored_resume_path, cover_letter_path FROM jobs "
                   "WHERE fit_score >= 6 AND apply_status IS NULL "
                   "AND tailored_resume_path IS NOT NULL").fetchall():
    if overclaims(r["tailored_resume_path"]) or overclaims(r["cover_letter_path"]):
        # Clear the artifacts so they regenerate, but do NOT reset
        # tailor_attempts/cover_attempts: zeroing the counters removes the
        # regen safety cap and lets a chronically-overclaiming job re-tailor
        # forever. Leave the caps intact so it stops after the normal limit.
        c.execute("UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL, "
                  "cover_letter_path=NULL, cover_letter_at=NULL "
                  "WHERE url=?", (r["url"],))
        print(f"  overclaim reset: {r['title'][:60]}")
        n_over += 1

c.commit()
left = c.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 6 AND apply_status IS NULL").fetchone()[0]
print(f"\ndemoted {n_loc} out-of-region + {n_metro} metro-below-8 "
      f"+ {n_title} leadership/wrong-domain + {n_lane} off-lane role-family "
      f"+ {n_spam} spam + {n_exp} over-experience jobs, "
      f"reset {n_over} overclaiming artifacts; {left} jobs remain in the 6+ queue")
c.close()
