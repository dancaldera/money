"""Shared cleaning for fetched OHLCV frames."""

from __future__ import annotations

import pandas as pd


def drop_incomplete_rows(df: pd.DataFrame, columns: list[str], symbol: str) -> pd.DataFrame:
    """Drop rows with any NaN in ``columns`` and guard against an empty result.

    Data providers append in-progress or partial bars with missing OHLC values
    (e.g. yfinance's current-session row). backtesting.py rejects any NaN, so
    callers strip incomplete rows before returning.
    """
    df = df.dropna(subset=columns)
    if df.empty:
        raise RuntimeError(f"No complete OHLC rows for '{symbol}'")
    return df
