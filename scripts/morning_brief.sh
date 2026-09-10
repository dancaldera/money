#!/bin/bash
# Read-only morning dump for the Hermes 09:00 buy/hold/sell briefing.
# Never places orders, never writes the ledger, never touches run2.yaml.
#
# Hermes cron injects this stdout into the agent prompt. Preview:
#   bash scripts/morning_brief.sh
set -u
STRATEGY="${STRATEGY:-sma_cross}"
RUN_ID="${RUN_ID:-run2}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

MONEY="$REPO_DIR/.venv/bin/money"
LOG="$REPO_DIR/results/morning_brief.log"
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
    "$MONEY" health --run-id "$RUN_ID"

  run_section "PAPER STATUS (Alpaca paper, live)" \
    "$MONEY" paper-status

  run_section "STOPS DRY-RUN (8% fill-derived, close nothing)" \
    "$MONEY" paper-stops --run-id "$RUN_ID" --dry-run

  run_section "SMA SCAN DRY-RUN (run2 baseline+shadow, record nothing)" \
    "$MONEY" paper-scan --run-id "$RUN_ID" --strategy "$STRATEGY" --dry-run

  echo
  echo "===== END ====="
} 2>&1 | tee -a "$LOG"