"""Prove an internship survives EVERY stage, from discovery filter to apply queue.

Context: a Microsoft Data Science internship posted 2026-09-01 was never
scraped. Internships turned out to be blocked in three separate places, and the
2026-08-28 fix only cleared two of them. This walks a realistic internship title
through each gate in order so a future regression is caught at the stage that
reintroduces it, rather than by noticing a missing job weeks later.
"""
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "applypilot" / "applypilot-setup"))
sys.path.insert(0, str(Path(__file__).parent / "applypilot" / "src"))

DATA_DESC = ("Build dashboards and analytics for the team. You will use SQL "
             "and Python to develop data pipelines and support machine "
             "learning experimentation and statistical analysis.")
PHARM_DESC = ("Assist licensed pharmacists dispensing prescriptions, counsel "
              "patients on medication, manage inventory and immunizations in a "
              "retail pharmacy setting.")

# (title, description, should_pass_prefilter)
CASES = [
    ("Data Science Intern", DATA_DESC, True),
    ("Software Engineering Intern - Data Platform", DATA_DESC, True),
    ("Applied Research Intern (NLP/ML/GenAI)", DATA_DESC, True),
    ("Data Analyst Co-op", DATA_DESC, True),
    ("Machine Learning Engineer Intern", DATA_DESC, True),
    # Must still be rejected — allowing internships must not let these in.
    ("Pharmacy Intern", PHARM_DESC, False),
    ("Volunteer Data Intern", DATA_DESC, False),
    ("AI Trainer (Remote)", DATA_DESC, False),
    ("Senior Data Scientist Intern", DATA_DESC, False),  # seniority still wins
]

failures = []

# ── Stage 1: discovery-time exclude_titles (jobspy.py / boards.py) ──────
cfg = yaml.safe_load((Path.home() / ".applypilot" / "searches.yaml").read_text(encoding="utf-8"))
excl = [x.lower() for x in cfg.get("exclude_titles", [])]
print("STAGE 1 — discovery exclude_titles")
for title, _desc, want in CASES:
    blocked = any(x in title.lower() for x in excl)
    # Only the seniority case should be blocked this early.
    expect_blocked = "senior" in title.lower()
    ok = blocked == expect_blocked
    if not ok:
        failures.append(f"discovery: {title!r} blocked={blocked} expected={expect_blocked}")
    print(f"  {'ok ' if ok else 'FAIL'} {title[:46]:46s} blocked={blocked}")

# ── Stage 2: prefilter role/intern/seniority gates ─────────────────────
import prefilter  # noqa: E402

print(f"\nSTAGE 2 — prefilter (INCLUDE_INTERNSHIPS default={os.environ.get('INCLUDE_INTERNSHIPS', '<unset>')})")
for title, _desc, want in CASES:
    reasons = prefilter.evaluate(title, "Seattle, WA", _desc)
    passed = not reasons
    ok = passed == want
    if not ok:
        failures.append(f"prefilter: {title!r} passed={passed} expected={want} ({reasons})")
    print(f"  {'ok ' if ok else 'FAIL'} {title[:46]:46s} -> {reasons or 'PASSES'}")

# ── Stage 3: nothing downstream re-parks internships ───────────────────
print("\nSTAGE 3 — downstream special-casing")
fixgates = (Path(__file__).parent / "fix-gates.py").read_text(encoding="utf-8")
parks = "SET apply_status='deferred'" in " ".join(fixgates.split())
print(f"  {'FAIL' if parks else 'ok '} fix-gates re-parks internships as deferred: {parks}")
if parks:
    failures.append("fix-gates still parks internships as deferred")

print("\n" + ("ALL STAGES PASS — internships flow like any other job"
              if not failures else f"{len(failures)} FAILURE(S):"))
for f in failures:
    print("  - " + f)
sys.exit(1 if failures else 0)
