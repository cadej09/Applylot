# scheduled-cycle.ps1 - hourly hands-off backlog chipper (user 2026-07-25).
# Runs run-cycle.ps1 -SkipDiscover: enrich -> score (as free quota allows;
# aborts fast when dead) -> gates -> tailor/apply 7+ -> push. A lock file makes
# overlapping triggers skip, so a long scoring-rich cycle is never doubled up.
# When quota is dead the run finishes in minutes and the next hour retries;
# when quota is live it runs long and intervening triggers skip. Harmless once
# the backlog drains (every stage no-ops on an empty queue).
#
# Stop it:   schtasks /Change /TN "ApplyPilot-Hourly" /DISABLE
# Remove it: schtasks /Delete  /TN "ApplyPilot-Hourly" /F
#
# -Discover: run a FULL cycle including discovery, and skip the idle check
# (discovery IS the work — it refills a drained pipeline). Use this for the
# manual "go find new jobs" run; it takes the same lock, so the hourly task
# can't start a second cycle on top of it once discovery adds work.
param([switch]$Discover)

$lock = "$HOME\.applypilot\cycle.lock"
if (Test-Path $lock) {
    # The lock stores the owning PID. If that process is gone the lock leaked
    # (killed cycle / crash / reboot) — take it over instead of blocking the
    # scheduler for hours. Age is only a last-resort backstop.
    $owner = (Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    $alive = $false
    if ($owner -match '^\d+$') {
        $alive = [bool](Get-Process -Id ([int]$owner) -ErrorAction SilentlyContinue)
    }
    $age = (Get-Date) - (Get-Item $lock).LastWriteTime
    if ($alive -and $age.TotalHours -lt 6) {
        Write-Host ("SCHED: cycle already running (pid {0}, {1}m), skipping this hour." -f $owner, [int]$age.TotalMinutes)
        exit 0
    }
    Write-Host ("SCHED: stale lock (pid {0} gone or {1}h old) — taking over." -f $owner, [int]$age.TotalHours)
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}

# NEVER let a real API key reach the apply stage's claude CLI (would bill the
# API instead of the Max plan).
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

# Skip entirely when there is no work (user 2026-07-26). A full cycle warms
# Ollama, walks every stage, launches Chrome and pushes git — pointless when
# nothing is pending. work-check.py exits 1 when score/tailor/enrich/claimable
# are all zero. NOTE: it counts CLAIMABLE applies (throttle-aware), not the raw
# ready-count, which over-reports jobs the worker can never actually claim.
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$queues = (& python .\work-check.py) 2>&1
$hasWork = ($LASTEXITCODE -eq 0)
if (-not $hasWork -and -not $Discover) {
    Write-Host ("SCHED: nothing to do ({0}) — skipping this hour." -f $queues)
    exit 0
}
if ($Discover) {
    Write-Host ("SCHED: FULL cycle with discovery ({0})." -f $queues)
} else {
    Write-Host ("SCHED: work pending ({0}) — running cycle." -f $queues)
}

$PID | Out-File -FilePath $lock -Encoding utf8 -Force
try {
    $log = "$HOME\job-ops\logs\sched-{0:yyyy-MM-dd}.log" -f (Get-Date)
    $mode = if ($Discover) { "FULL (with discovery)" } else { "SkipDiscover" }
    "===== SCHED CYCLE START {0:yyyy-MM-dd HH:mm} [{1}] =====" -f (Get-Date), $mode |
        Tee-Object -FilePath $log -Append
    if ($Discover) {
        & powershell -ExecutionPolicy Bypass -File "$HOME\job-ops\run-cycle.ps1" *>&1 |
            ForEach-Object { "$_" } | Tee-Object -FilePath $log -Append
    } else {
        & powershell -ExecutionPolicy Bypass -File "$HOME\job-ops\run-cycle.ps1" -SkipDiscover *>&1 |
            ForEach-Object { "$_" } | Tee-Object -FilePath $log -Append
    }
} finally {
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
