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
