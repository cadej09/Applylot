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
# REST call with a hard timeout — `ollama run` has none and once hung a cycle
# for 5 hours (2026-07-16) when the CLI client raced the server at startup.
# Warm-up failure is non-fatal: the local model is only the last-resort fallback.
try {
    Invoke-RestMethod -Uri http://localhost:11434/api/generate -Method Post `
        -Body '{"model":"llama3.1:8b","prompt":"Say OK","stream":false}' `
        -ContentType 'application/json' -TimeoutSec 180 | Out-Null
    Write-Host "Model warm."
} catch {
    Write-Host "WARN: Ollama warm-up failed or timed out; continuing without local warm model."
}

Write-Host "=== [2/6] Heal DB (reset errors, unstick, dedupe reposts) ==="
# Moved to heal-db.py (2026-07-16): piping the old here-string into `python -`
# injected a BOM on some consoles and the stage silently SyntaxError'd.
python heal-db.py


if ($SkipDiscover) {
    Write-Host "=== [3a/6] Discover SKIPPED (-SkipDiscover) — enrich only ==="
    applypilot run enrich
} else {
    Write-Host "=== [3a/6] Discover fresh jobs (last 24h, all boards) + enrich ==="
    applypilot run discover enrich
}

Write-Host "=== [3a2/6] Pre-filter (ZERO API): stamp junk title/seniority/location jobs fit_score=0 ==="
# Runs AFTER discover/enrich, BEFORE score. Deterministically marks jobs that
# can't possibly fit (wrong role family, too senior, non-WA/non-US-remote)
# with fit_score=0 + a 'PREFILTERED' reason, so the scorer skips them for free
# — the single biggest cost saver in the cycle. The heal step above is careful
# NOT to wipe these marks.
# Internships are kept (user pursuing a Master's, 2026-07-30). They get scored
# like anything else; fix-gates then parks them as 'deferred' so they are never
# auto-applied while enrollment is unconfirmed. internship-report.py lists them.
$env:INCLUDE_INTERNSHIPS = "1"
python applypilot\applypilot-setup\prefilter.py

Write-Host "=== [3b/6] Score pending jobs (NVIDIA NIM 70B free; falls back Cerebras -> Groq; local 8B BANNED) ==="
# 2026-07-16: the local 8B is banned from SCORING too — when the chain sank to
# it, llama3.1:8b ignored the scoring instruction and answered with resume
# tailoring ("Here's a tailored version..."), derailed by the master resume's
# own header text. Unparsed output logged score=0 for 92 straight jobs.
# Better to leave jobs pending (heal re-queues them) than write garbage.
$env:LLM_FALLBACK_LOCAL = "0"
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

Write-Host "=== [3c/6] Gate sweeps POST-score (location / seniority / spam / experience / education) ==="
# Must run AFTER score so jobs discovered+scored THIS cycle are demoted below the
# score-7 threshold before any tailor/apply credit is spent on them. (Previously
# ran pre-discover, which let same-cycle master's/experience/mill jobs slip through.)
python fix-gates.py
python fix-education.py
# Refresh the human-review list of scored internships (never auto-applied).
python internship-report.py

Write-Host "=== [4/6] Tailor + covers (both NIM 70B, `$0) + PDFs ==="
# Lesson learned 2026-07-12: the claude CLI is a poor STRUCTURED-output
# writer (agent behavior breaks the tailor JSON contract), but excellent at
# free-form prose. So tailoring went to NIM and covers to the Claude CLI.
#
# COVERS MOVED OFF THE CLAUDE CLI 2026-08-13 (user: "I do not want to be
# spending more money on this", now on the Pro plan). Only two things in this
# pipeline consume Claude plan usage at all: the apply agents and the cover
# stage. A big cycle writes ~170 covers, so on a session-metered plan that is
# a large share of the budget spent on short prose a free 70B writes fine.
# Apply agents — where the plan budget actually earns applications — keep the
# CLI. If cover quality regresses, flip $useCliForCovers back to $true.
$nimKey4 = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^\s*NVIDIA_API_KEY\s*=' } | Select-Object -First 1
if (-not $nimKey4) { Write-Host "FATAL: no NVIDIA_API_KEY in ~/.applypilot/.env"; exit 1 }
$nimVal = ($nimKey4 -split '=', 2)[1].Trim()
$hasCli = [bool](Get-Command claude -ErrorAction SilentlyContinue)
$useCliForCovers = $false     # 2026-08-13: covers run on NIM to preserve plan budget
# Never let the local 8B write documents.
$env:LLM_FALLBACK_LOCAL = "0"
1..15 | ForEach-Object {
    # Tailor: strict JSON -> NIM llama-70B
    $env:LLM_URL = "https://integrate.api.nvidia.com/v1"
    $env:LLM_API_KEY = $nimVal
    $env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
    # normal, not lenient: banned-word warnings are free signal, and since
    # 2026-07-16 the LLM judge + fabrication checks run in every mode anyway
    # (lenient used to skip the judge, which shipped fabricated resumes).
    applypilot run tailor --validation normal --min-score 6
    # Covers: prose. NIM by default (free); Claude CLI only if flipped back on.
    if ($hasCli -and $useCliForCovers) {
        $env:LLM_URL = "claude-cli"; $env:LLM_API_KEY = ""; $env:LLM_MODEL = "haiku"
    }
    applypilot run cover pdf --validation normal --min-score 6
}
Remove-Item Env:LLM_FALLBACK_LOCAL -ErrorAction SilentlyContinue

