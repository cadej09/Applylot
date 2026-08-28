"""Regression test for the role-family (title lane) gate and the target-company
boost floor. Run after touching scorer.py's lane regexes:

    python test-lane-gate.py

Background (2026-08-10): the apply threshold moved 7 -> 6. A funnel audit found
116 of 195 unclaimed 6s were off-lane "analyst" roles (Security Operations,
Incident Response, Pricing, Underwriting, Program, Contract/Billing) that scored
6 on SQL/Excel/dashboard overlap alone. Two things protect the new band:

  1. _cap_off_lane demotes an off-lane 6 to 5 (core data wording always wins).
  2. _boost_target_company's floor sits ABOVE the gate cap value, so employer
     reputation can no longer lift a gate-capped 5 onto the apply bar.

Both are easy to break with a well-meaning regex "simplification" — in
particular, a stem alternative like `underwrit` or `data scien` with a trailing
\\b silently never matches. Hence this table.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "applypilot" / "src"))
from applypilot.scoring.scorer import _cap_off_lane, _boost_target_company  # noqa: E402

# (title, expected score after the gate, why)
LANE_CASES = [
    # --- off-lane: shares the word "analyst" but the work is another field ---
    ("Security Operations Analyst", 5, "off-lane"),
    ("Incident Response Analyst", 5, "off-lane"),
    ("Pricing Analyst, The Americas", 5, "off-lane"),
    ("Analyst, Underwriting Analytics", 5, "off-lane stem 'underwrit'"),
    ("Program Analyst", 5, "off-lane"),
    ("Remote Property Insurance Quality Assurance Analyst", 5, "off-lane"),
    ("Marketing Analyst - Ads & Promotions", 5, "off-lane"),
    ("Contract to Cash - Contract Ops and Billing", 5, "off-lane"),
    ("Payroll Analyst", 5, "off-lane"),
    ("IT Support Analyst", 5, "off-lane"),
    # --- in-lane: core data wording must beat an incidental off-lane word ---
    ("Data Analyst, In-Store", 6, "core"),
    ("Business Analyst I, Category Pricing", 6, "core beats 'pricing'"),
    ("Data Analyst 3, Supply Chain Intelligence", 6, "core"),
    ("Business Intelligence Engineer, Global Procurement", 6, "core beats 'procurement'"),
    ("Associate Data Engineer (Early Career Talent)", 6, "core beats 'talent'"),
    ("Data Scientist, Security Issue Management", 6, "core stem beats 'security'"),
    ("Analytics Engineer", 6, "core"),
    ("Data Governance Analyst", 6, "core"),
    ("BI Developer", 6, "core"),
    ("Machine Learning Engineer", 6, "core"),
    # --- ambiguous: no core signal and no off-lane signal -> left alone ---
    ("Operations Analyst (Market Operations)", 6, "conservative: unknown stays"),
]


def main() -> int:
    fails = 0

    for title, expect, why in LANE_CASES:
        got = _cap_off_lane(title, {"score": 6, "reasoning": ""})["score"]
        if got != expect:
            fails += 1
            print(f"  FAIL {got} (want {expect})  {title}  [{why}]")

    # The gate must never touch 7+: that band has its own evidence bar and
    # 41 applications at score 7 produced zero rejections.
    for s in (7, 8, 9, 10):
        got = _cap_off_lane("Security Operations Analyst", {"score": s, "reasoning": ""})["score"]
        if got != s:
            fails += 1
            print(f"  FAIL 7+ exemption: score {s} -> {got}")

    # Boost floor: a gate-capped 5 must NOT be liftable onto the apply bar.
    prof = {"target_companies": ["Amazon", "Microsoft"]}
    job = {"company": "Amazon.com", "site": "linkedin"}
    for s, expect in ((5, 5), (6, 7), (7, 8), (8, 9), (9, 9)):
        got = _boost_target_company(job, {"score": s, "reasoning": ""}, prof)["score"]
        if got != expect:
            fails += 1
            print(f"  FAIL boost: score {s} -> {got} (want {expect})")

    total = len(LANE_CASES) + 4 + 5
    if fails:
        print(f"\n{fails}/{total} FAILED")
        return 1
    print(f"all {total} lane-gate checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
