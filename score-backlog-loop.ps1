# score-backlog-loop.ps1 - chew through the unscored backlog across NIM outage waves.
#
# Why a loop: the scorer aborts after 10 consecutive provider-chain failures.
# That guard assumes a chain exists to fall back to. As of 2026-09-05 NIM is the
# only live provider (Cerebras 402, Groq/OpenRouter 429, Kimi suspended for
# insufficient balance), and NIM 503s in waves - so a single wave ends the run
# even though NIM answers 97% of requests. Each pass scores ~100-150 jobs before
# a wave trips the abort; re-running picks up exactly where it left off because
# failures leave fit_score NULL rather than 0.
#
# Stops early when the backlog stops shrinking (nothing left that CAN be scored)
# so it never spins uselessly.
param(
    [int]$MaxPasses = 12,
    [int]$PauseSeconds = 120
)

$ErrorActionPreference = "Continue"
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:LLM_FALLBACK_LOCAL = "0"

# Read the NIM key from .env the way run-cycle.ps1 does. Setting LLM_API_KEY from
# a shell variable silently yields an empty key and a 401 on every call.
$nimLine = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^NVIDIA_API_KEY=' } | Select-Object -First 1
$env:LLM_API_KEY = ($nimLine -split '=', 2)[1].Trim()
$env:LLM_URL = "https://integrate.api.nvidia.com/v1"
$env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
if ($env:LLM_API_KEY.Length -lt 10) { Write-Host "NIM key not found in .env"; exit 1 }

function UnscoredCount {
    return [int](python -c "import sqlite3;from pathlib import Path;print(sqlite3.connect(Path.home()/'.applypilot'/'applypilot.db',timeout=180).execute('SELECT count(*) FROM jobs WHERE fit_score IS NULL').fetchone()[0])")
}

$before = UnscoredCount
Write-Host "BACKLOG-LOOP: starting with $before unscored"

for ($i = 1; $i -le $MaxPasses; $i++) {
    $start = UnscoredCount
    if ($start -eq 0) { Write-Host "BACKLOG-LOOP: backlog empty."; break }

    applypilot run score *>&1 | Out-String | Out-Null

    $end = UnscoredCount
    $done = $start - $end
    Write-Host "BACKLOG-LOOP: pass $i scored $done, $end remaining"

    # No progress means nothing scoreable is left (stubbed descriptions) or the
    # provider is hard-down - either way, more passes will not help.
    if ($done -le 0) {
        Write-Host "BACKLOG-LOOP: pass made no progress, stopping."
        break
    }
    Start-Sleep -Seconds $PauseSeconds
}

$after = UnscoredCount
Write-Host "BACKLOG-LOOP: done. $before -> $after (scored $($before - $after))"
