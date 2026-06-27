#!/bin/bash
# Shared helpers for the launchd wrapper scripts. Sourced, not executed.

# rotate_log <path> [max_lines]: keep a log bounded by trimming to the last half
# of max_lines once it grows past max_lines. Call before redirecting into <path>.
rotate_log() {
  local log="$1" max="${2:-2000}"
  if [ -f "$log" ] && [ "$(wc -l < "$log" 2>/dev/null || echo 0)" -gt "$max" ]; then
    tail -n "$((max / 2))" "$log" > "$log.tmp" 2>/dev/null && mv "$log.tmp" "$log"
  fi
}

# notify <title> <message>: best-effort macOS notification. LaunchAgents run in
# the user GUI session, so this surfaces on screen. Never fails the caller.
notify() {
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
}

# heartbeat <path>: record a successful run's unix timestamp, so a missed run
# (e.g. the Mac slept through the schedule) is visible as a stale file.
heartbeat() {
  date +%s > "$1" 2>/dev/null || true
}
