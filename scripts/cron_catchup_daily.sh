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
# Two modes, one guard:
#   * slot mode (default, the 22:00 job) — run the desk unless the last success
#     heartbeat is from AFTER today's slot time. A heartbeat from earlier TODAY
#     does not prove tonight's slot ran (an operator's manual run, a morning
#     scan) and treating it as proof would lose the day's bar forever, while the
#     re-run this allows is idempotent per bar (INSERT OR IGNORE +
#     UNIQUE(run_id,portfolio,symbol,bar_end,signal), and the client_order_id
#     derives from decision_id), so the guard can only ever cost a duplicate
#     no-op scan — never a lost bar.
#   * coverage mode (COVERAGE_ONLY=1, the hourly health watchdog) — the desk ran
#     and still failed, or DNS/network was down at BOTH slots: on 2026-09-29 the
#     18:35 run died on a full DNS outage (Alpaca and the CBOE VIX both
#     unresolvable), the 22:45 catch-up salvaged the bar, and nothing existed for
#     the case where that second run fails too. Here the trigger is the ledger's
#     own coverage line (`money health`: `coverage_<scope>: ... behind=1` = the
#     newest closed bar was never evaluated) instead of the clock, so it is safe
#     to run hourly: it stays silent and costs one read-only health call while
#     the ledger is current, and it acts within the ~24h window before the next
#     bar close makes the skip permanent. It refuses to race the slot inside the
#     bar-close window (18:00 CST bar close -> 18:35 slot), where the scheduled
#     job owns the run, and a stamp file bounds retries to one per cooldown.
#
# Designed for a no_agent Hermes cronjob — empty stdout means "send nothing".
# Test hooks: PAPERSCAN_HEARTBEAT overrides the heartbeat path, SLOT_HHMM
# overrides the slot time, PRE_SLOT_HHMM overrides (or blanks) the window the
# coverage mode must not race, HEALTH_SNAPSHOT reads a health report from a file
# instead of the CLI, SALVAGE_STAMP/SALVAGE_COOLDOWN_S bound retries,
# CHECK_ONLY=1 only prints the decision.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
source "$REPO_DIR/scripts/_lib.sh"

RUN_ID="${RUN_ID:-run3}"
RUN_CONFIG="${RUN_CONFIG:-config/run3.yaml}"
HB="${PAPERSCAN_HEARTBEAT:-$REPO_DIR/results/.last_success_paperscan}"
CHECK_ONLY="${CHECK_ONLY:-0}"
# Time of the daily run slot this guard backs up (local clock).
SLOT_HHMM="${SLOT_HHMM:-18:35}"
COVERAGE_ONLY="${COVERAGE_ONLY:-0}"
# The crypto bar closes at 00:00 UTC = 18:00 CST, so the scheduled slot owns the
# window from the bar close to the slot. Blank disables the check.
PRE_SLOT_HHMM="${PRE_SLOT_HHMM:-18:00}"
SALVAGE_STAMP="${SALVAGE_STAMP:-$REPO_DIR/results/.last_salvage_attempt}"
SALVAGE_COOLDOWN_S="${SALVAGE_COOLDOWN_S:-1800}"
HEALTH_SNAPSHOT="${HEALTH_SNAPSHOT:-}"
MONEY="$([ -x "$REPO_DIR/.venv/bin/money" ] && echo "$REPO_DIR/.venv/bin/money" || echo money)"

coverage_behind_scopes() {
  # Same contract as health_check.sh: one `coverage_<scope>:` line per asset
  # scope, `behind=1` meaning the ledger never evaluated the newest closed bar.
  local out
  if [ -n "$HEALTH_SNAPSHOT" ]; then
    out="$(cat "$HEALTH_SNAPSHOT" 2>/dev/null)"
  else
    out="$("$MONEY" health --run-id "$RUN_ID" --run-config "$RUN_CONFIG" 2>&1)"
  fi
  printf '%s\n' "$out" | sed -n 's/^ *coverage_\([a-z]*\): .*behind=1.*/\1/p' | tr '\n' ' ' | sed 's/ *$//'
}

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

reason=""
if [ "$COVERAGE_ONLY" = "1" ]; then
  now_hhmm="$(date +%H:%M)"
  if [ -n "$PRE_SLOT_HHMM" ] && [[ "$now_hhmm" > "$PRE_SLOT_HHMM" ]] && [[ "$now_hhmm" < "$SLOT_HHMM" ]]; then
    [ "$CHECK_ONLY" = "1" ] && echo "catch-up: skip (inside the $PRE_SLOT_HHMM-$SLOT_HHMM bar-close window — the scheduled slot owns it)"
    exit 0
  fi
  behind="$(coverage_behind_scopes)"
  if [ -z "$behind" ]; then
    [ "$CHECK_ONLY" = "1" ] && echo "catch-up: skip (coverage ok — the ledger evaluated the newest closed bar)"
    exit 0
  fi
  if [ -f "$SALVAGE_STAMP" ]; then
    last_attempt="$(date -r "$SALVAGE_STAMP" +%s 2>/dev/null || echo 0)"
    age=$(( $(date +%s) - last_attempt ))
    if [ "$age" -lt "$SALVAGE_COOLDOWN_S" ]; then
      [ "$CHECK_ONLY" = "1" ] && echo "catch-up: skip (a salvage attempt ran ${age}s ago, cooldown ${SALVAGE_COOLDOWN_S}s)"
      exit 0
    fi
  fi
  reason="scan coverage behind on ${behind} — the newest closed bar was never evaluated"
  headline="⏰ money · scan salvage: $reason — running the desk"
else
  if [ "$scanned_after_slot" = "1" ]; then
    [ "$CHECK_ONLY" = "1" ] && echo "catch-up: skip (scanned after the $SLOT_HHMM slot: $hb_stamp)"
    exit 0
  fi
  if [ "$CHECK_ONLY" = "1" ]; then
    echo "catch-up: WOULD RUN (last success: $hb_stamp, today's slot: $slot_stamp)"
    exit 0
  fi
  reason="missed the $SLOT_HHMM slot (last success: $hb_stamp)"
  headline="⏰ money · daily scan missed the $SLOT_HHMM slot (last success: $hb_stamp) — running catch-up"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo "catch-up: WOULD RUN ($reason)"
  exit 0
fi

printf '%s\n' "$headline"
OUT="$(bash "$REPO_DIR/scripts/daily_paper_run.sh" 2>&1)"
rc=$?
TAIL="$(printf '%s\n' "$OUT" | tail -20)"
# Always stamp the attempt: a failing run must not be retried every hour into a
# storm, the next hourly watchdog tick picks it up again after the cooldown.
mkdir -p "$(dirname "$SALVAGE_STAMP")" 2>/dev/null && touch "$SALVAGE_STAMP" 2>/dev/null

if [ "$rc" -ne 0 ]; then
  printf '⚠️ money · catch-up FAILED (exit %s)\n%s\n' "$rc" "$TAIL"
  notify "money lab" "Daily scan catch-up FAILED (exit $rc)"
  exit 0
fi

after_stamp="$(date -r "$HB" '+%F %H:%M' 2>/dev/null || echo unknown)"
if [ "$after_stamp" != "unknown" ] && { [ "$after_stamp" = "$slot_stamp" ] || [[ "$after_stamp" > "$slot_stamp" ]]; }; then
  printf '✅ money · catch-up OK — daily scan recorded for %s\n' "$after_stamp"
else
  printf '⚠️ money · catch-up exited 0 but tonight is still unscanned (heartbeat %s) — check results/paper_scan.log\n' "$after_stamp"
fi
exit 0
