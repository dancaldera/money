"""Indicator helpers shared by strategies.

These are written as plain functions over a price array so they work with
``backtesting.Strategy.self.I(...)``, which calls them and wraps the result.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(values, period: int) -> np.ndarray:
    """Simple moving average."""
    return pd.Series(values).rolling(period).mean().to_numpy()


def rsi(values, period: int = 14) -> np.ndarray:
    """Relative Strength Index using Wilder's smoothing.

    Wilder's smoothing is an exponential moving average with ``alpha = 1/period``
    (seeded once ``period`` observations exist). This matches the RSI shown by
    standard charting tools such as TradingView, so the backtest, the live signal
    and ``money signal`` all agree on the same indicator.

    Edge cases resolve naturally: no losses -> RSI 100, no gains -> RSI 0, and a
    perfectly flat series -> NaN (treated as "no signal").
    """
    s = pd.Series(values, dtype="float64")
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    return out.to_numpy()
