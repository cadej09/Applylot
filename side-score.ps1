# side-score.ps1 - catch-up scoring after the 2026-07-20 quota outage.
# Retries the score stage every 15 min until the provider chain is alive
# (the scorer now aborts fast on a dead chain instead of writing 0s),
# then runs the POST-score gate sweeps (mandatory ordering).

$ErrorActionPreference = "Continue"
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$nimKey = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^\s*NVIDIA_API_KEY\s*=' } | Select-Object -First 1
$env:LLM_URL = "https://integrate.api.nvidia.com/v1"
$env:LLM_API_KEY = ($nimKey -split '=', 2)[1].Trim()
$env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
$env:LLM_FALLBACK_LOCAL = "0"

foreach ($attempt in 1..12) {
    $pending = [int](python -c "import sqlite3,os;print(sqlite3.connect(os.path.expanduser('~/.applypilot/applypilot.db'),timeout=30).execute('SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL AND full_description IS NOT NULL').fetchone()[0])")
    Write-Host "SIDE-SCORE: attempt $attempt, pending=$pending"
    if ($pending -lt 1) { break }
    $log = "$env:TEMP\side-score-run.log"
    applypilot run score -w 2 *>&1 | Tee-Object -FilePath $log | Out-Null
    if (Select-String -Path $log -Pattern "Aborting scoring run" -Quiet) {
        Write-Host "SIDE-SCORE: chain still dead, sleeping 15 min"
        Start-Sleep 900
    }
}

Write-Host "SIDE-SCORE: running post-score gate sweeps"
python fix-gates.py
python fix-education.py
Write-Host "SIDE-SCORE: done."
