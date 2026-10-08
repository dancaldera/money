"""Contract tests for the scheduled wrapper scripts.

The CLI prints different shapes per command: ``paper-stops`` prints
``action=stopped`` / ``halted=False``, while ``execute-intents`` prints Python
dicts and ``reconcile`` prints ``halted: True``. A watchdog pattern that only
matches the wrong shape would never fire, which is a silent failure — so these
tests pin every grep pattern in the silent cron scripts against real samples.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from trading.run2.coverage import ScopeCoverage, bar_key

from .run2_helpers import ROOT


def _patterns(script: str) -> list[str]:
    """Every ERE the script passes to grep, single- or double-quoted."""
    found = re.findall(r"grep -[A-Za-z]*E? ?(?:'([^']+)'|\"([^\"]+)\")", script)
    # POSIX classes are valid for grep -E but not for Python's re; normalize so
    # the test validates the real pattern instead of failing on the syntax.
    return [(single or double).replace("[[:space:]]", r"\s") for single, double in found]


def test_silent_stop_patterns_match_real_cli_lines():
    patterns = _patterns((ROOT / "scripts" / "cron_silent_stop_run.sh").read_text())
    assert any(re.search(p, "  AAPL       P&L=-3.40% action=stopped") for p in patterns)
    assert any(re.search(p, "  halted: True") for p in patterns)
    assert not any(re.search(p, "  AAPL       P&L=-3.40% action=none") for p in patterns)
    assert not any(re.search(p, "  reconciliation: fills=0 fees=0 halted=False") for p in patterns)


def test_silent_stock_patterns_match_execute_intents_dict_repr():
    patterns = _patterns((ROOT / "scripts" / "cron_silent_stockexec.sh").read_text())
    # execute-intents renders rows with print(f"  {row}") -> dict repr
    assert any(
        re.search(p, "{'decision_id': 'abc', 'action': 'submitted', 'order_id': 'x'}")
        for p in patterns
    )
    assert any(re.search(p, "  halted: True") for p in patterns)
    assert not any(re.search(p, "  no eligible intents") for p in patterns)


def test_daily_wrapper_degrades_context_without_research_extra():
    """collect-context needs the optional [research] extra for FinBERT news; the
    wrapper must fall back to regime-only context instead of warning daily."""
    text = (ROOT / "scripts" / "daily_paper_run.sh").read_text()
    assert "import transformers" in text
    assert "--skip-news" in text and "CONTEXT_ARGS" in text


def test_daily_wrapper_matches_dict_and_action_equals_submitted():
    """Crypto execution rows come from execute-intents (dict repr); the notify
    must not depend on one exact shape only."""
    text = (ROOT / "scripts" / "daily_paper_run.sh").read_text()
    assert "'action': 'submitted'" in text
    assert "action=submitted" in text


def test_morning_brief_is_read_only_and_covers_the_desk():
    """The 09:00 Hermes briefing must never place orders or write the ledger."""
    text = (ROOT / "scripts" / "morning_brief.sh").read_text()
    assert "paper-status" in text
    assert "health --run-id" in text
    assert "paper-stops --run-id" in text and "--dry-run" in text
    assert "paper-scan --run-id" in text
    assert "paper-scan --run-id \"$RUN_ID\" --run-config \"$RUN_CONFIG\" --strategy \"$STRATEGY\" --dry-run" in text
    assert "execute-intents" not in text
    assert "reconcile" not in text
    assert "paper-close" not in text
    assert "run-init" not in text


CATCHUP = ROOT / "scripts" / "cron_catchup_daily.sh"


def _run_catchup(hb: Path, slot: str | None = None) -> subprocess.CompletedProcess:
    """Run the catch-up guard in CHECK_ONLY mode against a temp heartbeat."""
    env = dict(os.environ, PAPERSCAN_HEARTBEAT=str(hb), CHECK_ONLY="1")
    if slot is not None:
        env["SLOT_HHMM"] = slot
    return subprocess.run(
        ["bash", str(CATCHUP)], capture_output=True, text=True, env=env, cwd=str(ROOT)
    )


def _stamp_today(hb: Path, hour: int, minute: int) -> None:
    """Stamp the heartbeat with today's date at HH:MM.

    The guard compares the heartbeat's *clock time* against the slot, so its
    tests must control the time of day, not only the date.
    """
    moment = datetime.datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    os.utime(hb, (moment.timestamp(), moment.timestamp()))


def _run_bash(snippet: str) -> str:
    return subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, cwd=str(ROOT)
    ).stdout


def test_dry_run_preview_never_advances_the_success_heartbeat(tmp_path):
    """A preview must not make a day that never scanned look scanned.

    The 22:00 catch-up guard runs the desk only when today's success heartbeat is
    missing; a DRY_RUN preview that advanced it would mask a missed real run, and
    a missed day is permanent (paper-scan only evaluates the newest closed bar).
    """
    success, preview = tmp_path / "success", tmp_path / "preview"
    out = _run_bash(f'source scripts/_lib.sh; record_scan_success 1 "{success}" "{preview}"')
    assert preview.exists() and not success.exists()
    assert "NOT advanced" in out
    # end to end: the guard still sees the day as unscanned
    assert "WOULD RUN" in _run_catchup(success).stdout

    _run_bash(f'source scripts/_lib.sh; record_scan_success 0 "{success}" "{preview}"')
    assert success.read_text().strip().isdigit()
    # A REAL success silences the guard. Its stamp is "now", so compare it against
    # a slot the test has already passed (SLOT_HHMM hook) — the guard measures the
    # scan against the slot time, not just the calendar day.
    assert "skip" in _run_catchup(success, slot="00:00").stdout


def test_daily_wrapper_records_success_through_the_preview_guard():
    text = (ROOT / "scripts" / "daily_paper_run.sh").read_text()
    assert "record_scan_success" in text
    assert ".last_preview_paperscan" in text
    assert 'heartbeat "$REPO_DIR/results/.last_success_paperscan"' not in text


def test_catchup_is_silent_when_the_slot_already_scanned(tmp_path):
    """Idempotence: a scan AFTER today's slot silences it, whoever ran it (the
    18:35 script job or an earlier catch-up).

    The no_agent cron job delivers stdout verbatim, so the silent path must print
    nothing but the CHECK_ONLY note. The heartbeat is a calendar stamp, so the
    guard compares it against the slot TIME — a heartbeat from later today is
    tonight's scan, a heartbeat from earlier today is not.
    """
    hb = tmp_path / "hb"
    hb.write_text("1")
    _stamp_today(hb, 23, 55)  # after the 18:35 slot
    out = _run_catchup(hb).stdout
    assert "skip" in out and "WOULD RUN" not in out


def test_catchup_runs_when_todays_only_scan_predates_the_slot(tmp_path):
    """Money regression: a success from EARLIER today must not silence tonight.

    `paper-scan` only evaluates the newest closed bar, so the bar the 18:35 slot
    never scanned is a permanent signal loss (a fresh cross is required to
    re-enter). An operator's manual run at 09:00 advances the same heartbeat the
    slot writes, and a date-only comparison would treat it as proof that tonight
    scanned — losing the bar silently. The re-run the guard then does is
    idempotent per bar, so erring towards running costs a duplicate no-op scan,
    never a lost entry.
    """
    hb = tmp_path / "hb"
    hb.write_text("1")
    _stamp_today(hb, 0, 5)  # today, before the 18:35 slot
    out = _run_catchup(hb).stdout
    assert "WOULD RUN" in out and "skip" not in out


def test_catchup_would_run_when_heartbeat_is_stale_or_missing(tmp_path):
    stale = tmp_path / "stale"
    stale.write_text("1")
    two_days = time.time() - 2 * 86400
    os.utime(stale, (two_days, two_days))
    out = _run_catchup(stale).stdout
    assert "WOULD RUN" in out and "skip" not in out
    assert "WOULD RUN" in _run_catchup(tmp_path / "missing").stdout


def test_catchup_check_only_never_runs_the_desk(tmp_path):
    """CHECK_ONLY is the test hook; the real path must call the daily wrapper."""
    assert "WOULD RUN" in _run_catchup(tmp_path / "missing").stdout
    text = CATCHUP.read_text()
    assert "daily_paper_run.sh" in text
    assert "CHECK_ONLY" in text
    # the slot the guard backs up must stay overridable: the DST-free comparison
    # and the tests both depend on it.
    assert "SLOT_HHMM" in text


def test_catchup_flags_a_wrapper_that_did_not_record(tmp_path):
    """Exit 0 without advancing the heartbeat is the silent failure mode."""
    text = CATCHUP.read_text()
    assert 'source "$REPO_DIR/scripts/_lib.sh"' in text
    assert "unscanned" in text


def test_health_coverage_line_drives_the_watchdog_sed():
    """The coverage alert fires only if health_check.sh's sed still matches the
    real ``money health`` line: the CLI writes the shape, the watchdog parses it.

    Coverage is the second half of the missed-bar hole the 22:00 catch-up guard
    closes: a bar that is never the newest one when a run looks (a run that
    fetches fresh data and records nothing, a slot that steps over the 00:00 UTC
    crypto close) is never scanned and the frozen strategy needs a fresh cross to
    re-enter — so the cross is lost for good. Only ``behind=1`` (a live lag, still
    actionable) may alert; an old ``gaps`` skip must not alert forever.
    """
    expr = next(e for e in re.findall(r"sed -n '([^']+)'", (ROOT / "scripts" / "health_check.sh").read_text())
                if "coverage_" in e)

    def _scope(behind: bool) -> str:
        return ScopeCoverage(
            scope="crypto",
            symbols=8,
            newest_closed=bar_key("2026-09-13T00:00:00+00:00"),
            newest_recorded=bar_key("2026-09-12T00:00:00+00:00" if behind else "2026-09-13T00:00:00+00:00"),
            gaps=(bar_key("2026-09-11T00:00:00+00:00"),),
            recent=(bar_key("2026-09-13T00:00:00+00:00"), bar_key("2026-09-12T00:00:00+00:00")),
        ).health_line()

    assert _run_bash(f"printf '%s\\n' '{_scope(True)}' | sed -n '{expr}'").strip() == "crypto"
    assert _run_bash(f"printf '%s\\n' '{_scope(False)}' | sed -n '{expr}'").strip() == ""


def test_health_pending_line_drives_the_watchdog_sed():
    """The pending-intent alert fires only if health_check.sh's sed still matches
    the real ``money health`` line: the CLI writes the shape, the watchdog parses
    it.

    A buy intent that never executes permanently reserves exposure and blocks its
    symbol (`buy_already_pending`), so a stuck one must surface in the health
    dump; the count=0 shape must not match or the alert could never clear.
    """
    expr = next(e for e in re.findall(r"sed -n '([^']+)'", (ROOT / "scripts" / "health_check.sh").read_text())
                if "pending_intents" in e)

    line = "  pending_intents: count=1 oldest=AMD oldest_age_h=76.4"
    assert _run_bash(f"printf '%s\\n' '{line}' | sed -n '{expr}'").strip() == "76.4"
    assert _run_bash(f"printf '%s\\n' '  pending_intents: count=0' | sed -n '{expr}'").strip() == ""


def _health_line(scope: str, behind: bool) -> str:
    """A real ``money health`` coverage line, from the CLI's own formatter."""
    return ScopeCoverage(
        scope=scope,
        symbols=8,
        newest_closed=bar_key("2026-09-29T00:00:00+00:00"),
        newest_recorded=bar_key("2026-09-28T00:00:00+00:00" if behind else "2026-09-29T00:00:00+00:00"),
        gaps=(),
        recent=(),
    ).health_line()


