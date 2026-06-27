"""Fetch historical crypto OHLCV candles via yfinance (free, no API key).

Uses yfinance BASE-QUOTE tickers (e.g. ``BTC-USD``), which provide deep daily
history back to each coin's inception — unlike exchange OHLCV endpoints (e.g.
Kraken) that cap responses at a few hundred candles.
"""

from __future__ import annotations

import pandas as pd

from .yahoo import download_ohlcv


def _yf_symbol(symbol: str) -> str:
    """Map a config symbol to a yfinance ticker: ``BTC/USD`` -> ``BTC-USD``."""
    return symbol.replace("/", "-").upper()


def fetch_crypto(
    symbol: str,
    timeframe: str = "1d",
    since: str | None = None,
) -> pd.DataFrame:
    """Return a DataFrame indexed by datetime with OHLCV columns for a crypto pair."""
    return download_ohlcv(_yf_symbol(symbol), timeframe, since, label=symbol)
