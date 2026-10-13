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
#
# THIS SLOT HAS NO SECOND LINE: nothing else executes stock intents (the 22:00
# catch-up runs the daily wrapper, which executes crypto only), so a tick that
# dies on a DNS blip or a reset connection leaves the day's stock intents pending
# until the next morning at the earliest — and a stale intent is what the frozen
# 2% gap cap expires. The step is therefore retried in place on a TRANSIENT
# transport failure (run_step in _lib.sh), but never once an order was submitted
# (a re-run would only re-post the same client_order_id). The script exits with
# the CLI's rc so cron_silent_stockexec.sh's `rc -ne 0` alert branch can actually
# fire — it was dead code while this wrapper always exited 0, the same defect the
# stop monitor carried until 2026-10-08.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

RUN_ID="${RUN_ID:-run3}"
RUN_CONFIG="${RUN_CONFIG:-config/run3.yaml}"

# Test seams (opt-in, like MONEY_BIN/STOP_LOG in intraday_stop_run.sh).
MONEY="${MONEY_BIN:-$REPO_DIR/.venv/bin/money}"
LOG="${STOCK_LOG:-$REPO_DIR/results/stock_execution.log}"
HEARTBEAT_FILE="${STOCKEXEC_HEARTBEAT:-$REPO_DIR/results/.last_success_stockexecution}"
STOCK_MAX_ATTEMPTS="${STOCK_MAX_ATTEMPTS:-3}"
STOCK_RETRY_SLEEP="${STOCK_RETRY_SLEEP:-45}"
SUBMITTED_RE="${SUBMITTED_RE:-('action': 'submitted'|action=submitted)}"

rotate_log "$LOG"
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

rc=0
{
  echo "Stock intent execution: $(date)  dry_run=${DRY_RUN:-0}"
  run_step "$STOCK_MAX_ATTEMPTS" "$STOCK_RETRY_SLEEP" "$SUBMITTED_RE" \
    "$MONEY" execute-intents --run-id "$RUN_ID" --run-config "$RUN_CONFIG" --asset stock $EXTRA
  OUT="$STEP_OUT"
  rc="$STEP_RC"
  [ "$STEP_ATTEMPTS" -gt 1 ] && echo "Stock execution attempts used: $STEP_ATTEMPTS"
  echo "$OUT"
  echo "Exit code: $rc"
  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Stock intent execution FAILED (exit $rc)"
    printf '%s\n' "$OUT" | "$MONEY" email-report \
      --alert-title "stock intent execution FAILED (exit $rc)" || true
  elif [ "${DRY_RUN:-0}" != "1" ]; then
    # Booking only — a fill that reconcile misses here is picked up by the next
    # reconcile (the stop tick and the daily run both run one), so it stays
    # best-effort, but a transient failure is still retried in place.
    run_step "$STOCK_MAX_ATTEMPTS" "$STOCK_RETRY_SLEEP" "" \
      "$MONEY" reconcile --run-id "$RUN_ID" --run-config "$RUN_CONFIG" || true
    heartbeat "$HEARTBEAT_FILE"
  fi
} >> "$LOG" 2>&1
exit "$rc"