# The confirmation fingerprint the guard derives from ``_health_line``'s sample:
# scope@newest_closed, i.e. *which* gap, not just which scope is behind.
GAP_FINGERPRINT = "crypto@2026-09-29T00:00:00"


def _run_salvage(
    tmp_path,
    *,
    behind: bool,
    stamp: Path | None = None,
    confirm: bool = False,
    pre_slot: str = "",
    slot: str | None = None,
) -> str:
    """Run the catch-up guard in coverage mode, with the ledger state injected.

    ``confirm`` pre-seeds the confirmation marker with the fingerprint of the
    injected gap, aged past ``SALVAGE_CONFIRM_S``: that is what a *later*
    watchdog tick looks like (the first sighting only records the gap).
    """
    snapshot = tmp_path / f"health_{behind}.txt"
    snapshot.write_text(
        _health_line("crypto", behind) + "\n" + _health_line("equity", False) + "\n"
    )
    confirm_stamp = tmp_path / "coverage_confirm"
    if confirm:
        confirm_stamp.write_text(GAP_FINGERPRINT)
        long_ago = time.time() - 2 * 3600
        os.utime(confirm_stamp, (long_ago, long_ago))
    env = dict(
        os.environ,
        CHECK_ONLY="1",
        COVERAGE_ONLY="1",
        HEALTH_SNAPSHOT=str(snapshot),
        PAPERSCAN_HEARTBEAT=str(tmp_path / "hb_missing"),
        SALVAGE_STAMP=str(stamp or tmp_path / "salvage_stamp"),
        COVERAGE_CONFIRM_STAMP=str(confirm_stamp),
        PRE_SLOT_HHMM=pre_slot,
    )
    if slot is not None:
        env["SLOT_HHMM"] = slot
    return subprocess.run(
        ["bash", str(CATCHUP)], capture_output=True, text=True, env=env, cwd=str(ROOT)
    ).stdout


