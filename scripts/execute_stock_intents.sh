#!/bin/bash
# Execute frozen stock intents for signals captured after the prior close; the 2%
# marketable-limit guard prevents an adverse gap from being chased.
#
# Timing on this Mac (Hermes cron; local clock = UTC-6, so ET = local + 2h): the
# slot is 08:31 local, so fills land ~10:31 ET — about an hour after the 09:30 ET
# open — plus cron jitter (observed 10:31-11:11 ET on 2026-09-10..16). The
# launchd/systemd timers in scripts/ do fire at 09:31 America/New_York on other
# machines. Entering ~+60min vs the official open measured drift-neutral
# (-0.035% mean / 0.000% median over 199 stock signals, 30-min bars, 2026-09), so
# the delay is not a reason to move this slot. Keep this comment in step with it.
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

