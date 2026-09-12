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

# record_scan_success <dry_run 0|1> <success_heartbeat> <preview_heartbeat>:
# record a finished daily scan. A DRY_RUN preview must NEVER advance the success
# heartbeat: the 22:00 catch-up guard runs the desk only when today's success
# heartbeat is missing, so a preview would mask a missed real run — and a missed
# day is permanent (paper-scan only ever evaluates the newest closed bar, so that
# bar's fresh crosses are never seen again). Previews keep a separate marker so a
# human can still tell that one ran.
record_scan_success() {
  local dry_run="$1" success="$2" preview="$3"
  if [ "$dry_run" = "1" ]; then
    heartbeat "$preview"
    echo "preview (DRY_RUN): success heartbeat NOT advanced ($success) — the catch-up guard still sees today as unscanned"
    return 0
  fi
  heartbeat "$success"
}