def test_coverage_salvage_acts_on_the_ledger_not_the_clock(tmp_path):
    """The third line of defence: a slot that ran and still lost its bar.

    2026-09-29: the 18:35 run died on a full DNS outage (Alpaca and the CBOE VIX
    both unresolvable) and the 22:45 catch-up salvaged the bar. Had that second
    run failed too, nothing was left — `health_check.sh` only *reports* a
    `behind=1` coverage line, and once the next bar closes the skip is permanent
    (`paper-scan` only ever evaluates the newest closed bar). So the hourly
    watchdog now calls this guard in coverage mode: the heartbeat is irrelevant
    here (it is missing in both cases below), the ledger's coverage line decides.
    """
    # First sighting: recorded, not acted on — a bar that has just closed is
    # behind=1 while the slot is still running (2026-09-30's duplicate run).
    first = _run_salvage(tmp_path, behind=True)
    assert "first sighting" in first and "WOULD RUN" not in first
    assert (tmp_path / "coverage_confirm").read_text() == GAP_FINGERPRINT
    # A later tick with the same gap (the slot died, or never scanned) acts.
    out = _run_salvage(tmp_path, behind=True, confirm=True)
    assert "WOULD RUN" in out and "coverage behind on crypto" in out
    assert "skip" not in out
    # Coverage current: one read-only health call and nothing else (the hourly
    # job must stay silent, its stdout is delivered verbatim) — and the marker is
    # dropped so a fresh gap starts its own confirmation clock.
    quiet = _run_salvage(tmp_path, behind=False)
    assert "skip (coverage ok" in quiet and "WOULD RUN" not in quiet
    assert not (tmp_path / "coverage_confirm").exists()


