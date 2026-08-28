# ============================================================
# finish-tonight.ps1 - resume pipeline WITHOUT discovery and run
# through to auto-apply. Safe to re-run; every stage skips done work.
#
# Model assignment (the standing policy):
#   score        -> NVIDIA NIM llama-70B   (free; caps catch the garbage)
#   tailor/cover -> Vertex Gemini 2.5 Flash (Google $300 credit; best writer)
#   apply        -> Claude Code on Max plan ($0; ANTHROPIC key stripped)
#
# Usage:  powershell -ExecutionPolicy Bypass -File $HOME\job-ops\finish-tonight.ps1
# ============================================================
$ErrorActionPreference = "Continue"
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1

# UTF-8 (Korean-locale console defaults to cp949 and crashes rich output)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

Write-Host "=== [0/6] Keep PC awake ==="
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null

Write-Host "=== [1/6] Finish scoring pending jobs (NIM free tier) ==="
$nimKey = Get-Content "$HOME\.applypilot\.env" |
    Where-Object { $_ -match '^\s*NVIDIA_API_KEY\s*=' } | Select-Object -First 1
if ($nimKey) {
    $env:LLM_URL = "https://integrate.api.nvidia.com/v1"
    $env:LLM_API_KEY = ($nimKey -split '=', 2)[1].Trim()
    $env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
} else {
    $env:LLM_URL = "vertex"; $env:LLM_API_KEY = "vertex"
    $env:LLM_MODEL = "google/gemini-2.5-flash"
}
applypilot run score -w 2

Write-Host "=== [2/6] Gate sweeps on the fresh scores ==="
python fix-gates.py
python fix-education.py
python fix-covers.py

Write-Host "=== [3/6] Tailor (NIM 70B strict-JSON) + covers (Claude CLI prose) + PDFs ==="
$nimVal = ($nimKey -split '=', 2)[1].Trim()
$hasCli = [bool](Get-Command claude -ErrorAction SilentlyContinue)
$env:LLM_FALLBACK_LOCAL = "0"
1..10 | ForEach-Object {
    $env:LLM_URL = "https://integrate.api.nvidia.com/v1"
    $env:LLM_API_KEY = $nimVal
    $env:LLM_MODEL = "nvidia/nemotron-3-super-120b-a12b"
    applypilot run tailor --validation lenient
    if ($hasCli) {
        $env:LLM_URL = "claude-cli"; $env:LLM_API_KEY = ""; $env:LLM_MODEL = "haiku"
    }
    applypilot run cover pdf --validation lenient
}
Remove-Item Env:LLM_FALLBACK_LOCAL -ErrorAction SilentlyContinue

Write-Host "=== [4/6] Final QA on the ready queue ==="
python qa-ready.py

Write-Host "=== [5/6] Auto-apply (Max plan, 3 workers, whole queue) ==="
taskkill /F /IM chrome.exe 2>$null
Start-Sleep 3
applypilot apply --limit 999 --workers 3

Write-Host "=== [6/6] Sync data repo ==="
Set-Location $HOME\.applypilot
git add -A
git commit -m "overnight finish $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
git push

Set-Location $HOME\job-ops
Write-Host ""
Write-Host "=== DONE ==="
applypilot status
