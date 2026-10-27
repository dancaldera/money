#!/bin/bash
# Read-only morning dump for the Hermes 09:00 buy/hold/sell briefing.
# Never places orders, never writes the ledger, never touches the live manifest.
#
# Hermes cron injects this stdout into the agent prompt. Preview:
#   bash scripts/morning_brief.sh
set -u
STRATEGY="${STRATEGY:-sma_cross}"
RUN_ID="${RUN_ID:-run3}"
RUN_CONFIG="${RUN_CONFIG:-config/run3.yaml}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

MONEY="${MONEY_BIN:-$REPO_DIR/.venv/bin/money}"
PY="${MONEY_PY:-$REPO_DIR/.venv/bin/python}"
LOG="${BRIEF_LOG:-$REPO_DIR/results/morning_brief.log}"
rotate_log "$LOG"

run_section() {
  local title="$1"
  shift
  echo
  echo "===== $title ====="
  if "$@"; then
    :
  else
    echo "[FAILED exit $?] $*"
  fi
}

{
  echo "MONEY MORNING BRIEF $(date)"
  echo "repo=$REPO_DIR  run_id=$RUN_ID  strategy=$STRATEGY"
  echo "mode=read-only (no orders, no ledger writes)"

  run_section "HEALTH (ledger, no broker)" \
    "$MONEY" health --run-id "$RUN_ID" --run-config "$RUN_CONFIG"

  run_section "PAPER STATUS (Alpaca paper, live)" \
    "$MONEY" paper-status

  # The realized record and the capital lens: what the book actually earned by
  # leg and how the live sample sits against the replay it was chosen from.
  # Both read the ledger only (no broker, no network, no writes).
  run_section "RUN REPORT (ledger, no broker)" \
    "$MONEY" run-report --run-id "$RUN_ID" --run-config "$RUN_CONFIG"

  run_section "LIVE DEPLOYMENT (slots, marked P&L, live-vs-replay parity)" \
    "$PY" "$REPO_DIR/scripts/analysis_live_deployment.py" \
      --ledger "$REPO_DIR/results/$RUN_ID/ledger.sqlite" --run-config "$RUN_CONFIG"

  run_section "STOPS DRY-RUN (8% fill-derived, close nothing)" \
    "$MONEY" paper-stops --run-id "$RUN_ID" --run-config "$RUN_CONFIG" --dry-run

  run_section "SMA SCAN DRY-RUN ($RUN_ID baseline+shadow, record nothing)" \
    "$MONEY" paper-scan --run-id "$RUN_ID" --run-config "$RUN_CONFIG" --strategy "$STRATEGY" --dry-run

  echo
  echo "===== END ====="
} 2>&1 | tee -a "$LOG"