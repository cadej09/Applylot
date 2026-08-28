"""Verify the NIM model swap end-to-end through the real scorer.

meta/llama-3.3-70b-instruct reached EOL 2026-08-26T09:00Z (HTTP 410). This
exercises the actual scoring code path against the replacement rather than a
bare HTTP call, so prompt handling and JSON parsing are covered too.
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "applypilot/src")
from applypilot import config

config.load_env()
os.environ["LLM_URL"] = "https://integrate.api.nvidia.com/v1"
os.environ["LLM_MODEL"] = "nvidia/nemotron-3-super-120b-a12b"
os.environ["LLM_API_KEY"] = os.environ.get("NVIDIA_API_KEY", "")
os.environ["LLM_FALLBACK_LOCAL"] = "0"

from applypilot.scoring import scorer  # noqa: E402 - must follow env setup

# Same source the real scoring stage uses (scorer.py:624).
RESUME = scorer.RESUME_PATH.read_text(encoding="utf-8")

con = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db", timeout=60)
con.row_factory = sqlite3.Row
rows = con.execute(
    """SELECT url, title, COALESCE(company,site) emp, location, fit_score prev,
              COALESCE(full_description,'') full_description
       FROM jobs WHERE length(COALESCE(full_description,'')) > 400
       ORDER BY discovered_at DESC LIMIT 3"""
).fetchall()

print(f"scoring {len(rows)} real unscored jobs through applypilot.scoring.scorer")
ok = 0
for r in rows:
    try:
        d = dict(r); d.setdefault('site', d.get('emp'))
        out = scorer.score_job(RESUME, d)
        score = out.get("score") if isinstance(out, dict) else out
        status = "OK " if score is not None else "NULL(!)"
        ok += score is not None
        print(f"  {status} new={score} (was {r['prev']})  {(r['title'] or '')[:38]:38s} | {(r['emp'] or '?')[:18]}")
    except Exception as e:
        print(f"  FAIL {type(e).__name__}: {str(e)[:130]}")
print(f"\n{ok}/{len(rows)} scored successfully on the replacement model")
