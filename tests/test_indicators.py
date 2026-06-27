"""Tests for the shared indicator helpers (offline, deterministic)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading.strategies.base import rsi, sma


def _wilder_rsi_reference(closes: np.ndarray, period: int = 14) -> float:
    """Independent recursive Wilder RSI (SMA-seeded) for the final bar.

    This is the textbook definition: seed the average gain/loss with the simple
    mean of the first ``period`` deltas, then smooth recursively with the Wilder
    multiplier ``(period-1)/period``. Used to confirm ``rsi()`` is Wilder-style
    rather than a plain rolling mean.
    """
    deltas = np.diff(closes)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def test_rsi_matches_wilder_definition():
    rng = np.random.default_rng(3)
    closes = 100 + rng.normal(0, 1, size=200).cumsum()
    # Over 200 bars the EMA vs SMA seed difference washes out; they converge.
    got = float(rsi(closes, 14)[-1])
    expected = _wilder_rsi_reference(closes, 14)
    assert abs(got - expected) < 0.1


def test_rsi_differs_from_simple_rolling_mean():
    """Guard against regressing to the old SMA-based RSI."""
    rng = np.random.default_rng(11)
    closes = 100 + rng.normal(0, 1, size=120).cumsum()
    s = pd.Series(closes)
    delta = s.diff()
    sma_rs = delta.clip(lower=0).rolling(14).mean() / (-delta.clip(upper=0)).rolling(14).mean()
    sma_rsi = (100 - 100 / (1 + sma_rs)).to_numpy()
    # The two methods should give materially different values mid-series.
    assert abs(float(rsi(closes, 14)[60]) - float(sma_rsi[60])) > 1.0


def test_rsi_bounds_and_warmup():
    closes = 100 + np.random.default_rng(1).normal(0, 1, size=80).cumsum()
    out = rsi(closes, 14)
    finite = out[np.isfinite(out)]
    assert finite.min() >= 0.0 and finite.max() <= 100.0
    assert np.isnan(out[:14]).all()  # not enough history to compute yet


def test_sma_basic():
    out = sma(np.array([1.0, 2, 3, 4, 5]), 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == 2.0 and out[4] == 4.0
