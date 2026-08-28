# side-tailor.ps1 - tailor+cover+pdf on the side while the main cycle crawls.
# Exits as soon as the main cycle reaches its own tailor stage (=== [4/6])
# so the two never double-process the pending_tailor queue.
param([string]$CycleLog)

$ErrorActionPreference = "Continue"
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

function CycleAtStage4 {
    if (-not (Test-Path $CycleLog)) { return $false }
    return [bool](Select-String -Path $CycleLog -Pattern '=== \[4/6\]' -Quiet)
}

$nimKey = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^\s*NVIDIA_API_KEY\s*=' } | Select-Object -First 1
if (-not $nimKey) { Write-Host "FATAL: no NVIDIA_API_KEY"; exit 1 }
$nimVal = ($nimKey -split '=', 2)[1].Trim()
$hasCli = [bool](Get-Command claude -ErrorAction SilentlyContinue)
$env:LLM_FALLBACK_LOCAL = "0"

foreach ($i in 1..8) {
    if (CycleAtStage4) { Write-Host "SIDE-TAILOR: cycle reached stage 4, exiting."; break }
    $pending = [int](python .\tailor-count.py)
    Write-Host "SIDE-TAILOR: loop $i, pending_tailor=$pending"
    if ($pending -lt 1) { Write-Host "SIDE-TAILOR: tailor queue drained, exiting."; break }
    $env:LLM_URL = "https://integrate.api.nvidia.com/v1"
    $env:LLM_API_KEY = $nimVal
    $env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
    applypilot run tailor --validation normal --min-score 6
    if (CycleAtStage4) { Write-Host "SIDE-TAILOR: cycle reached stage 4, exiting."; break }
    if ($hasCli) {
        $env:LLM_URL = "claude-cli"; $env:LLM_API_KEY = ""; $env:LLM_MODEL = "haiku"
    }
    applypilot run cover pdf --validation normal --min-score 6
}
Write-Host "SIDE-TAILOR: done."
