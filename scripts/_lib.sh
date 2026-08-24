#!/bin/bash
# Shared helpers for the launchd wrapper scripts. Sourced, not executed.

# rotate_log <path> [max_lines]: keep a log bounded by trimming to the last half
# of max_lines once it grows past max_lines. Call before redirecting into <path>.
rotate_log() {
  local log="$1" max="${2:-2000}"
  mkdir -p "$(dirname "$log")" 2>/dev/null || true
  if [ -f "$log" ] && [ "$(wc -l < "$log" 2>/dev/null || echo 0)" -gt "$max" ]; then
    tail -n "$((max / 2))" "$log" > "$log.tmp" 2>/dev/null && mv "$log.tmp" "$log"
  fi
}

# notify <title> <message>: best-effort desktop notification — notify-send on
# Linux, osascript on macOS. Never fails the caller.
notify() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "$1" "$2" >/dev/null 2>&1 || true
  else
    /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
  fi
}

# heartbeat <path>: record a successful run's unix timestamp, so a missed run
# (e.g. the Mac slept through the schedule) is visible as a stale file.
heartbeat() {
  date +%s > "$1" 2>/dev/null || true
}
