"""Derive a single live trade signal from recent bars.

These mirror the backtest strategies in ``trading.strategies`` and reuse the same
indicator helpers and parameter defaults, so live behaviour stays consistent with
what you backtested. Each function looks only at the most recent bar(s) and
returns one of: "BUY", "SELL", or "HOLD". The trader reconciles the signal with
the current position (e.g. it won't buy what it already holds).
"""

from __future__ import annotations

import pandas as pd

from ..strategies import RsiMeanReversion, SmaCross
from ..strategies.base import rsi, sma


def sma_cross_signal(df: pd.DataFrame) -> str:
    """BUY on a fresh fast-over-slow up-cross, SELL on the reverse cross."""
    if len(df) < SmaCross.n2 + 2:
        return "HOLD"
    close = df["Close"]
    s1 = pd.Series(sma(close, SmaCross.n1))
    s2 = pd.Series(sma(close, SmaCross.n2))
    up = s1.iloc[-2] <= s2.iloc[-2] and s1.iloc[-1] > s2.iloc[-1]
    down = s1.iloc[-2] >= s2.iloc[-2] and s1.iloc[-1] < s2.iloc[-1]
    return "BUY" if up else "SELL" if down else "HOLD"


def rsi_meanrev_signal(df: pd.DataFrame) -> str:
    """BUY when RSI is oversold, SELL when overbought."""
    if len(df) < RsiMeanReversion.period + 1:
        return "HOLD"
    last = pd.Series(rsi(df["Close"], RsiMeanReversion.period)).iloc[-1]
    if pd.isna(last):
        return "HOLD"
    if last < RsiMeanReversion.lower:
        return "BUY"
    if last > RsiMeanReversion.upper:
        return "SELL"
    return "HOLD"


SIGNALS = {
    "sma_cross": sma_cross_signal,
    "rsi_meanrev": rsi_meanrev_signal,
}


def get_signal_fn(name: str):
    try:
        return SIGNALS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown strategy '{name}'. Available: {', '.join(sorted(SIGNALS))}"
        )
