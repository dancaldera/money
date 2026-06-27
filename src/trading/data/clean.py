"""Shared cleaning for fetched OHLCV frames."""

from __future__ import annotations

from datetime import datetime, timezone

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


def truncation_warning(df: pd.DataFrame, since: str | None, symbol: str, tolerance_days: int = 7) -> str | None:
    """Return a message if the data starts much later than ``since``, else None.

    Some sources silently cap how far back they serve (e.g. Kraken returns only
    ~720 daily candles). When the earliest bar is well after the requested start,
    the history is truncated and the caller should be told rather than backtest
    on a shorter window than it thinks.
    """
    if df.empty or not since:
        return None
    requested = pd.Timestamp(since)
    actual = pd.Timestamp(df.index.min())
    if actual.tzinfo is not None:
        actual = actual.tz_convert("UTC").tz_localize(None)
    gap_days = (actual - requested).days
    if gap_days > tolerance_days:
        return (
            f"{symbol}: requested history since {requested.date()} but data starts "
            f"{actual.date()} ({gap_days} days later) — source is capping history."
        )
    return None


def drop_forming_bar(df: pd.DataFrame, timeframe: str = "1d", now: datetime | None = None) -> pd.DataFrame:
    """Drop the last bar if its period has not closed yet (UTC).

    Live signals should decide on the last *completed* bar, matching the backtest
    which only acts on closed bars. Continuous markets (crypto) always return a
    still-forming current-period candle; trading on it makes the signal flicker
    between runs. ``now`` is injectable for testing.
    """
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is not None:
        now_ts = now_ts.tz_convert("UTC").tz_localize(None)

    last = pd.Timestamp(df.index[-1])
    if last.tzinfo is not None:
        last = last.tz_convert("UTC").tz_localize(None)

    unit = "h" if timeframe == "1h" else "D"
    forming = last.floor(unit) == now_ts.floor(unit)
    return df.iloc[:-1] if forming else df
