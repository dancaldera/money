"""Contract tests for the scheduled wrapper scripts.

The CLI prints different shapes per command: ``paper-stops`` prints
``action=stopped`` / ``halted=False``, while ``execute-intents`` prints Python
dicts and ``reconcile`` prints ``halted: True``. A watchdog pattern that only
matches the wrong shape would never fire, which is a silent failure — so these
tests pin every grep pattern in the silent cron scripts against real samples.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

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
    assert "paper-scan --run-id \"$RUN_ID\" --strategy \"$STRATEGY\" --dry-run" in text
    assert "execute-intents" not in text
    assert "reconcile" not in text
    assert "paper-close" not in text
    assert "run-init" not in text


CATCHUP = ROOT / "scripts" / "cron_catchup_daily.sh"


def _run_catchup(hb: Path) -> subprocess.CompletedProcess:
    """Run the catch-up guard in CHECK_ONLY mode against a temp heartbeat."""
    env = dict(os.environ, PAPERSCAN_HEARTBEAT=str(hb), CHECK_ONLY="1")
    return subprocess.run(
        ["bash", str(CATCHUP)], capture_output=True, text=True, env=env, cwd=str(ROOT)
    )


def test_catchup_is_silent_when_todays_scan_already_succeeded(tmp_path):
    """Idempotence: any successful scan today (agent job or catch-up) silences it.

    The 17:00 daily run is an AGENT job, so it can die (model outage, inactivity
    timeout) before it ever reaches the wrapper; this guard must never double-run
    the desk on a day that already scanned. The no_agent cron job delivers stdout
    verbatim, so the real path must print nothing.
    """
    hb = tmp_path / "hb"
    hb.write_text("1")
    out = _run_catchup(hb).stdout
    assert "skip" in out and "WOULD RUN" not in out


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


def test_catchup_flags_a_wrapper_that_did_not_record(tmp_path):
    """Exit 0 without advancing the heartbeat is the silent failure mode."""
    text = CATCHUP.read_text()
    assert 'source "$REPO_DIR/scripts/_lib.sh"' in text
    assert "unscanned" in text
