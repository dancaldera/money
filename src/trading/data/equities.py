"""Fetch historical stock OHLCV candles via yfinance (free, no API key)."""

from __future__ import annotations

import pandas as pd

from .yahoo import download_ohlcv


def fetch_equity(
    symbol: str,
    timeframe: str = "1d",
    since: str | None = None,
) -> pd.DataFrame:
    """Return a DataFrame indexed by datetime with OHLCV columns for a stock ticker."""
    return download_ohlcv(symbol, timeframe, since, label=symbol)
