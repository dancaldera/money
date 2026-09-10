"""Contract tests for the scheduled wrapper scripts.

The CLI prints different shapes per command: ``paper-stops`` prints
``action=stopped`` / ``halted=False``, while ``execute-intents`` prints Python
dicts and ``reconcile`` prints ``halted: True``. A watchdog pattern that only
matches the wrong shape would never fire, which is a silent failure — so these
tests pin every grep pattern in the silent cron scripts against real samples.
"""

from __future__ import annotations

import re

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
