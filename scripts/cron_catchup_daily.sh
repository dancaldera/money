#!/bin/bash
# Hermes-cron catch-up guard for the daily paper scan (paper desk, fake money).
#
# Why: the 17:00 "daily paper run" job is an AGENT job, so it can die before it
# ever reaches the wrapper (model outage, credit exhaustion, inactivity
# timeout). That silently costs a whole day of frozen SMA signals, and the loss
# is permanent: `paper-scan` only evaluates the LATEST closed bar, so a fresh
# cross that lands on the missed bar_end is never seen again — a skipped entry
# that no later run can recover. On 2026-09-10 the 17:00 run died in the model
# API before executing the wrapper and the day's scan never happened.
#
# This guard runs later the same evening and executes the wrapper only when
# today's success heartbeat is missing, so it is idempotent: after any
# successful scan (agent job or catch-up) it stays silent.
#
# Designed for a no_agent Hermes cronjob — empty stdout means "send nothing".
# Test hooks: PAPERSCAN_HEARTBEAT overrides the heartbeat path (tests point it
# at a temp file), CHECK_ONLY=1 only prints the decision, never runs the desk.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

HB="${PAPERSCAN_HEARTBEAT:-$REPO_DIR/results/.last_success_paperscan}"
CHECK_ONLY="${CHECK_ONLY:-0}"

if [ -f "$HB" ]; then
  hb_day="$(date -r "$HB" +%F 2>/dev/null || echo unknown)"
else
  hb_day="missing"
fi
today="$(date +%F)"

if [ "$hb_day" = "$today" ]; then
  [ "$CHECK_ONLY" = "1" ] && echo "catch-up: skip (scan already succeeded today, $hb_day)"
  exit 0
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo "catch-up: WOULD RUN (heartbeat: $hb_day, today: $today)"
  exit 0
fi

printf '⏰ money · daily scan missed today (heartbeat: %s) — running catch-up\n' "$hb_day"
OUT="$(bash "$REPO_DIR/scripts/daily_paper_run.sh" 2>&1)"
rc=$?
TAIL="$(printf '%s\n' "$OUT" | tail -20)"

if [ "$rc" -ne 0 ]; then
  printf '⚠️ money · catch-up FAILED (exit %s)\n%s\n' "$rc" "$TAIL"
  notify "money lab" "Daily scan catch-up FAILED (exit $rc)"
  exit 0
fi

# Confirm from the heartbeat side: a wrapper exit 0 that did not advance the
# heartbeat means the scan never recorded (the desk would stay silent on a real
# failure) — so say so loudly instead.
after_day="$(date -r "$HB" +%F 2>/dev/null || echo unknown)"
if [ "$after_day" = "$today" ]; then
  printf '✅ money · catch-up OK — daily scan recorded for %s\n' "$after_day"
else
  printf '⚠️ money · catch-up exited 0 but today is still unscanned (heartbeat %s) — check results/paper_scan.log\n' "$after_day"
fi
exit 0
