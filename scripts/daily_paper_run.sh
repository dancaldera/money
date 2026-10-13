#!/bin/bash
# Daily paper-trading run. Invoked by the Hermes cron job "money · daily paper
# run" (and by the 22:00 catch-up guard on a day that never scanned).
#
# SLOT MATTERS: this must run ~18:35 Mexico City time (00:35 UTC), just after the
# 00:00 UTC crypto daily close. At 17:00 CST (23:00 UTC) the newest *closed*
# crypto bar is one full UTC day old, so a crypto entry executes ~23h after its
# signal: measured over 8 cryptos / 4.7y that pays 0.06% of drift per cross and
# expires 18% of buy intents (~10/year) against the frozen 3% gap cap — trades the
# desk never takes, and a fresh cross is required to re-enter, so the move is
# missed outright. Reproduce: `python scripts/analysis_execution_gaps.py`.
#
# Change STRATEGY here to switch what the schedule trades.
# sma_cross chosen over rsi_meanrev on out-of-sample evidence (`money validate`):
# higher held-out alpha and the only both-windows winner. Not a proven edge —
# both strategies are regime-dependent — just the better-supported of the two.
#
# A TRANSIENT transport failure is retried in place (run_step in _lib.sh). This
# slot runs once a day and `paper-scan` only ever evaluates the newest closed
# bar, so a DNS blip here costs a permanent signal loss unless something else
# picks the bar up: on 2026-09-29 a full DNS outage (Alpaca and the CBOE VIX both
# unresolvable) killed this run and only the 22:45 catch-up saved the bar. The
# retry makes the slot survive a blip of seconds-to-minutes on its own; the
# 22:00 catch-up and the hourly coverage salvage stay as the lines behind it. A
# step that already submitted an order is never retried, and a deterministic
# failure (BrokerError, frozen-manifest mismatch) ends at once. The script exits
# with the CLI's rc, so the catch-up guard (`rc -ne 0`) and the Hermes job status
# can both see a failure instead of reading a silent 0.
set -u
STRATEGY="sma_cross"
# Live run: run3 (config/run3.yaml) — the breadth deployment opened 2026-09-19.
# Env-overridable so other-machine timers and tests can point elsewhere.
RUN_ID="${RUN_ID:-run3}"
RUN_CONFIG="${RUN_CONFIG:-config/run3.yaml}"

# Repo root = parent of this script's directory.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

# Test seams (opt-in, same convention as PAPERSCAN_HEARTBEAT in
# cron_catchup_daily.sh): point the CLI, the python probe, the log and the
# heartbeats at stubs/temp files to exercise the retry contract offline without
# touching live state.
MONEY="${MONEY_BIN:-$REPO_DIR/.venv/bin/money}"
PY="${MONEY_PY:-$REPO_DIR/.venv/bin/python}"
LOG="${SCAN_LOG:-$REPO_DIR/results/paper_scan.log}"
SCAN_HB="${PAPERSCAN_HEARTBEAT:-$REPO_DIR/results/.last_success_paperscan}"
SCAN_PREVIEW_HB="${PAPERSCAN_PREVIEW_HEARTBEAT:-$REPO_DIR/results/.last_preview_paperscan}"
CONTEXT_HB="${CONTEXT_HEARTBEAT:-$REPO_DIR/results/.last_success_context}"
SCAN_MAX_ATTEMPTS="${SCAN_MAX_ATTEMPTS:-3}"
SCAN_RETRY_SLEEP="${SCAN_RETRY_SLEEP:-45}"
# execute-intents prints Python dicts ({"action": "submitted"}), paper-stops
# prints `action=stopped`; a step whose output shows it acted must not re-run.
SUBMITTED_RE="${SUBMITTED_RE:-('action': 'submitted'|action=submitted)}"

rotate_log "$LOG"

# Set DRY_RUN=1 to evaluate signals without placing orders (for testing).
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

