# ============================================================
# run-cycle.ps1 - ApplyPilot full autonomous cycle
# Usage:  powershell -ExecutionPolicy Bypass -File $HOME\job-ops\run-cycle.ps1
# Stages: heal+dedupe -> score (local, free) -> tailor/cover/pdf (Haiku)
#         -> apply (Max plan, rate-limit aware) -> git push
# Safe to re-run any time: every stage skips work already done.
# NOTE: no secrets in this file — the Anthropic key is read from ~/.applypilot/.env
# ============================================================
param([switch]$SkipDiscover)  # -SkipDiscover: resume a same-day run without re-crawling the boards

$ErrorActionPreference = "Continue"
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1

# Force UTF-8 everywhere: the Korean-locale console defaults to cp949, which
# crashes rich's output on em-dashes/checkmarks whenever stdout is piped.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

Write-Host "=== [0/6] Keep PC awake ==="
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null

Write-Host "=== [1/6] Ensure Ollama is up + scoring model warmed ==="
$env:Path += ";$env:LOCALAPPDATA\Programs\Ollama"
$env:OLLAMA_KEEP_ALIVE = "4h"
try {
    Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 5 | Out-Null
    Write-Host "Ollama: running"
} catch {
    Write-Host "Ollama: starting..."
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep 12
}
Write-Host "Warming llama3.1:8b (scoring model)..."
ollama run llama3.1:8b "Say OK" | Out-Null
Write-Host "Model warm."

Write-Host "=== [2/6] Heal DB (reset errors, unstick, dedupe reposts) ==="
@'
import hashlib, re, sqlite3
c = sqlite3.connect(r"C:\Users\you\.applypilot\applypilot.db")
c.row_factory = sqlite3.Row

n1 = c.execute("UPDATE jobs SET fit_score=NULL WHERE fit_score=0").rowcount
n2 = c.execute("UPDATE jobs SET apply_status=NULL, agent_id=NULL WHERE apply_status='in_progress'").rowcount

# Idempotent schema upgrades (response tracking + real employer name)
for col in ("response_status TEXT", "company TEXT"):
    try:
        c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass  # already exists

# Stale-job retirement: unapplied postings older than 30 days are usually
# filled or buried; retire them so the queue stays fresh.
n4 = c.execute(
    "UPDATE jobs SET apply_status='expired', apply_error='stale: posted >30 days ago' "
    "WHERE fit_score >= 7 AND apply_status IS NULL "
    "AND discovered_at < datetime('now', '-30 days')"
).rowcount

# Dedupe reposts: same normalized title + same description = same job under
# a different URL. Keep the newest, demote the rest out of the 7+ queue.
rows = c.execute(
    "SELECT url, title, full_description, discovered_at FROM jobs "
    "WHERE fit_score >= 7 AND COALESCE(apply_status,'') NOT IN ('applied','in_progress')"
).fetchall()
groups = {}
for r in rows:
    key = (
        re.sub(r"\W+", "", (r["title"] or "").lower()),
        hashlib.md5(re.sub(r"\s+", " ", (r["full_description"] or "")[:2000]).encode()).hexdigest(),
    )
    groups.setdefault(key, []).append(r)
n3 = 0
for g in groups.values():
    if len(g) < 2:
        continue
    g.sort(key=lambda r: r["discovered_at"] or "", reverse=True)
    for r in g[1:]:
        c.execute(
            "UPDATE jobs SET fit_score=6, "
            "score_reasoning=COALESCE(score_reasoning,'') || ' [duplicate posting]' "
            "WHERE url=?", (r["url"],))
        n3 += 1
c.commit()
print(f"reset {n1} error-scores, unstuck {n2} jobs, demoted {n3} duplicate reposts, retired {n4} stale")
'@ | python -

if ($SkipDiscover) {
    Write-Host "=== [3a/6] Discover SKIPPED (-SkipDiscover) — enrich only ==="
    applypilot run enrich
} else {
    Write-Host "=== [3a/6] Discover fresh jobs (last 24h, all boards) + enrich ==="
    applypilot run discover enrich
}

Write-Host "=== [3b/6] Score pending jobs (NVIDIA NIM 70B free; auto-falls back to Groq -> local) ==="
$nimKey = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^\s*NVIDIA_API_KEY\s*=' } | Select-Object -First 1
if ($nimKey) {
    $env:LLM_URL = "https://integrate.api.nvidia.com/v1"
    $env:LLM_API_KEY = ($nimKey -split '=', 2)[1].Trim()
    $env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
} else {
    $env:LLM_URL = "http://localhost:11434/v1"
    $env:LLM_API_KEY = "ollama"
    $env:LLM_MODEL = "llama3.1:8b"
}
applypilot run score -w 2

Write-Host "=== [3c/6] Deterministic gates (location / seniority / spam / experience / education) ==="
# These stamp junk BELOW the score-7 tailor threshold so no tailor/apply credit
# is ever spent on out-of-state, senior, content-mill, over-experience, or
# graduate-degree-required postings. Cheap, no API. Runs every cycle.
python fix-gates.py
python fix-education.py

Write-Host "=== [4/6] Tailor + covers + PDFs (Gemini 2.5 Flash on trial credit; Haiku if no key) ==="
$gemKey = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^\s*GEMINI_API_KEY\s*=' } | Select-Object -First 1
if ($gemKey) {
    $env:LLM_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    $env:LLM_API_KEY = ($gemKey -split '=', 2)[1].Trim()
    $env:LLM_MODEL = "gemini-2.5-flash"
} else {
    $dotenvKey = Get-Content "$HOME\.applypilot\.env" |
        Where-Object { $_ -match '^\s*LLM_API_KEY\s*=' } | Select-Object -First 1
    if (-not $dotenvKey) { Write-Host "FATAL: no GEMINI_API_KEY or LLM_API_KEY in ~/.applypilot/.env"; exit 1 }
    $env:LLM_URL = "https://api.anthropic.com/v1"
    $env:LLM_API_KEY = ($dotenvKey -split '=', 2)[1].Trim()
    $env:LLM_MODEL = "claude-haiku-4-5-20251001"
}
1..15 | ForEach-Object {
    applypilot run tailor cover pdf --validation lenient
}

Write-Host "=== [5/6] Auto-apply (2 workers on Max plan, whole queue - pauses/resumes across session limits) ==="
taskkill /F /IM chrome.exe 2>$null
Start-Sleep 3
applypilot apply --limit 999 --workers 2

Write-Host "=== [6/6] Sync data repo to GitHub ==="
Set-Location $HOME\.applypilot
git add -A
git commit -m "auto cycle $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
git push

Set-Location $HOME\job-ops
Write-Host ""
Write-Host "=== CYCLE COMPLETE ==="
applypilot status
