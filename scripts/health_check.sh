#!/bin/bash
# money health check (read-only, no orders): heartbeat freshness + frozen-run halt state.
# Alerts through _lib.sh (osascript on macOS) and exits non-zero when something is stuck.
#
#   bash scripts/health_check.sh
#
# Thresholds are env-overridable: a sleeping laptop legitimately stalls the
# scheduler, so the stop monitor allows 6h (its interval is 30m), the daily
# scan 30h, and a pending buy intent 96h (PENDING_MAX_AGE_H). Force-test the
# alert path with: PAPER_MAX_AGE_S=1 bash scripts/health_check.sh
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1
# shellcheck source=scripts/_lib.sh
source "$REPO_DIR/scripts/_lib.sh"

RUN_ID="${RUN_ID:-run3}"
RUN_CONFIG="${RUN_CONFIG:-config/run3.yaml}"
PAPER_MAX_AGE_S="${PAPER_MAX_AGE_S:-108000}"  # 30h
STOP_MAX_AGE_S="${STOP_MAX_AGE_S:-21600}"     # 6h
LOG="$REPO_DIR/results/health.log"
rotate_log "$LOG"
MONEY="$( [ -x "$REPO_DIR/.venv/bin/money" ] && echo "$REPO_DIR/.venv/bin/money" || echo money )"

problems=""
add_problem() {
  if [ -n "$problems" ]; then
    problems="${problems}"$'\n'"- $1"
  else
    problems="- $1"
  fi
}

check_heartbeat() {
  local heartbeat="$1" max_age="$2" label="$3"
  if [ ! -f "$heartbeat" ]; then
    add_problem "$label: never succeeded (no heartbeat file)"
    return
  fi
  local age
  age=$(( $(date +%s) - $(cat "$heartbeat" 2>/dev/null || echo 0) ))
  if [ "$age" -gt "$max_age" ]; then
    add_problem "$label: last success $((age / 3600))h ago (limit $((max_age / 3600))h)"
  fi
}

check_heartbeat "$REPO_DIR/results/.last_success_paperscan" "$PAPER_MAX_AGE_S" "daily paper scan"
check_heartbeat "$REPO_DIR/results/.last_success_stopmonitor" "$STOP_MAX_AGE_S" "stop monitor"

health_out="$("$MONEY" health --run-id "$RUN_ID" --run-config "$RUN_CONFIG" 2>&1)"
health_rc=$?
if [ "$health_rc" -ne 0 ]; then
  add_problem "money health failed (exit $health_rc): $(printf '%s' "$health_out" | head -1)"
else
  halted="$(printf '%s\n' "$health_out" | sed -n 's/^ *halted: //p')"
  halt_reason="$(printf '%s\n' "$health_out" | sed -n 's/^ *halt_reason: //p')"
  if [ "$halted" = "True" ]; then
    add_problem "run is HALTED — reason: ${halt_reason:-unknown} (buys stop until reset)"
  fi
  # Scan coverage: `money health` reports, per asset scope, whether the ledger has
  # evaluated the newest *closed* bar of the cached data. A bar that is never the
  # newest one when a run looks is never scanned at all, and the frozen strategy
  # needs a fresh SMA cross to re-enter — so the cross is lost for good. Only
  # behind=1 (a live, still-actionable lag) alerts here; the permanent `gaps` list
  # stays in the log and the morning brief so an old skip cannot alert forever.
  behind_scopes="$(printf '%s\n' "$health_out" | sed -n 's/^ *coverage_\([a-z]*\): .*behind=1.*/\1/p')"
  for scope in $behind_scopes; do
    add_problem "scan coverage: ledger has not evaluated the newest closed $scope bar (data is ahead) — a skipped bar is never re-scanned, so re-run the desk"
  done
  # A buy intent that never executes permanently reserves exposure and blocks
  # its symbol (buy_already_pending) — e.g. an AMD intent sat pending 3 days
  # unnoticed (2026-09-11 bar, executed 2026-09-14). Alert only when the age is
  # abnormal: PENDING_MAX_AGE_H (default 96h) leaves room for a normal weekend
  # plus slot jitter.
  pending_age="$(printf '%s\n' "$health_out" | sed -n 's/^ *pending_intents: .*oldest_age_h=\([0-9.]*\).*/\1/p')"
  if [ -n "$pending_age" ] && awk -v a="$pending_age" -v m="${PENDING_MAX_AGE_H:-96}" 'BEGIN{exit !(a > m)}'; then
    add_problem "a buy intent has been pending ${pending_age}h (limit ${PENDING_MAX_AGE_H:-96}h) — it reserves exposure and blocks its symbol; check the scan/execution logs"
  fi
fi

{
  echo "--------------------------------------------------------------------"
  echo "money health: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  if [ -n "$problems" ]; then
    echo "UNHEALTHY"
    printf '%s\n' "$problems"
  else
    echo "healthy"
  fi
  printf '%s\n' "$health_out" | sed 's/^/  /'
} >> "$LOG" 2>&1

if [ -n "$problems" ]; then
  notify "money" "Health check FAILED — $(printf '%s' "$problems" | head -1)"
  echo "UNHEALTHY:"
  printf '%s\n' "$problems"
  exit 1
fi

echo "healthy"
exit 0
