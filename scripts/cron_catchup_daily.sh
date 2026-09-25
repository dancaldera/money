#!/bin/bash
# Hermes-cron catch-up guard for the daily paper scan (paper desk, fake money).
#
# Why: the day's scan is only recoverable inside the same evening. `paper-scan`
# evaluates the LATEST closed bar, so a fresh cross that lands on a bar the desk
# never scanned is never seen again — a skipped entry no later run can recover.
# The 18:35 "daily paper run" slot (00:35 UTC, just after the crypto daily
# close) is the one that must scan: at 22:00 CST the same bar is still the
# newest closed one, only the entry price drifts (~3.4h).
#
# The slot has failed for two reasons in practice: it was an AGENT job that died
# on the 600s inactivity watchdog (2026-09-22 and 2026-09-24: nothing traded and
# the catch-up became the day's only scan), and it was an agent job that died in
# the model API before executing the wrapper at all (2026-09-10). It is now a
# no_agent script job; this guard stays as the second line of defence.
#
# Rule: run the desk unless the last success heartbeat is from AFTER today's
# slot time. A heartbeat from earlier TODAY does not prove tonight's slot ran
# (an operator's manual run, a morning scan) and treating it as proof would lose
# the day's bar forever, while the re-run this allows is idempotent per bar
# (INSERT OR IGNORE + UNIQUE(run_id,portfolio,symbol,bar_end,signal), and the
# client_order_id derives from decision_id), so the guard can only ever cost a
# duplicate no-op scan — never a lost bar.
#
# Designed for a no_agent Hermes cronjob — empty stdout means "send nothing".
# Test hooks: PAPERSCAN_HEARTBEAT overrides the heartbeat path, SLOT_HHMM
# overrides the slot time, CHECK_ONLY=1 only prints the decision.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

HB="${PAPERSCAN_HEARTBEAT:-$REPO_DIR/results/.last_success_paperscan}"
CHECK_ONLY="${CHECK_ONLY:-0}"
# Time of the daily run slot this guard backs up (local clock).
SLOT_HHMM="${SLOT_HHMM:-18:35}"

today="$(date +%F)"
slot_stamp="$today $SLOT_HHMM"
if [ -f "$HB" ]; then
  hb_stamp="$(date -r "$HB" '+%F %H:%M' 2>/dev/null || echo unknown)"
else
  hb_stamp="missing"
fi

scanned_after_slot=0
if [ "$hb_stamp" != "missing" ] && [ "$hb_stamp" != "unknown" ]; then
  # `>` inside [[ ]] compares as strings; the "YYYY-MM-DD HH:MM" shape sorts
  # chronologically, which is why no date arithmetic is needed here (macOS and
  # Linux `date` disagree on the flags, so this stays portable).
  if [ "$hb_stamp" = "$slot_stamp" ] || [[ "$hb_stamp" > "$slot_stamp" ]]; then
    scanned_after_slot=1
  fi
fi

if [ "$scanned_after_slot" = "1" ]; then
  [ "$CHECK_ONLY" = "1" ] && echo "catch-up: skip (scanned after the $SLOT_HHMM slot: $hb_stamp)"
  exit 0
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo "catch-up: WOULD RUN (last success: $hb_stamp, today's slot: $slot_stamp)"
  exit 0
fi

printf '⏰ money · daily scan missed the %s slot (last success: %s) — running catch-up\n' "$SLOT_HHMM" "$hb_stamp"
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
after_stamp="$(date -r "$HB" '+%F %H:%M' 2>/dev/null || echo unknown)"
if [ "$after_stamp" != "unknown" ] && { [ "$after_stamp" = "$slot_stamp" ] || [[ "$after_stamp" > "$slot_stamp" ]]; }; then
  printf '✅ money · catch-up OK — daily scan recorded for %s\n' "$after_stamp"
else
  printf '⚠️ money · catch-up exited 0 but tonight is still unscanned (heartbeat %s) — check results/paper_scan.log\n' "$after_stamp"
fi
exit 0
