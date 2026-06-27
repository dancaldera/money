"""Fetch historical crypto OHLCV candles via ccxt (public data, no API key)."""

from __future__ import annotations

import time

import ccxt
import pandas as pd

# Columns backtesting.py expects.
_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def fetch_crypto(
    symbol: str,
    timeframe: str = "1d",
    since: str | None = None,
    exchange: str = "kraken",
    limit: int = 720,
) -> pd.DataFrame:
    """Return a DataFrame indexed by datetime with OHLCV columns.

    Paginates through the exchange's history starting at ``since`` (YYYY-MM-DD).
    Uses only public market data — no authentication, no orders.
    """
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    if not ex.has.get("fetchOHLCV"):
        raise RuntimeError(f"Exchange '{exchange}' does not expose OHLCV data")

    since_ms = ex.parse8601(f"{since}T00:00:00Z") if since else None
    rows: list[list] = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
        if not batch:
            break
        # Drop the overlap candle that pagination repeats.
        if rows and batch[0][0] == rows[-1][0]:
            batch = batch[1:]
        rows.extend(batch)
        if len(batch) < limit - 1:
            break
        since_ms = batch[-1][0] + 1
        time.sleep(ex.rateLimit / 1000)

    if not rows:
        raise RuntimeError(f"No candles returned for {symbol} on {exchange}")

    df = pd.DataFrame(rows, columns=["ts", *(_COLUMNS)])
    df["Date"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.drop(columns="ts").set_index("Date").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df[_COLUMNS].astype(float)
