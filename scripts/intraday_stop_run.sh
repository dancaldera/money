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

# Set DRY_RUN=1 to report breaches without closing anything (for testing).
EXTRA=""
[ "${DRY_RUN:-0}" = "1" ] && EXTRA="--dry-run"

echo "--------------------------------------------------------------------"
echo "Stop monitor: $(date)  dry_run=${DRY_RUN:-0}"

# The CLI loads .env from the working directory, so keys are picked up here.
"$REPO_DIR/.venv/bin/money" paper-stops $EXTRA
echo "Exit code: $?"
