"""Pick a scoring model by running the REAL scorer prompt over REAL jobs.

Why this exists: nvidia/nemotron-3-super-120b-a12b passed a hand-written
3-job JSON test on 2026-08-26 and was promoted to the NIM slot, then was
demoted after 8 requests in production on 2026-09-02 for "unparseable scoring
output". The toy prompt was not representative -- reasoning models behave
differently on the long scorer prompt. Test the way production calls it.

Reports parse rate and latency per candidate. No DB writes.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "applypilot/src")
from applypilot import config  # noqa: E402

config.load_env()
os.environ["LLM_FALLBACK_LOCAL"] = "0"

from applypilot.scoring import scorer  # noqa: E402
from applypilot import llm  # noqa: E402

CANDIDATES = [
    ("NIM", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
     "nvidia/nemotron-3-super-120b-a12b"),
    ("NIM", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
     "nvidia/nemotron-3-nano-30b-a3b"),
    ("NIM", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
     "nvidia/nemotron-3-ultra-550b-a55b"),
    ("NIM", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
     "mistralai/mistral-nemotron"),
    ("Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b"),
]

con = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db", timeout=180)
con.row_factory = sqlite3.Row
jobs = con.execute(
    """SELECT url, title, site, location, full_description
       FROM jobs WHERE length(COALESCE(full_description,'')) > 600
       ORDER BY discovered_at DESC LIMIT 4"""
).fetchall()
resume = scorer.RESUME_PATH.read_text(encoding="utf-8")
print(f"scoring {len(jobs)} real jobs through the production scorer\n")

for label, url, keyvar, model in CANDIDATES:
    key = os.environ.get(keyvar, "")
    if not key:
        print(f"  {model[:44]:44s} no key configured", flush=True)
        continue
    os.environ["LLM_URL"] = url
    os.environ["LLM_API_KEY"] = key
    os.environ["LLM_MODEL"] = model
    scorer._facts_cache = None
    # llm.get_client() memoises a module-level singleton, so without this every
    # candidate after the first was silently scored by the FIRST chain built —
    # the tell was two different models returning identical scores. Reset it,
    # and empty the fallback list so a failure cannot be masked by a different
    # provider answering in this candidate's name.
    llm._instance = None
    _real_fallbacks = llm._fallback_providers
    llm._fallback_providers = lambda *_a, **_k: []
    ok = bad = 0
    scores, t0 = [], time.time()
    for j in jobs:
        try:
            out = scorer.score_job(resume, dict(j))
            s = out.get("score")
            if s is None:
                bad += 1
            else:
                ok += 1
                scores.append(s)
        except Exception:
            bad += 1
    llm._fallback_providers = _real_fallbacks
    llm._instance = None
    el = time.time() - t0
    rate = f"{ok}/{len(jobs)}"
    verdict = "USABLE" if bad == 0 else ("MARGINAL" if ok > bad else "UNUSABLE")
    print(f"  {label:9s} {model[:40]:40s} parsed {rate:5s} "
          f"{el/len(jobs):5.1f}s/job  {verdict:9s} scores={scores}", flush=True)
