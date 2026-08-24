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
RUN_ID="run2"

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

  # Capture point-in-time research inputs first. Context is shadow-only, so a
  # provider/model failure is loud but cannot prevent the frozen baseline scan.
  CONTEXT_OUT="$("$REPO_DIR/.venv/bin/money" collect-context --run-id "$RUN_ID" 2>&1)"
  context_rc=$?
  if [ "$context_rc" -ne 0 ]; then
    CONTEXT_OUT="$(printf 'CONTEXT WARNING (exit %s):\n%s' "$context_rc" "$CONTEXT_OUT")"
  elif [ "${DRY_RUN:-0}" != "1" ]; then
    heartbeat "$REPO_DIR/results/.last_success_context"
  fi

  SCAN_OUT="$("$REPO_DIR/.venv/bin/money" paper-scan --run-id "$RUN_ID" --strategy "$STRATEGY" $EXTRA 2>&1)"
  scan_rc=$?
  EXEC_OUT="$("$REPO_DIR/.venv/bin/money" execute-intents --run-id "$RUN_ID" --asset crypto $EXTRA 2>&1)"
  exec_rc=$?
  if [ "${DRY_RUN:-0}" = "1" ]; then
    RECON_OUT="dry run — reconciliation skipped"
    reconcile_rc=0
  else
    RECON_OUT="$("$REPO_DIR/.venv/bin/money" reconcile --run-id "$RUN_ID" 2>&1)"
    reconcile_rc=$?
  fi
  OUT="$(printf '%s\n\n%s\n\n%s\n\n%s' "$CONTEXT_OUT" "$SCAN_OUT" "$EXEC_OUT" "$RECON_OUT")"
  rc=$scan_rc
  [ "$exec_rc" -ne 0 ] && rc=$exec_rc
  [ "$reconcile_rc" -ne 0 ] && rc=$reconcile_rc
  echo "$OUT"
  echo "Exit code: $rc"

  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Daily paper-scan FAILED (exit $rc) — check results/paper_scan.log"
    # Immediate alert email with the failure detail (best-effort; never fails the run).
    printf '%s\n' "$OUT" | "$REPO_DIR/.venv/bin/money" email-report \
      --alert-title "daily paper-scan FAILED (exit $rc)" || true
  else
    heartbeat "$REPO_DIR/results/.last_success_paperscan"
    submitted="$(printf '%s\n' "$EXEC_OUT" | grep -c "'action': 'submitted'" || true)"
    [ "${submitted:-0}" != "0" ] && notify "money lab" "Crypto execution submitted $submitted order(s)"
    # Full email digest: account, positions, today's signals, backtest alpha,
    # system health. Real runs only; preview anytime with:
    #   money email-report --dry-run
    if [ "${DRY_RUN:-0}" != "1" ]; then
      "$REPO_DIR/.venv/bin/money" email-report || true
    fi
  fi
} >> "$LOG" 2>&1
