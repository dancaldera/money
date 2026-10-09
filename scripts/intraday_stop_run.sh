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

RUN_ID="${RUN_ID:-run3}"
RUN_CONFIG="${RUN_CONFIG:-config/run3.yaml}"
# Test seams, same convention as PAPERSCAN_HEARTBEAT / SALVAGE_STAMP elsewhere:
# point MONEY_BIN, STOP_LOG and STOP_HEARTBEAT at stubs to exercise this offline.
MONEY="${MONEY_BIN:-$REPO_DIR/.venv/bin/money}"

LOG="${STOP_LOG:-$REPO_DIR/results/stop_monitor.log}"
HEARTBEAT_FILE="${STOP_HEARTBEAT:-$REPO_DIR/results/.last_success_stopmonitor}"
rotate_log "$LOG"

# Bounded retry for TRANSIENT transport failures only. A DNS blip or a reset
# connection fails one 30-min tick and leaves every open position unenforced
# until the next one — measured 2026-10-08 03:48 CST (ConnectionResetError(54))
# and 07:40 CST (NameResolutionError), ~54 min of unprotected book while BTC was
# already -5%. paper-stops only reads state and closes breached positions, and it
# holds back a symbol whose stop order is already submitted-and-unreconciled, so
# a retry cannot double-submit; the loop still stops the moment any action was
# taken, as a second line. Failures a retry cannot fix (BrokerError,
# RunSafetyError, a frozen-manifest mismatch) are not in the pattern and end the
# loop at once.
#
# The bounded HTTP timeout (trading/net.py, 5s connect / 20s read) turns a hang
# — measured 2026-10-07 04:47 CST: a broker call blocked past the job's 3600s
# watchdog and the next tick only ran 6h later, ~12 ticks unenforced — into one
# of these transient messages, so "timed out" MUST stay in the pattern.
STOP_MAX_ATTEMPTS="${STOP_MAX_ATTEMPTS:-3}"
STOP_RETRY_SLEEP="${STOP_RETRY_SLEEP:-45}"
TRANSIENT_RE='ConnectionError|NameResolutionError|ConnectionResetError|Max retries exceeded|Connection reset by peer|timed out|Temporary failure in name resolution'

# Set DRY_RUN=1 to report breaches without closing anything (for testing).
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

# The CLI's exit code must reach the caller. It used to be swallowed (the failure
# branch below ends in `|| true` and an `if` returns 0), so
# scripts/cron_silent_stop_run.sh's ``rc -ne 0`` branch was dead code and a
# failed stop check was invisible everywhere the operator looks: the desktop
# notification is the only other alert and the email path is unconfigured
# (no EMAIL_* keys in .env, "Email reports not configured — alert not sent").
# Measured 2026-10-08 07:40 CST: a NameResolutionError left the book unprotected
# and the Hermes job still reported ok. A non-zero exit now surfaces it.
rc=0

{
  echo "--------------------------------------------------------------------"
  echo "Stop monitor: $(date)  dry_run=${DRY_RUN:-0}"

  # The CLI loads .env from the working directory, so keys are picked up here.
  attempt=1
  while :; do
    OUT="$("$MONEY" paper-stops --run-id "$RUN_ID" --run-config "$RUN_CONFIG" $EXTRA 2>&1)"
    rc=$?
    [ "$rc" -eq 0 ] && break
    if printf '%s\n' "$OUT" | grep -qE 'action=(stopped|would_stop)'; then
      echo "attempt $attempt failed after acting on a position — not retrying"
      break
    fi
    if [ "$attempt" -ge "$STOP_MAX_ATTEMPTS" ] || ! printf '%s\n' "$OUT" | grep -qE "$TRANSIENT_RE"; then
      break
    fi
    echo "transient broker failure on attempt $attempt/$STOP_MAX_ATTEMPTS — retrying in ${STOP_RETRY_SLEEP}s"
    attempt=$((attempt + 1))
    sleep "$STOP_RETRY_SLEEP"
  done
  if [ "$attempt" -gt 1 ]; then
    echo "Stop monitor attempts used: $attempt"
  fi
  echo "$OUT"
  echo "Exit code: $rc"

  if [ "$rc" -ne 0 ]; then
    notify "money lab" "Stop monitor FAILED (exit $rc) — check results/stop_monitor.log"
    # Immediate alert email with the failure detail (best-effort; never fails the run).
    printf '%s\n' "$OUT" | "$MONEY" email-report \
      --alert-title "stop monitor FAILED (exit $rc)" || true
  else
    if [ "${DRY_RUN:-0}" = "1" ]; then
      # A preview cut nothing, so it must not look like a real tick: the hourly
      # watchdog reads this heartbeat, and a preview advancing it would hide a
      # genuinely stale stop monitor (same rule as record_scan_success for the
      # scan heartbeat).
      heartbeat "${STOP_PREVIEW_HEARTBEAT:-$REPO_DIR/results/.last_preview_stopmonitor}"
      echo "preview (DRY_RUN): success heartbeat NOT advanced ($HEARTBEAT_FILE)"
    else
      heartbeat "$HEARTBEAT_FILE"
    fi
    stopped="$(printf '%s\n' "$OUT" | grep -c 'action=stopped' || true)"
    if [ "${stopped:-0}" != "0" ] && [ "${DRY_RUN:-0}" != "1" ]; then
      notify "money lab" "Stop-loss closed $stopped position(s)"
      # Immediate alert email so the stop is visible in the inbox, not just in
      # this log (best-effort).
      printf '%s\n' "$OUT" | "$MONEY" email-report \
        --alert-title "stop-loss closed $stopped position(s)" || true
    fi
  fi
} >> "$LOG" 2>&1
exit "$rc"
