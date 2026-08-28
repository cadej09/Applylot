# run-all.ps1 — job-ops end-to-end pipeline (Windows)
#
#   .\run-all.ps1                 # discover → bridge → evaluate/tailor → apply DRY-RUN
#   .\run-all.ps1 -Submit        # same, but applications are actually submitted
#   .\run-all.ps1 -SkipDiscover  # reuse existing discoveries
#   .\run-all.ps1 -MinScore 8 -Limit 5
#
# Requires per machine: node+npm (career-ops), python+applypilot CLI, claude CLI.

param(
    [int]$MinScore = 7,
    [int]$Limit = 10,
    [switch]$Submit,
    [switch]$SkipDiscover,
    [switch]$SkipEvaluate,
    [int]$ApplyWorkers = 1
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host "`n=== job-ops pipeline ===" -ForegroundColor Cyan

# Stage 1 — ApplyPilot discovery + pre-scoring
if (-not $SkipDiscover) {
    Write-Host "`n[1/4] ApplyPilot: discover + enrich + pre-score..." -ForegroundColor Yellow
    Push-Location "$Root\applypilot"
    applypilot run
    Pop-Location
} else { Write-Host "`n[1/4] Discovery skipped." -ForegroundColor DarkGray }

# Stage 2 — Bridge: high-fit discoveries → career-ops inbox
Write-Host "`n[2/4] Bridge: ApplyPilot → career-ops pipeline inbox..." -ForegroundColor Yellow
Push-Location "$Root\career-ops"
node applypilot-bridge.mjs --min-score $MinScore --limit $Limit
Pop-Location

# Stage 3 — career-ops: deep evaluation + tailored CV/cover + handoff back
if (-not $SkipEvaluate) {
    Write-Host "`n[3/4] career-ops: evaluate, tailor, hand off (headless Claude)..." -ForegroundColor Yellow
    Push-Location "$Root\career-ops"
    $prompt = @"
Process every pending URL in data/pipeline.md following modes/pipeline.md (batch mode).
For each job scoring >= 4.0/5: generate the tailored CV PDF, then run
  node applypilot-handoff.mjs --url <job-url> --pdf <generated-pdf> --txt <tailored-cv-text-file>
so ApplyPilot applies with the tailored materials. Below 4.0/5: mark Evaluated, no handoff.
Finish with node merge-tracker.mjs.
"@
    # --dangerously-skip-permissions is required for unattended runs; this repo is
    # the sandbox it operates in. Remove it to review each action interactively.
    claude -p $prompt --dangerously-skip-permissions
    Pop-Location
} else { Write-Host "`n[3/4] Evaluation skipped." -ForegroundColor DarkGray }

# Stage 4 — ApplyPilot: form-filling application
Write-Host "`n[4/4] ApplyPilot: auto-apply..." -ForegroundColor Yellow
Push-Location "$Root\applypilot"
if ($Submit) {
    Write-Host "SUBMIT MODE — applications WILL be sent." -ForegroundColor Red
    applypilot apply --workers $ApplyWorkers
} else {
    Write-Host "Dry-run: forms filled, nothing submitted. Re-run with -Submit to send." -ForegroundColor Green
    applypilot apply --workers $ApplyWorkers --dry-run
}
Pop-Location

Write-Host "`n=== done ===" -ForegroundColor Cyan
Write-Host "Review:  career-ops\reports\  +  applypilot dashboard"
Write-Host "Sync:    git add -A ; git commit -m 'pipeline run' ; git push`n"
