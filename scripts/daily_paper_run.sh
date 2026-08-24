#!/bin/bash
# Daily paper-trading run. Invoked by launchd (see scripts/com.money.paperscan.plist).
# Runs the chosen strategy across the watchlist on the Alpaca PAPER account.
# The launchd schedule is 18:05 Mexico City time, just after the 00:00 UTC
# crypto daily close, so the latest closed crypto candle is fresh.
#
# Change STRATEGY here to switch what the schedule trades.
# sma_cross chosen over rsi_meanrev on out-of-sample evidence (`money validate`):
# higher held-out alpha and the only both-windows winner. Not a proven edge —
# both strategies are regime-dependent — just the better-supported of the two.
set -u
STRATEGY="sma_cross"

# Repo root = parent of this script's directory.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

LOG="$REPO_DIR/results/paper_scan.log"
rotate_log "$LOG"

# Set DRY_RUN=1 to evaluate signals without placing orders (for testing).
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

{
  echo "===================================================================="
  echo "Daily paper run: $(date)"
  echo "strategy=$STRATEGY  repo=$REPO_DIR  dry_run=${DRY_RUN:-0}"

  # The CLI loads .env from the working directory, so keys are picked up here.
  OUT="$("$REPO_DIR/.venv/bin/money" paper-scan --strategy "$STRATEGY" $EXTRA 2>&1)"
  rc=$?
  echo "$OUT"
  echo "Exit code: $rc"

  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Daily paper-scan FAILED (exit $rc) — check results/paper_scan.log"
    # Immediate alert email with the failure detail (best-effort; never fails the run).
    printf '%s\n' "$OUT" | "$REPO_DIR/.venv/bin/money" email-report \
      --alert-title "daily paper-scan FAILED (exit $rc)" || true
  else
    heartbeat "$REPO_DIR/results/.last_success_paperscan"
    placed="$(printf '%s\n' "$OUT" | grep -oE '^[0-9]+ order' | grep -oE '^[0-9]+' | head -1)"
    if [ "${placed:-0}" != "0" ] && [ "${DRY_RUN:-0}" != "1" ]; then
      notify "money lab" "Daily scan placed $placed order(s)"
    fi
    # Full email digest: account, positions, today's signals, backtest alpha,
    # system health. Real runs only; preview anytime with:
    #   money email-report --dry-run
    if [ "${DRY_RUN:-0}" != "1" ]; then
      "$REPO_DIR/.venv/bin/money" email-report || true
    fi
  fi
} >> "$LOG" 2>&1
