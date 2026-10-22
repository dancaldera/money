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

# TRANSIENT_RE: the TRANSPORT failures a retry can fix — a DNS blip, a reset
# connection, or one of the bounded HTTP timeouts (trading/net.py, 5s connect /
# 20s read) that turns a hang into one of these messages. Measured in this desk:
# ConnectionResetError(54) and NameResolutionError (stop ticks, 2026-10-08), a
# full DNS outage at the 18:35 slot (2026-09-29), and a connect timeout. A
# DETERMINISTIC failure — BrokerError, RunSafetyError, a frozen-manifest
# mismatch, a rejected duplicate client_order_id — is deliberately not in the
# pattern: retrying it only delays the alert. One definition, shared by every
# wrapper, so the pattern cannot drift between them.
TRANSIENT_RE='ConnectionError|NameResolutionError|ConnectionResetError|Max retries exceeded|Connection reset by peer|timed out|Temporary failure in name resolution'

# run_step <max_attempts> <sleep_s> <no_retry_regex> <cmd...>: run one step of a
# scheduled wrapper, capturing combined output in STEP_OUT, its exit code in
# STEP_RC and the number of attempts used in STEP_ATTEMPTS.
#
# Retries only when the failure matches TRANSIENT_RE, never beyond max_attempts,
# and NEVER when the output matched no_retry_regex (pass "" for none): that
# pattern marks a step that already acted, where a re-run could double the
# action. Callers pass the "an order was already submitted" pattern. Every retry
# is announced, so the log shows what the schedule absorbed.
run_step() {
  local max="$1" sleep_s="$2" no_retry="$3"
  shift 3
  local attempt=1 out rc
  while :; do
    out="$("$@" 2>&1)"
    rc=$?
    [ "$rc" -eq 0 ] && break
    if [ -n "$no_retry" ] && printf '%s\n' "$out" | grep -qE "$no_retry"; then
      echo "attempt $attempt failed after acting — not retrying"
      break
    fi
    if [ "$attempt" -ge "$max" ] || ! printf '%s\n' "$out" | grep -qE "$TRANSIENT_RE"; then
      break
    fi
    echo "transient transport failure on attempt $attempt/$max — retrying in ${sleep_s}s"
    attempt=$((attempt + 1))
    [ "$sleep_s" -gt 0 ] && sleep "$sleep_s"
  done
  STEP_OUT="$out"
  STEP_RC="$rc"
  STEP_ATTEMPTS="$attempt"
  return 0
}

# replay_since <log> <byte_offset>: copy to stdout the bytes appended to <log>
# after <byte_offset> (from `wc -c`, taken before the redirected group). The
# scheduled wrappers write only to the log, and the silent cron jobs only read
# script stdout — without this replay a halt, a stop, or a submitted intent
# never reaches the job that pages the operator. Measured 2026-10-21: stock
# sells latched reconciliation_failed and both the stock slot and the stop tick
# exited 0 with empty stdout, so nobody was paged.
replay_since() {
  local log="$1" before="${2:-0}"
  [ -f "$log" ] || return 0
  tail -c +$((before + 1)) "$log"
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
