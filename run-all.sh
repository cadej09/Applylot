#!/usr/bin/env bash
# run-all.sh — job-ops end-to-end pipeline (Mac/Linux)
#
#   ./run-all.sh                 # discover → bridge → evaluate/tailor → apply DRY-RUN
#   ./run-all.sh --submit        # same, but applications are actually submitted
#   ./run-all.sh --skip-discover --min-score 8 --limit 5
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

MIN_SCORE=7; LIMIT=10; SUBMIT=0; SKIP_DISCOVER=0; SKIP_EVALUATE=0; WORKERS=1
while [[ $# -gt 0 ]]; do case "$1" in
  --min-score) MIN_SCORE="$2"; shift 2;;
  --limit) LIMIT="$2"; shift 2;;
  --submit) SUBMIT=1; shift;;
  --skip-discover) SKIP_DISCOVER=1; shift;;
  --skip-evaluate) SKIP_EVALUATE=1; shift;;
  --workers) WORKERS="$2"; shift 2;;
  *) echo "unknown flag: $1"; exit 1;;
esac; done

echo "=== job-ops pipeline ==="

if [[ $SKIP_DISCOVER -eq 0 ]]; then
  echo "[1/4] ApplyPilot: discover + enrich + pre-score..."
  (cd "$ROOT/applypilot" && applypilot run)
else echo "[1/4] Discovery skipped."; fi

echo "[2/4] Bridge: ApplyPilot → career-ops pipeline inbox..."
(cd "$ROOT/career-ops" && node applypilot-bridge.mjs --min-score "$MIN_SCORE" --limit "$LIMIT")

if [[ $SKIP_EVALUATE -eq 0 ]]; then
  echo "[3/4] career-ops: evaluate, tailor, hand off (headless Claude)..."
  PROMPT='Process every pending URL in data/pipeline.md following modes/pipeline.md (batch mode).
For each job scoring >= 4.0/5: generate the tailored CV PDF, then run
  node applypilot-handoff.mjs --url <job-url> --pdf <generated-pdf> --txt <tailored-cv-text-file>
so ApplyPilot applies with the tailored materials. Below 4.0/5: mark Evaluated, no handoff.
Finish with node merge-tracker.mjs.'
  # --dangerously-skip-permissions is required for unattended runs.
  (cd "$ROOT/career-ops" && claude -p "$PROMPT" --dangerously-skip-permissions)
else echo "[3/4] Evaluation skipped."; fi

echo "[4/4] ApplyPilot: auto-apply..."
if [[ $SUBMIT -eq 1 ]]; then
  echo "SUBMIT MODE — applications WILL be sent."
  (cd "$ROOT/applypilot" && applypilot apply --workers "$WORKERS")
else
  echo "Dry-run: forms filled, nothing submitted. Re-run with --submit to send."
  (cd "$ROOT/applypilot" && applypilot apply --workers "$WORKERS" --dry-run)
fi

echo "=== done ==="
echo "Review:  career-ops/reports/  +  applypilot dashboard"
echo "Sync:    git add -A && git commit -m 'pipeline run' && git push"
