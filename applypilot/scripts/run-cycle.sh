#!/usr/bin/env bash
# ============================================================
# run-cycle.sh - ApplyPilot full autonomous cycle (macOS/Linux)
# Usage:  ./scripts/run-cycle.sh [--skip-discover]
# Mirror of run-cycle.ps1. No secrets in this file — keys are
# read from ~/.applypilot/.env at runtime.
# ============================================================
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

export PYTHONUTF8=1
ENV_FILE="$HOME/.applypilot/.env"
getkey() { grep -E "^\s*$1\s*=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d ' '; }

echo "=== [0/6] Keep machine awake ==="
if command -v caffeinate >/dev/null; then
  caffeinate -dimsu -w $$ &   # macOS: stay awake while this script runs
fi

echo "=== [1/6] Ollama (optional local fallback) ==="
if command -v ollama >/dev/null; then
  curl -s http://localhost:11434/api/tags >/dev/null || (ollama serve &>/dev/null & sleep 10)
  ollama run llama3.1:8b "Say OK" >/dev/null 2>&1 || true
fi

echo "=== [2/6] Heal DB ==="
python - <<'EOF'
import hashlib, re, sqlite3
from pathlib import Path
c = sqlite3.connect(Path.home() / ".applypilot" / "applypilot.db")
c.row_factory = sqlite3.Row
n1 = c.execute("UPDATE jobs SET fit_score=NULL WHERE fit_score=0").rowcount
n2 = c.execute("UPDATE jobs SET apply_status=NULL, agent_id=NULL WHERE apply_status='in_progress'").rowcount
for col in ("response_status TEXT", "company TEXT"):
    try: c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    except sqlite3.OperationalError: pass
n4 = c.execute("UPDATE jobs SET apply_status='expired', apply_error='stale: posted >30 days ago' "
               "WHERE fit_score >= 7 AND apply_status IS NULL "
               "AND discovered_at < datetime('now', '-30 days')").rowcount
rows = c.execute("SELECT url, title, full_description, discovered_at FROM jobs "
                 "WHERE fit_score >= 7 AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')").fetchall()
groups = {}
for r in rows:
    key = (re.sub(r"\W+", "", (r["title"] or "").lower()),
           hashlib.md5(re.sub(r"\s+", " ", (r["full_description"] or "")[:2000]).encode()).hexdigest())
    groups.setdefault(key, []).append(r)
n3 = 0
for g in groups.values():
    if len(g) < 2: continue
    g.sort(key=lambda r: r["discovered_at"] or "", reverse=True)
    for r in g[1:]:
        c.execute("UPDATE jobs SET fit_score=6, score_reasoning=COALESCE(score_reasoning,'') "
                  "|| ' [duplicate posting]' WHERE url=?", (r["url"],))
        n3 += 1
c.commit()
print(f"reset {n1} error-scores, unstuck {n2}, demoted {n3} dupes, retired {n4} stale")
EOF

if [[ "${1:-}" == "--skip-discover" ]]; then
  echo "=== [3a/6] Discover SKIPPED — enrich only ==="
  applypilot run enrich
else
  echo "=== [3a/6] Discover fresh jobs (last 24h) + enrich ==="
  applypilot run discover enrich
fi

echo "=== [3b/6] Score (NVIDIA NIM 70B free; falls back Groq -> local) ==="
NIM_KEY=$(getkey NVIDIA_API_KEY)
if [[ -n "$NIM_KEY" ]]; then
  export LLM_URL="https://integrate.api.nvidia.com/v1"
  export LLM_API_KEY="$NIM_KEY"
  export LLM_MODEL="meta/llama-3.3-70b-instruct"
else
  export LLM_URL="http://localhost:11434/v1" LLM_API_KEY="ollama" LLM_MODEL="llama3.1:8b"
fi
applypilot run score -w 2

echo "=== [4/6] Tailor + covers + PDFs ==="
GEM_KEY=$(getkey GEMINI_API_KEY)
ANT_KEY=$(getkey LLM_API_KEY)
if [[ -n "$GEM_KEY" ]]; then
  export LLM_URL="https://generativelanguage.googleapis.com/v1beta/openai"
  export LLM_API_KEY="$GEM_KEY" LLM_MODEL="gemini-2.5-flash"
elif [[ -n "$ANT_KEY" ]]; then
  export LLM_URL="https://api.anthropic.com/v1"
  export LLM_API_KEY="$ANT_KEY" LLM_MODEL="claude-haiku-4-5-20251001"
else
  echo "FATAL: no GEMINI_API_KEY or LLM_API_KEY in ~/.applypilot/.env"; exit 1
fi
for i in $(seq 1 15); do applypilot run tailor cover pdf --validation lenient; done

echo "=== [5/6] Auto-apply ==="
pkill -f "Google Chrome" 2>/dev/null; sleep 3
applypilot apply --limit 999 --workers 2

echo "=== [6/6] Sync data repo ==="
cd "$HOME/.applypilot"
git add -A && git commit -m "auto cycle $(date '+%Y-%m-%d %H:%M')" && git push

echo "=== CYCLE COMPLETE ==="
applypilot status
