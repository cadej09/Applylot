# side-apply.ps1 - chunked apply passes on the side while the main cycle runs.
# Small --limit chunks so each pass exits cleanly. Exits the moment the main
# cycle reaches its OWN apply stage (=== [5/6]), which taskkills chrome -- that
# is the collision to avoid. (Normal use: the operator stops the cycle before
# stage 5, so this guard is just a safety net.) No taskkill here.
param([string]$CycleLog)

$ErrorActionPreference = "Continue"
Set-Location $HOME\job-ops
& .\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

function CycleAtApply {
    if (-not $CycleLog) { return $false }
    if (-not (Test-Path $CycleLog)) { return $false }
    return [bool](Select-String -Path $CycleLog -Pattern '=== \[5/6\]' -Quiet)
}

function ReadyCount {
    return [int](python .\ready-count.py)
}

function TailorPending {
    return [int](python .\tailor-count.py)
}

# ready-count.py over-counts vs the launcher's acquire_job (it can't model the
# per-company weekly throttle or the application_url requirement). So a chunk
# that claims NOTHING ("Done: 0 applied, 0 failed") means the queue is
# effectively unclaimable even though ReadyCount > 0. Track consecutive no-op
# chunks and exit instead of busy-spinning (that bug spawned chrome every ~10s).
$idle = 0
$noop = 0
while ($true) {
    if (CycleAtApply) { Write-Host "SIDE-APPLY: cycle reached its own apply stage, exiting."; break }
    $ready = ReadyCount
    $feeding = TailorPending   # >0 means more resumes (=> more claimable jobs) are still coming
    Write-Host "SIDE-APPLY: ready queue = $ready, tailor pipeline = $feeding"
    if ($ready -lt 1) {
        # Nothing ready. Keep waiting while the pipeline is still tailoring; only
        # give up once it has been dry AND nothing more is coming.
        if ($feeding -gt 0) { $idle = 0; Start-Sleep 120; continue }
        $idle++
        if ($idle -ge 20) { Write-Host "SIDE-APPLY: queue empty and pipeline drained, exiting."; break }
        Start-Sleep 120
        continue
    }
    $idle = 0
    $out = (applypilot apply --limit 3 --workers 1 --min-score 6 2>&1 | Out-String)
    Write-Host $out
    # USAGE-LIMIT BAIL (2026-08-21). A plan limit does not error here — the
    # agent returns a 130-byte "You've hit your weekly limit" transcript and the
    # chunk reports failures, so the loop would keep going and burn an
    # apply_attempt on every remaining job (that cost 52 attempts on 08-20).
    # Stop immediately and loudly; the jobs stay claimable for after the reset.
    if ($out -match "hit your (weekly|session|usage) limit") {
        $resetLine = ([regex]::Match($out, "hit your \w+ limit[^\r\n]*")).Value
        Write-Host "SIDE-APPLY: STOPPING — plan usage limit reached. $resetLine"
        Write-Host "SIDE-APPLY: remaining jobs left claimable; re-run after the reset."
        break
    }
    if (CycleAtApply) { Write-Host "SIDE-APPLY: cycle reached its own apply stage, exiting."; break }
    $m = [regex]::Match($out, 'Done:\s*(\d+)\s*applied,\s*(\d+)\s*failed')
    $claimedNothing = $m.Success -and [int]$m.Groups[1].Value -eq 0 -and [int]$m.Groups[2].Value -eq 0
    if ($claimedNothing) {
        # Ready-count sees jobs but acquire_job claimed none (all throttled / no
        # application_url). If tailoring is still feeding, wait for fresh
        # claimable jobs rather than exit; only bail when the pipeline is dry.
        if ($feeding -gt 0) { $noop = 0; Start-Sleep 120; continue }
        $noop++
        if ($noop -ge 3) {
            Write-Host "SIDE-APPLY: 3 chunks claimed nothing and pipeline dry (throttled/unclaimable), exiting."
            break
        }
        Start-Sleep 60
    } else {
        $noop = 0
        Start-Sleep 10
    }
}
Write-Host "SIDE-APPLY: done."
