#!/bin/bash
# Intraday stop-loss monitor. Invoked frequently by launchd (see
# scripts/com.money.stopmonitor.plist). Reads each open paper position's live
# P&L and closes any that have fallen through the stop-loss threshold, so a
# losing position is cut intraday instead of waiting for the once-a-day
# signal scan (scripts/daily_paper_run.sh).
set -u

# Repo root = parent of this script's directory.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

LOG="$REPO_DIR/results/stop_monitor.log"
rotate_log "$LOG"

# Set DRY_RUN=1 to report breaches without closing anything (for testing).
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

{
  echo "--------------------------------------------------------------------"
  echo "Stop monitor: $(date)  dry_run=${DRY_RUN:-0}"

  # The CLI loads .env from the working directory, so keys are picked up here.
  OUT="$("$REPO_DIR/.venv/bin/money" paper-stops $EXTRA 2>&1)"
  rc=$?
  echo "$OUT"
  echo "Exit code: $rc"

  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Stop monitor FAILED (exit $rc) — check results/stop_monitor.log"
  else
    heartbeat "$REPO_DIR/results/.last_success_stopmonitor"
    stopped="$(printf '%s\n' "$OUT" | grep -oE '^[0-9]+ position' | grep -oE '^[0-9]+' | head -1)"
    if [ "${stopped:-0}" != "0" ] && [ "${DRY_RUN:-0}" != "1" ]; then
      notify "money lab" "Stop-loss closed $stopped position(s)"
    fi
  fi
} >> "$LOG" 2>&1
