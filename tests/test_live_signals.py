"""Offline tests for live signal generation (no network, no Alpaca keys)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading.live.signals import get_signal_fn, rsi_meanrev_signal, sma_cross_signal


def _frame(close: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=len(close), freq="D")
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0},
        index=idx,
    )


def test_rsi_signal_extremes():
    # Strictly declining prices -> RSI 0 -> oversold -> BUY.
    assert rsi_meanrev_signal(_frame(np.linspace(200, 50, 60))) == "BUY"
    # Strictly rising prices -> RSI 100 -> overbought -> SELL.
    assert rsi_meanrev_signal(_frame(np.linspace(50, 200, 60))) == "SELL"


def test_sma_cross_detects_both_crosses():
    # Rise, then fall, then rise. The fall makes the fast SMA cross BELOW the
    # slow (a fresh SELL), and the final rise makes it cross back ABOVE (BUY).
    close = np.concatenate(
        [np.linspace(100, 160, 50), np.linspace(160, 90, 50), np.linspace(90, 180, 50)]
    )
    df = _frame(close)
    signals = [sma_cross_signal(df.iloc[:i]) for i in range(35, len(df) + 1)]
    assert "SELL" in signals
    assert "BUY" in signals


def test_signal_returns_valid_values():
    df = _frame(np.linspace(100, 105, 80))
    for name in ("sma_cross", "rsi_meanrev"):
        assert get_signal_fn(name)(df) in {"BUY", "SELL", "HOLD"}


def test_short_history_is_hold():
    df = _frame(np.linspace(100, 110, 5))
    assert sma_cross_signal(df) == "HOLD"
    assert rsi_meanrev_signal(df) == "HOLD"