def test_coverage_salvage_cannot_race_the_daily_slot(tmp_path):
    """Inside the bar-close window (18:00 UTC close -> 18:35 slot) the scheduled
    job owns the run: a cache that just advanced must not start a second scan
    minutes before the slot, or the measured entry timing stops meaning anything.
    """
    now = datetime.datetime.now()
    start, end = now - datetime.timedelta(minutes=10), now + datetime.timedelta(minutes=10)
    if start.strftime("%H:%M") > end.strftime("%H:%M"):
        pytest.skip("window wraps past midnight")
    out = _run_salvage(
        tmp_path, behind=True, confirm=True, pre_slot=start.strftime("%H:%M"), slot=end.strftime("%H:%M")
    )
    assert "bar-close window" in out and "WOULD RUN" not in out
    # ...and outside it the same state still acts.
    assert "WOULD RUN" in _run_salvage(tmp_path, behind=True, confirm=True)


def test_coverage_salvage_bounds_retries_with_a_cooldown_stamp(tmp_path):
    """A bar that stays unevaluated must not be retried into a storm: the stamp
    is written on every attempt (success or failure), so the next hourly tick is
    the earliest retry.
    """
    stamp = tmp_path / "salvage_stamp"
    stamp.write_text("")
    out = _run_salvage(tmp_path, behind=True, stamp=stamp, confirm=True)
    assert "cooldown" in out and "WOULD RUN" not in out
    text = CATCHUP.read_text()
    assert 'touch "$SALVAGE_STAMP"' in text
    # the stamp must be written before the failure branch can exit
    assert text.index('touch "$SALVAGE_STAMP"') < text.index("catch-up FAILED")
    # an attempt older than the cooldown is allowed through again
    two_days = time.time() - 2 * 86400
    os.utime(stamp, (two_days, two_days))
    assert "WOULD RUN" in _run_salvage(tmp_path, behind=True, stamp=stamp, confirm=True)


