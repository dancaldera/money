"""Shared yfinance OHLCV download used by both the equity and crypto fetchers.

yfinance is free, needs no API key, and serves deep daily history (back to each
instrument's inception) for both stocks and crypto, so both asset classes use it.
"""

from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

from .clean import drop_incomplete_rows, truncation_warning

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def download_ohlcv(ticker: str, timeframe: str = "1d", since: str | None = None, label: str | None = None) -> pd.DataFrame:
    """Return a clean OHLCV DataFrame for ``ticker`` indexed by datetime.

    ``label`` is the human-facing name used in messages (e.g. ``BTC/USD`` for a
    ``BTC-USD`` ticker); it defaults to ``ticker``.
    """
    label = label or ticker
    df = yf.download(
        ticker,
        start=since,
        interval=timeframe,
        auto_adjust=True,
        progress=False,
        multi_level_index=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for '{label}' ({ticker})")

    # Older/newer yfinance may still return a column MultiIndex for one ticker.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[_COLUMNS].copy()
    df.index.name = "Date"
    # yfinance often appends a current-session bar with volume but NaN OHLC.
    df = drop_incomplete_rows(df, _COLUMNS, label)

    msg = truncation_warning(df, since, label)
    if msg:
        print(f"WARN  {msg}", file=sys.stderr)

    return df.astype(float)
