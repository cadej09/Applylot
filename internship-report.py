"""Write the internship review list (user decision 2026-07-30; ranking policy
2026-08-06).

Internships are discovered and scored like everything else but never
auto-applied: the user decides per posting (including whether to bother with
enrollment-gated ones). Ranking = JD relevance + company quality/WLB from the
Glassdoor/Reddit research in ~/.applypilot/company_quality.json — NOT
fit_score, which runs structurally low for internships.

Output: ~/.applypilot/internship_review.md
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = os.path.expanduser("~/.applypilot/applypilot.db")
OUT = Path.home() / ".applypilot" / "internship_review.md"
QUALITY = Path.home() / ".applypilot" / "company_quality.json"

# Phrases that usually mean "must currently be enrolled".
ENROLL_RX = re.compile(
    r"(currently enrolled|must be enrolled|actively enrolled|pursuing a "
    r"(bachelor|master|degree)|current student|rising (junior|senior|sophomore)|"
    r"returning to school|graduating in 20\d\d|enrolled in an accredited)",
    re.IGNORECASE)

# Data/analytics relevance, NOT fit_score, decides what lands here.
# Internships score structurally low for this candidate — the scorer sees
# "must be a current student", which he is not until admission — so a 6+ bar
# left the list empty while burying real matches (a Bellevue "Data Science
# Intern" scored 1). Relevance ranks them; the score is shown for context.
ROLE_RX = re.compile(
    r"\b(data|analyt|analyst|scientist|science|bi\b|business intelligence|"
    r"machine learning|\bml\b|statistic|quantitative|insight|reporting)\b",
    re.IGNORECASE)
# The SQL below uses LIKE '%ntern%' as a cheap pre-filter, which also matches
# "Internal" and "International" — word boundaries are enforced here.
INTERN_RX = re.compile(
    r"\b(intern|internship|co-?op|apprentice|trainee)\b", re.IGNORECASE)
# Out-of-scope locations: he is WA-based, open to the listed relocation metros.
# Checked against the URL too, because employer ATS rows often leave `location`
# empty while the country sits in the job URL (Thomson Reuters, Autodesk).
FOREIGN_RX = re.compile(
    r"\b(india|canada|taiwan|costa.?rica|quebec|mumbai|delhi|singapore|"
    r"united.?kingdom|london|england|germany|japan|china|korea|philippines|"
    r"brazil|mexico|ireland|france|spain|poland|netherlands|australia)\b",
    re.IGNORECASE)
# Graduate-degree-only research internships are out of reach today.
PHD_RX = re.compile(r"\b(ph\.?d|doctoral|postdoc)\b", re.IGNORECASE)

c = sqlite3.connect(DB, timeout=30)
c.row_factory = sqlite3.Row
raw = c.execute(
    "SELECT url, title, COALESCE(company, site) emp, fit_score, location, "
    "full_description, discovered_at, application_url "
    "FROM jobs WHERE applied_at IS NULL "
    "AND COALESCE(apply_status,'') NOT IN ('discarded','applied') "
    "AND (title LIKE '%ntern%' OR title LIKE '%o-op%' OR title LIKE '%pprentice%' "
    "     OR title LIKE '%rainee%') "
    "ORDER BY fit_score DESC, discovered_at DESC"
).fetchall()
rows = []
for r in raw:
    title = r["title"] or ""
    # Employer name counts too: "MedTourEasy Delhi" carries the country when
    # both the location column and the URL are uninformative.
    where = (f"{r['location'] or ''} {title} {r['emp'] or ''} "
             f"{r['url'] or ''} {r['application_url'] or ''}")
    if not INTERN_RX.search(title):      # "Internal"/"International" false hits
        continue
    if not ROLE_RX.search(title):        # must be data/analytics adjacent
        continue
    if FOREIGN_RX.search(where):         # non-US posting
        continue
    if PHD_RX.search(title):             # graduate-degree-only research roles
        continue
    rows.append(r)

# Company quality/WLB map (Glassdoor/Reddit research). tier 1 strong,
# 2 decent, 3 weak WLB, 0 spam-mill. Missing file = neutral everywhere.
try:
    _q = json.loads(QUALITY.read_text(encoding="utf-8"))["companies"]
except (OSError, KeyError, ValueError):
    _q = {}


def quality(emp: str):
    emp_l = (emp or "").lower()
    for name, info in _q.items():
        if name.lower() in emp_l or emp_l in name.lower():
            return info
    return None

# Relevance: how close the TITLE is to his lane (data analyst > BI/DS > ML/DE).
STRONG_RX = re.compile(r"\b(data analy|business intelligence|\bbi\b|analytics|"
                       r"data science|data scientist|insight|reporting)", re.IGNORECASE)
MED_RX = re.compile(r"\b(machine learning|\bml\b|data engineer|statistic|"
                    r"quantitative)\b", re.IGNORECASE)
WA_RX = re.compile(r"\b(seattle|bellevue|redmond|tacoma|Seattle|everett|"
                   r"washington,? (wa|state)|, wa\b|remote)\b", re.IGNORECASE)
TIER_BONUS = {1: 3, 2: 1, 3: -2, 0: -6}


def rank(r) -> float:
    title = r["title"] or ""
    pts = 4 if STRONG_RX.search(title) else (2 if MED_RX.search(title) else 0)
    info = quality(r["emp"])
    if info:
        pts += TIER_BONUS.get(info.get("tier"), 0)
    where = f"{r['location'] or ''} {r['url'] or ''}"
    if WA_RX.search(where):
        pts += 1
    pts += min(r["fit_score"] or 0, 5) / 10          # small tiebreak from score
    return pts


rows.sort(key=rank, reverse=True)

open_now, enroll_gated = [], []
for r in rows:
    (enroll_gated if ENROLL_RX.search(r["full_description"] or "") else open_now).append(r)

lines = [
    "# Internship review list",
    "",
    f"_Generated {datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M}. "
    f"{len(rows)} data/analytics internships, not auto-applied._",
    "",
    "Found and scored by the pipeline but never submitted automatically: most "
    "internships require current enrollment, which is pending Master's "
    "admission. Decide per role, then say which to apply to.",
    "",
    "**On the scores:** they run low by design. The scorer compares you against "
    "the posting, and a posting demanding a *current student* rates a graduate "
    "as a poor fit — so the number reflects enrollment status, not how good the "
    "role is for you. Ranked instead by JD relevance + company quality/WLB "
    "(Glassdoor/Reddit research in company_quality.json); treat the score as "
    "context.",
    "",
]


def section(title, items, note):
    lines.append(f"## {title} ({len(items)})")
    lines.append("")
    lines.append(f"_{note}_")
    lines.append("")
    if not items:
        lines.append("None found this run.")
        lines.append("")
        return
    lines.append("| Score | Role | Employer | Quality/WLB | Location | Link |")
    lines.append("|---|---|---|---|---|---|")
    for r in items:
        t = (r["title"] or "")[:58].replace("|", "/")
        e = str(r["emp"] or "")[:26].replace("|", "/")
        loc = str(r["location"] or "")[:24].replace("|", "/")
        info = quality(r["emp"])
        qcol = (f"T{info['tier']}: {info['note'][:38]}" if info else "?").replace("|", "/")
        lines.append(f"| {r['fit_score']} | {t} | {e} | {qcol} | {loc} | "
                     f"[open]({r['application_url'] or r['url']}) |")
    lines.append("")


section("No explicit enrollment requirement found", open_now,
        "Worth a look now — the description does not obviously demand current "
        "enrollment. Verify on the posting before applying.")
section("Appears to require current enrollment", enroll_gated,
        "Likely blocked until you are enrolled. Good targets for the Summer 2027 "
        "cycle (postings open roughly Aug-Oct) once admission is confirmed.")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {OUT}")
print(f"  {len(open_now)} without an obvious enrollment requirement")
print(f"  {len(enroll_gated)} that appear to require enrollment")
