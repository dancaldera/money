#!/bin/bash
# Daily paper-trading run. Invoked by launchd (see scripts/com.money.paperscan.plist).
# Runs the chosen strategy across the watchlist on the Alpaca PAPER account.
#
# Change STRATEGY here to switch what the schedule trades.
set -u
STRATEGY="rsi_meanrev"

# Repo root = parent of this script's directory.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1

# Set DRY_RUN=1 to evaluate signals without placing orders (for testing).
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

echo "===================================================================="
echo "Daily paper run: $(date)"
echo "strategy=$STRATEGY  repo=$REPO_DIR  dry_run=${DRY_RUN:-0}"

# The CLI loads .env from the working directory, so keys are picked up here.
"$REPO_DIR/.venv/bin/money" paper-scan --strategy "$STRATEGY" $EXTRA
echo "Exit code: $?"
