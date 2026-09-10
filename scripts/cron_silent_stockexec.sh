#!/bin/bash
# Hermes-cron stock execution (paper desk, fake money).
# Runs scripts/execute_stock_intents.sh just after the US cash open and stays
# SILENT unless an intent was submitted, the run halted, or the wrapper failed.
# Designed for a no_agent Hermes cronjob — empty stdout means "send nothing".
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(bash "$REPO_DIR/scripts/execute_stock_intents.sh" 2>&1)"
rc=$?
TAIL="$(printf '%s\n' "$OUT" | tail -30)"

if [ "$rc" -ne 0 ]; then
  printf '⚠️ money · ejecución de stocks FALLÓ (exit %s)\n%s\n' "$rc" "$TAIL"
  exit 0
fi

# `money execute-intents` prints Python dicts: {'action': 'submitted', ...}
if printf '%s\n' "$TAIL" | grep -qE "('action': 'submitted'|action=submitted)"; then
  printf '📈 money · intents de acciones ejecutados\n%s\n' "$TAIL"
fi
# halt state appears as `halted: True` (reconcile) or `halted=False` (paper-stops)
if printf '%s\n' "$TAIL" | grep -qE 'halted[:=] *True'; then
  printf '🛑 money · run HALTED (revisar results/stock_execution.log)\n%s\n' "$TAIL"
fi
exit 0