Write-Host "=== [4b/6] Liveness pre-check (ZERO API): retire dead postings before spending agents ==="
# Measured 2026-08-13: 34% of apply-agent sessions were spent discovering a
# posting had expired. One HTTP GET catches those for free, before Chrome and
# the agent ever start. Conservative — only a 404/410 from a real ATS or an
# explicit expiry phrase retires a job; timeouts, bot-blocks (403/429), and
# aggregator URLs are left alone for the agent to judge. A high-scoring but
# stale job is NOT discarded unless it is confirmed dead (user 2026-08-13).
python check-expired.py
if ($LASTEXITCODE -ne 0) { Write-Host "  liveness pre-check failed (non-fatal); continuing to apply" }

Write-Host "=== [5/6] Auto-apply (1 worker, whole queue - pauses/resumes across session limits) ==="
# 1 worker since 2026-07-17: running 3 workers on CLONED copies of the same
# login session tripped Google's abuse detection and revoked the session
# everywhere (forced a manual re-login). worker-1/2 profile dirs are stale.
# AUTH PRE-CHECK (2026-08-02): the CLI's Max-plan OAuth can die underneath us —
# the login is shared with the interactive Claude Code session and refresh
# tokens rotate, so a race leaves "OAuth session expired and could not be
# refreshed". When that happened the apply stage ground through 23 jobs at $0,
# burning their attempt counters on instant spawn failures. One cheap probe
# first: if auth is down, skip the stage loudly — jobs stay claimable.
$authOk = $false
try {
    $probe = (claude -p "Reply with exactly: OK" --model haiku 2>&1 | Out-String)
    # Also catch USAGE LIMITS, not just auth. 2026-08-20: the weekly limit was
    # exhausted; the probe only looked for OAuth strings, so it passed and the
    # stage ground through 54 jobs whose agents each died instantly on
    # "You've hit your weekly limit" — burning an attempt on every one for
    # $1.37 total. Attempts are capped at 3, so a quota outage silently spent a
    # third of the retry budget for the whole queue. Treat a limit exactly like
    # dead auth: skip the stage loudly, leave the jobs claimable.
    if ($probe -notmatch "Failed to authenticate|OAuth|hit your (weekly|session|usage) limit|rate limit") {
        $authOk = $true
    }
} catch {}
if (-not $authOk) {
    Write-Host "APPLY SKIPPED: the claude CLI probe failed. Jobs stay claimable; no attempts burned."
    Write-Host "  probe said: $($probe.Trim() -replace "`r?`n.*", '')"
    Write-Host "  If OAuth/login: run 'claude' interactively and /login."
    Write-Host "  If a usage limit: wait for the reset time above, then re-run the apply stage."
} else {
taskkill /F /IM chrome.exe 2>$null
Start-Sleep 3
applypilot apply --limit 999 --workers 1 --min-score 6
}
# APPLY THRESHOLD is 7+ (raised from 6 on 2026-07-23, relaxed from a brief 8).

Write-Host "=== [5a1/6] Manual-review sheet (user 2026-08-14: full good-fit dataset for hand-applying) ==="
# Writes ~/.applypilot/manual_review.html — every 6+ job the bot could not
# submit, with clickable apply links and file:// links to the tailored resume
# and cover PDFs already on disk. Deliberately a FILE, not an email: the set is
# ~130 jobs / 260 PDFs (too big for an inbox), and a file still works when the
# Gmail OAuth token has expired, which is exactly when the packet email cannot.
python manual-review.py
if ($LASTEXITCODE -ne 0) { Write-Host "  review sheet failed (non-fatal)" }

Write-Host "=== [5a2/6] Manual-apply packet (user 2026-08-06: email manual jobs + internships) ==="
# Emails apply_status='manual' jobs (8+ at attempt cap) with resume+cover PDFs
# to the owner, plus the internship review list when it changed. State file
# prevents resends; exits quietly if nothing new or gmail-send token is dead.
python send-manual-packet.py
if ($LASTEXITCODE -ne 0) { Write-Host "  packet send failed (non-fatal, will retry next cycle)" }

Write-Host "=== [5b/6] Auto-prune DB (user 2026-07-31: no more manual pruning) ==="
# Stubs descriptions of rejected/low-score rows and VACUUMs, keeping the file
# well under GitHub's 100 MB push limit. Runs AFTER apply (nothing else touches
# the DB here, so the VACUUM's exclusive lock is safe) and BEFORE the push.
# History is never deleted — rows keep url/title/score/applied metadata, which
# is what the dedup guard, company throttle and prefilter skip-list run on.
# Internship descriptions are exempt inside prune-db.py.
python prune-db.py

Write-Host "=== [6/6] Sync data repo to GitHub ==="
Set-Location $HOME\.applypilot
git add -A
git commit -m "auto cycle $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
git push

Set-Location $HOME\job-ops
Write-Host ""
Write-Host "=== CYCLE COMPLETE ==="
applypilot status