def test_coverage_salvage_needs_a_second_tick_before_it_runs(tmp_path):
    """A gap seen for the first time is only recorded (2026-09-30 regression).

    The 18:35 slot started 9m late, so the watchdog's 18:45 tick read behind=1
    while the slot's own context was still running and started a full duplicate
    desk run (18:44:58 -> 21:32:23). The guard now needs the gap to survive
    ``SALVAGE_CONFIRM_S`` and be seen again on a later tick; the slot's scan
    clears the marker, so no second run happens.
    """
    out = _run_salvage(tmp_path, behind=True)
    assert "first sighting" in out and "WOULD RUN" not in out
    # same gap, marker fresh (the next tick is seconds later): still confirming
    assert "confirming at" in _run_salvage(tmp_path, behind=True)
    text = CATCHUP.read_text()
    # the marker is keyed on scope@newest_closed, so a *new* bar restarts it
    assert "newest_closed=" in text and "behind_fp" in text
    # the race window alone could not catch a late slot, which is why this exists
    assert "SALVAGE_CONFIRM_S" in text


def test_hourly_watchdog_wires_the_salvage_in_coverage_mode():
    """The salvage only exists if the hourly job actually calls it — and it must
    not run the slot-mode rule there, where a heartbeat from earlier today would
    be read as "tonight ran" (it is 00:00-18:35 that would fire every hour).
    """
    text = (ROOT / "scripts" / "cron_silent_health.sh").read_text()
    assert "COVERAGE_ONLY=1 bash" in text
    assert "cron_catchup_daily.sh" in text
    assert "HEALTH_SNAPSHOT" not in text  # no test hook in the live path


# --- stop monitor: retry on transient broker failures, and never hide a failure
#
# The 8% stop is the only intraday protection the book has, and it is a 30-min
# tick: a tick that dies on a DNS blip or a reset connection leaves every open
# position unenforced until the next one (measured 2026-10-08: 03:48 CST
# ConnectionResetError(54), 07:40 CST NameResolutionError — ~54 min unprotected
# with BTC already -5%). Two rules are pinned here: a *transport* failure is
# retried in place, and the CLI's exit code reaches the caller, because the
# silent cron wrapper's alert branch (`rc -ne 0`) was unreachable while the
# wrapper always exited 0 — so a failed stop check was invisible everywhere the
# operator looks (no EMAIL_* keys in .env, the desktop notification the only
# other alert).

STOP_WRAPPER = ROOT / "scripts" / "intraday_stop_run.sh"
SILENT_STOP = ROOT / "scripts" / "cron_silent_stop_run.sh"

# A stub `money` CLI: calls 1..STUB_FAILS-1 of paper-stops fail (printing
# `$STUB_ERR`, which is what the retry regex reads), then it succeeds —
# STUB_FAILS is the number of the first call that succeeds.
_STUB = """#!/bin/bash
echo "$@" >> "$STUB_CALLS"
[ "$1" = "paper-stops" ] || exit 0
n="$(grep -c '^paper-stops' "$STUB_CALLS")"
if [ "$n" -lt "${STUB_FAILS:-0}" ]; then
  printf '%s\\n' "${STUB_ERR:-NameResolutionError: Max retries exceeded}"
  exit 1
fi
printf '%s\\n' "  AAPL       P&L=+1.00% action=none" \
              "  reconciliation: fills=0 fees=0 halted=False"
exit 0
"""


def _stop_env(tmp_path, **over):
    stub = tmp_path / "money_stub"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    calls = tmp_path / "calls"
    # Silence the desktop notification path (osascript on macOS, notify-send on
    # Linux): the tests are about the exit code, not about popping up alerts.
    quiet = tmp_path / "bin"
    quiet.mkdir(exist_ok=True)
    for tool in ("osascript", "notify-send"):
        (quiet / tool).write_text("#!/bin/bash\nexit 0\n")
        (quiet / tool).chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{quiet}:{os.environ.get('PATH', '')}",
        MONEY_BIN=str(stub),
        STUB_CALLS=str(calls),
        STOP_LOG=str(tmp_path / "stop.log"),
        STOP_HEARTBEAT=str(tmp_path / "stop_hb"),
        STOP_RETRY_SLEEP="0",
    )
    env.update(over)
    return env, calls, tmp_path / "stop_hb"


def _stop_calls(calls: Path) -> int:
    return sum(1 for line in calls.read_text().splitlines() if line.startswith("paper-stops"))


def _run_stop(script, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, cwd=str(ROOT)
    )


