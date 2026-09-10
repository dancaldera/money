#!/bin/bash
# Hermes-cron stop monitor (paper desk, fake money).
# Runs scripts/intraday_stop_run.sh and stays SILENT unless something happened:
# a stop was fired, the run halted, or the wrapper failed.
# Designed for a no_agent Hermes cronjob — empty stdout means "send nothing".
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(bash "$REPO_DIR/scripts/intraday_stop_run.sh" 2>&1)"
rc=$?
TAIL="$(printf '%s\n' "$OUT" | tail -30)"

if [ "$rc" -ne 0 ]; then
  printf '⚠️ money · stop monitor FALLÓ (exit %s)\n%s\n' "$rc" "$TAIL"
  exit 0
fi

# The CLI prints `action=stopped` / `action=would_stop` (never a Python dict repr).
if printf '%s\n' "$TAIL" | grep -qE 'action=(stopped|would_stop)'; then
  printf '🔻 money · stop disparado\n%s\n' "$TAIL"
fi
if printf '%s\n' "$TAIL" | grep -qE 'halted=True'; then
  printf '🛑 money · run HALTED (revisar results/stop_monitor.log)\n%s\n' "$TAIL"
fi
exit 0