rc=0
{
  echo "===================================================================="
  echo "Daily paper run: $(date)"
  echo "strategy=$STRATEGY  repo=$REPO_DIR  run_id=$RUN_ID  dry_run=${DRY_RUN:-0}"

  # Capture point-in-time research inputs first. Context is shadow-only, so a
  # provider/model failure is loud but cannot prevent the frozen baseline scan.
  # FinBERT news needs the optional [research] extra (torch/transformers, ~3 GB);
  # without it, degrade to regime-only context instead of warning every day.
  CONTEXT_ARGS=""
  "$PY" -c "import transformers" >/dev/null 2>&1 || CONTEXT_ARGS="--skip-news"
  run_step "$SCAN_MAX_ATTEMPTS" "$SCAN_RETRY_SLEEP" "" \
    "$MONEY" collect-context --run-id "$RUN_ID" --run-config "$RUN_CONFIG" $CONTEXT_ARGS
  CONTEXT_OUT="$STEP_OUT"
  context_rc="$STEP_RC"
  context_attempts="$STEP_ATTEMPTS"
  if [ "$context_rc" -ne 0 ]; then
    CONTEXT_OUT="$(printf 'CONTEXT WARNING (exit %s):\n%s' "$context_rc" "$CONTEXT_OUT")"
  elif [ "${DRY_RUN:-0}" != "1" ]; then
    heartbeat "$CONTEXT_HB"
  fi

  run_step "$SCAN_MAX_ATTEMPTS" "$SCAN_RETRY_SLEEP" "" \
    "$MONEY" paper-scan --run-id "$RUN_ID" --run-config "$RUN_CONFIG" --strategy "$STRATEGY" $EXTRA
  SCAN_OUT="$STEP_OUT"
  scan_rc="$STEP_RC"
  scan_attempts="$STEP_ATTEMPTS"

  run_step "$SCAN_MAX_ATTEMPTS" "$SCAN_RETRY_SLEEP" "$SUBMITTED_RE" \
    "$MONEY" execute-intents --run-id "$RUN_ID" --run-config "$RUN_CONFIG" --asset crypto $EXTRA
  EXEC_OUT="$STEP_OUT"
  exec_rc="$STEP_RC"
  exec_attempts="$STEP_ATTEMPTS"

  if [ "${DRY_RUN:-0}" = "1" ]; then
    RECON_OUT="dry run — reconciliation skipped"
    reconcile_rc=0
    reconcile_attempts=1
  else
    run_step "$SCAN_MAX_ATTEMPTS" "$SCAN_RETRY_SLEEP" "" \
      "$MONEY" reconcile --run-id "$RUN_ID" --run-config "$RUN_CONFIG"
    RECON_OUT="$STEP_OUT"
    reconcile_rc="$STEP_RC"
    reconcile_attempts="$STEP_ATTEMPTS"
  fi

  OUT="$(printf '%s\n\n%s\n\n%s\n\n%s' "$CONTEXT_OUT" "$SCAN_OUT" "$EXEC_OUT" "$RECON_OUT")"
  rc="$scan_rc"
  [ "$exec_rc" -ne 0 ] && rc="$exec_rc"
  [ "$reconcile_rc" -ne 0 ] && rc="$reconcile_rc"
  if [ "$context_attempts" -gt 1 ] || [ "$scan_attempts" -gt 1 ] \
    || [ "$exec_attempts" -gt 1 ] || [ "$reconcile_attempts" -gt 1 ]; then
    echo "Transient retries used — context:$context_attempts scan:$scan_attempts execute:$exec_attempts reconcile:$reconcile_attempts"
  fi
  echo "$OUT"
  echo "Exit code: $rc"

  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Daily paper-scan FAILED (exit $rc) — check results/paper_scan.log"
    # Immediate alert email with the failure detail (best-effort; never fails the run).
    printf '%s\n' "$OUT" | "$MONEY" email-report \
      --alert-title "daily paper-scan FAILED (exit $rc)" || true
  else
    record_scan_success "${DRY_RUN:-0}" "$SCAN_HB" "$SCAN_PREVIEW_HB"
    # execute-intents prints Python dicts ({"action": "submitted"}), while
    # paper-stops prints `action=stopped`; match either form so the notify
    # never silently dies on a CLI output change.
    submitted="$(printf '%s\n' "$EXEC_OUT" | grep -cE "$SUBMITTED_RE" || true)"
    [ "${submitted:-0}" != "0" ] && notify "money lab" "Crypto execution submitted $submitted order(s)"
    # Full email digest: account, positions, today's signals, backtest alpha,
    # system health. Real runs only; preview anytime with:
    #   money email-report --dry-run
    if [ "${DRY_RUN:-0}" != "1" ]; then
      "$MONEY" email-report || true
    fi
  fi
} >> "$LOG" 2>&1
exit "$rc"
