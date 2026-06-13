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
    """Relative Strength Index (Wilder-style smoothing via rolling mean).

    Edge cases resolve naturally: a window with no losses -> RSI 100, no gains
    -> RSI 0, and a flat window (no movement) -> NaN (treated as "no signal").
    """
    s = pd.Series(values, dtype="float64")
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.to_numpy()
