#!/bin/bash
# Execute frozen stock intents just after the regular US session opens. Signals
# were captured after the prior close; the 2% marketable-limit guard prevents an
# adverse opening gap from being chased.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

LOG="$REPO_DIR/results/stock_execution.log"
rotate_log "$LOG"
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

{
  echo "Stock intent execution: $(date)  dry_run=${DRY_RUN:-0}"
  OUT="$("$REPO_DIR/.venv/bin/money" execute-intents --run-id run2 --asset stock $EXTRA 2>&1)"
  rc=$?
  echo "$OUT"
  echo "Exit code: $rc"
  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Stock intent execution FAILED (exit $rc)"
    printf '%s\n' "$OUT" | "$REPO_DIR/.venv/bin/money" email-report \
      --alert-title "stock intent execution FAILED (exit $rc)" || true
  elif [ "${DRY_RUN:-0}" != "1" ]; then
    "$REPO_DIR/.venv/bin/money" reconcile --run-id run2 || true
    heartbeat "$REPO_DIR/results/.last_success_stockexecution"
  fi
} >> "$LOG" 2>&1

