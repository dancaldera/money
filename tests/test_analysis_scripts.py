"""Tests for the execution-timing analysis scripts.

``scripts/analysis_execution_gaps.py`` backs the claim that the daily run's slot
decides which crypto bar is scanned, so its measurement primitives are pinned
here on synthetic frames (the scripts themselves read the gitignored ``data/``).

Frames are laid out so the fixtures are easy to reason about: 40 flat bars (so
SMA(30) exists and starts flat), then a 10-bar rally that produces exactly one
fresh up cross on the 41st bar, then a long decline that produces exactly one
fresh down cross.
"""

from __future__ import annotations

import importlib.util

import pandas as pd
import pytest

from .run2_helpers import ROOT

_SPEC = importlib.util.spec_from_file_location(
    "analysis_execution_gaps", ROOT / "scripts" / "analysis_execution_gaps.py"
)
assert _SPEC and _SPEC.loader
gaps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gaps)

FLAT = [100.0] * 40  # index 0-39
CROSS = 40  # index of the fresh up cross (first non-flat bar)
RALLY = [100 + i for i in range(1, 11)]  # index 40-49, ends at 110
DECLINE = [110 - i for i in range(1, 41)]  # index 50-89, ends at 70


def _frame(closes, opens=None, lows=None):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Open": list(opens) if opens is not None else list(closes),
            "High": [c * 1.01 for c in closes],
            "Low": list(lows) if lows is not None else [c * 0.99 for c in closes],
            "Close": list(closes),
        },
        index=idx,
    )


def test_fresh_crosses_fires_once_per_crossing():
    closes = FLAT + RALLY + DECLINE
    up, down = gaps.fresh_crosses(_frame(closes)["Close"])
    assert int(up.sum()) == 1 and int(down.sum()) == 1
    assert up[up].index[0] == pd.Timestamp("2024-01-01") + pd.Timedelta(days=CROSS)
    assert down[down].index[0] > up[up].index[0]
    # Regression guard: shift() on a bool series gives object dtype, where `~` is
    # integer bitwise negation, so `~above.shift(1).fillna(False)` marks EVERY bar
    # as a fresh cross (that bug inflated a cross count from 74 to 873 on BTC).
    assert int(up.sum()) + int(down.sum()) < len(closes) / 2


def test_late_drift_cost_prices_the_bar_after_the_signal():
    out = gaps.late_drift_cost({"TEST": _frame(FLAT + [101, 102, 103, 104, 105])})
    assert out.loc[0, "buys"] == 1
    assert out.loc[0, "buy_drift_%"] == pytest.approx(100 * (102 / 101 - 1), abs=1e-6)

    # a sell waits a bar too: a cross that keeps falling costs the delayed exit
    out = gaps.late_drift_cost({"TEST": _frame(FLAT + RALLY + DECLINE)})
    assert out.loc[0, "sells"] == 1
    assert out.loc[0, "sell_cost_%"] != 0.0


def test_buy_intents_expired_counts_only_adverse_gaps_over_the_cap():
    # +4% on the bar after the signal cross: outside the frozen 3% crypto cap
    row = gaps.late_drift_cost({"TEST": _frame(FLAT + [101, 105, 106, 107, 108])}).loc[0]
    assert row["buys_expired_%"] == 100.0

    # +1%: inside the cap, the intent is executable a bar late
    row = gaps.late_drift_cost({"TEST": _frame(FLAT + [101, 102, 103, 104, 105])}).loc[0]
    assert row["buys_expired_%"] == 0.0


def test_stock_gap_fill_separates_open_resting_and_never_filled():
    closes = FLAT + [101, 101, 101]  # sells nothing; limit = 101 * 1.02 = 103.02
    opens = [100.0] * CROSS + [105, 105, 105]  # next open gaps 5% above the limit

    # never trades back to the limit -> the entry is never filled
    lows = [100.0] * CROSS + [100.0, 104, 104]
    row = gaps.stock_gap_fill(_frame(closes, opens=opens, lows=lows))
    assert row["signals"] == 1 and row["never_%"] == 100.0

    # same gap, but the session dips to the limit -> it rests and fills later
    lows = [100.0] * CROSS + [100.0, 101, 104]
    row = gaps.stock_gap_fill(_frame(closes, opens=opens, lows=lows))
    assert row["resting_%"] == 100.0 and row["at_open_%"] == 0.0

    # opens at the signal close -> marketable at the open, no cap effect
    opens = [100.0] * CROSS + [100.5, 100.5, 100.5]
    lows = [99.0] * len(closes)
    assert gaps.stock_gap_fill(_frame(closes, opens=opens, lows=lows))["at_open_%"] == 100.0


def test_stock_gap_fill_handles_no_signals():
    assert gaps.stock_gap_fill(_frame([100.0] * 10))["signals"] == 0
