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
  RUN_ID="${RUN_ID:-run2}" \
  STOP_MAX_AGE_S="${STOP_MAX_AGE_S:-21600}" \
  PAPER_MAX_AGE_S="${PAPER_MAX_AGE_S:-108000}" \
  bash "$REPO_DIR/scripts/health_check.sh" 2>&1
)"
rc=$?

if [ "$rc" -ne 0 ]; then
  printf '🩺 money · health UNHEALTHY\n%s\n' "$OUT"
fi
exit 0
