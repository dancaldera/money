"""Fetch historical stock OHLCV candles via yfinance (free, no API key)."""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from .clean import drop_incomplete_rows

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def fetch_equity(
    symbol: str,
    timeframe: str = "1d",
    since: str | None = None,
) -> pd.DataFrame:
    """Return a DataFrame indexed by datetime with OHLCV columns for a stock ticker."""
    df = yf.download(
        symbol,
        start=since,
        interval=timeframe,
        auto_adjust=True,
        progress=False,
        multi_level_index=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for ticker '{symbol}'")

    # Older/newer yfinance may still return a column MultiIndex for one ticker.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[_COLUMNS].copy()
    df.index.name = "Date"

    # yfinance often appends a current-session bar with volume but NaN OHLC.
    df = drop_incomplete_rows(df, _COLUMNS, symbol)
    return df.astype(float)