def test_stop_wrapper_retries_a_transient_broker_failure(tmp_path):
    env, calls, hb = _stop_env(tmp_path, STUB_FAILS="3")
    done = _run_stop(STOP_WRAPPER, env)
    assert done.returncode == 0
    assert _stop_calls(calls) == 3  # two transient failures, then the real check
    assert hb.exists()  # the run counts as successful
    log = (tmp_path / "stop.log").read_text()
    assert "transient broker failure on attempt 1/3" in log
    assert "transient broker failure on attempt 2/3" in log
    assert "Stop monitor attempts used: 3" in log and "Exit code: 0" in log


def test_stop_wrapper_retry_is_bounded_and_surfaces_the_failure(tmp_path):
    """Three failures must not become an unbounded loop — and the run must end
    non-zero so the silent cron wrapper can alert instead of swallowing it."""
    env, calls, hb = _stop_env(tmp_path, STUB_FAILS="9")
    done = _run_stop(STOP_WRAPPER, env)
    assert _stop_calls(calls) == 3
    assert done.returncode == 1 and not hb.exists()
    assert "Exit code: 1" in (tmp_path / "stop.log").read_text()


def test_stop_wrapper_never_retries_after_acting_on_a_position(tmp_path):
    """A re-run after a submitted close could double-submit, so any
    ``action=stopped`` line ends the loop even though the failure looks transport."""
    env, calls, hb = _stop_env(
        tmp_path, STUB_FAILS="9", STUB_ERR="  AAPL  P&L=-9.00% action=stopped\nboom ConnectionResetError"
    )
    done = _run_stop(STOP_WRAPPER, env)
    assert _stop_calls(calls) == 1
    assert done.returncode == 1
    assert "failed after acting on a position" in (tmp_path / "stop.log").read_text()


def test_stop_wrapper_never_retries_a_failure_a_retry_cannot_fix(tmp_path):
    """A frozen-manifest mismatch or a BrokerError is deterministic: retrying only
    delays the alert."""
    env, calls, _ = _stop_env(tmp_path, STUB_FAILS="9", STUB_ERR="BrokerError: frozen values changed")
    done = _run_stop(STOP_WRAPPER, env)
    assert _stop_calls(calls) == 1 and done.returncode == 1


def test_failed_stop_monitor_reaches_the_silent_cron_wrapper(tmp_path):
    """End to end: the failure must produce the alert line the Hermes job delivers
    (empty stdout = nothing sent), which required the inner wrapper's exit code."""
    env, _, _ = _stop_env(tmp_path, STUB_FAILS="9")
    done = _run_stop(SILENT_STOP, env)
    assert done.returncode == 0  # the silent wrapper always exits 0 by design
    assert "stop monitor FALL" in done.stdout
    # ...and a healthy run stays silent.
    ok_env, _, _ = _stop_env(tmp_path, STUB_FAILS="0")
    quiet = _run_stop(SILENT_STOP, ok_env)
    assert quiet.stdout.strip() == ""


def test_stop_wrapper_defaults_still_point_at_the_repo_cli():
    """The seams are opt-in: the live path must keep using the venv CLI, or the
    scheduled job would run a stub (or fail to find `money`)."""
    text = STOP_WRAPPER.read_text()
    assert 'MONEY="${MONEY_BIN:-$REPO_DIR/.venv/bin/money}"' in text
    assert '.venv/bin/money" paper-stops' not in text  # all calls go through $MONEY


def test_dry_run_preview_never_advances_the_stop_heartbeat(tmp_path):
    """A DRY_RUN preview places no stops, so it must not satisfy the hourly
    watchdog's staleness check for the stop monitor — the same rule the scan
    heartbeat already follows (record_scan_success). It keeps its own marker."""
    preview = tmp_path / ".last_preview_stopmonitor"
    env, _, hb = _stop_env(tmp_path, DRY_RUN="1", STOP_PREVIEW_HEARTBEAT=str(preview))
    assert _run_stop(STOP_WRAPPER, env).returncode == 0
    assert preview.exists() and not hb.exists()
    assert "success heartbeat NOT advanced" in (tmp_path / "stop.log").read_text()
    # a real (non-preview) success still advances the success heartbeat
    env, _, hb = _stop_env(tmp_path)
    assert _run_stop(STOP_WRAPPER, env).returncode == 0 and hb.exists()

