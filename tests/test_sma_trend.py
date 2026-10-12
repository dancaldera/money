"""Unit tests for ``sma_trend_up`` — the trend *state* the experiment-only
re-entry window consults (the live gate only ever sees the crossing *event*)."""

from __future__ import annotations

import numpy as np

from trading.strategies.base import sma_trend_up


def test_trend_up_is_true_when_fast_sma_leads():
    assert sma_trend_up([10.0] * 30 + [9.0, 20.0], fast=10, slow=30) is True


def test_trend_up_is_false_after_a_deep_decline():
    # Fast average collapses while the 30-bar average is still carrying the
    # earlier, higher prices -> the trend has broken.
    values = [10.0] * 30 + [9.0, 20.0, 20.0, 18.0] + [2.0] * 10
    assert sma_trend_up(values, fast=10, slow=30) is False


def test_trend_up_needs_a_full_slow_window():
    assert sma_trend_up([10.0] * 30, fast=10, slow=30) is False


def test_trend_up_is_false_when_a_window_value_is_missing():
    values = [10.0] * 30 + [9.0, 20.0]
    values[-1] = float("nan")
    assert sma_trend_up(values, fast=10, slow=30) is False


def test_trend_up_accepts_lists_and_series():
    closes = np.linspace(100.0, 120.0, 60)
    assert sma_trend_up(closes, fast=10, slow=30) is True
