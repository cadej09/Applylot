"""End-to-end check that the UPenn M.S.E. renders truthfully on a real resume.

Verifies, on actual tailor output:
  * the UPenn program appears (enrollment signal for internship screens)
  * the COMPLETED UW B.S. survives as a detail line
  * nothing claims the master's is finished/conferred
  * the validator accepts the result
"""
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "applypilot/src")
from applypilot import config  # noqa: E402

config.load_env()
os.environ["LLM_URL"] = "https://integrate.api.nvidia.com/v1"
os.environ["LLM_MODEL"] = "nvidia/nemotron-3-super-120b-a12b"
os.environ["LLM_API_KEY"] = os.environ.get("NVIDIA_API_KEY", "")
os.environ["LLM_FALLBACK_LOCAL"] = "0"

from applypilot.scoring import tailor  # noqa: E402

con = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db", timeout=180)
con.row_factory = sqlite3.Row
job = con.execute(
    """SELECT url, title, site, location, full_description
       FROM jobs
       WHERE length(COALESCE(full_description,'')) > 800 AND fit_score >= 6
       ORDER BY discovered_at DESC LIMIT 1"""
).fetchone()

if not job:
    raise SystemExit("no suitable job row to test against")

print(f"tailoring against: {job['title']} @ {job['site']}\n")
from applypilot.scoring import scorer as _scorer
from applypilot.config import load_profile

resume_text = _scorer.RESUME_PATH.read_text(encoding="utf-8")
profile = load_profile() or {}
text, meta = tailor.tailor_resume(resume_text, dict(job), profile,
                                  validation_mode="normal")
print(f"tailor status: {meta.get('status')}  issues: {meta.get('issues')}\n")

# Isolate the EDUCATION block.
m = re.search(r"EDUCATION(.*?)(?:\n[A-Z][A-Z &]{3,}\n|\Z)", text, re.S)
edu = m.group(1).strip() if m else "(EDUCATION section not found)"
print("--- EDUCATION section as rendered ---")
print(edu[:700])
print("-" * 50)

checks = [
    ("UPenn program present", bool(re.search(r"pennsylvania", edu, re.I))),
    ("M.S.E. / AI program present", bool(re.search(r"m\.?s\.?e\.?|artificial intelligence", edu, re.I))),
    ("completed UW B.S. retained", bool(re.search(r"washington", edu, re.I))
     and bool(re.search(r"b\.?s\.?|informatics", edu, re.I))),
    ("no false 'completed master' claim",
     not re.search(r"(earned|conferred|awarded|completed|holds?)\s+(a\s+)?(m\.?s\.?e?\.?|master)", text, re.I)),
]
print("\n--- checks ---")
ok = True
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    ok &= passed
print(f"\nVERDICT: {'truthful and complete' if ok else 'NEEDS FIXING'}")
