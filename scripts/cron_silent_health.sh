#!/bin/bash
# Hermes-cron watchdog (paper desk, fake money): silent when healthy,
# prints the full health report when anything looks stuck.
# Designed for a no_agent Hermes cronjob — empty stdout means "send nothing".
#
# Thresholds: daily scan 30h, stop monitor 6h (a sleeping laptop stalls both the
# scheduler and the loop, so the stop window is deliberately generous).
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(
  RUN_ID="${RUN_ID:-run3}" \
  STOP_MAX_AGE_S="${STOP_MAX_AGE_S:-21600}" \
  PAPER_MAX_AGE_S="${PAPER_MAX_AGE_S:-108000}" \
  bash "$REPO_DIR/scripts/health_check.sh" 2>&1
)"
rc=$?

if [ "$rc" -ne 0 ]; then
  printf '🩺 money · health UNHEALTHY\n%s\n' "$OUT"
fi

# Salvage line: health_check.sh only *reports* a bar the ledger never evaluated
# (coverage behind=1); nothing re-ran it, and once the next bar closes the skip
# is permanent (paper-scan only ever evaluates the newest closed bar). The
# catch-up guard owns "run the desk when the day is unscanned", so call it here
# in coverage mode: it decides from the ledger's own coverage line, stays silent
# while coverage is current, refuses to race the 18:35 slot, and speaks only if
# it acts. Report-only watchdogs plus a single evening slot leave a hole where
# DNS is down at both the slot and the catch-up (2026-09-29: the 18:35 run died
# on a full DNS outage and the 22:45 catch-up salvaged it).
COVERAGE_ONLY=1 bash "$REPO_DIR/scripts/cron_catchup_daily.sh"
exit 0
