"""Alpaca daily OHLCV used by the frozen paper-trading run."""

from __future__ import annotations

from datetime import datetime, timezone
import os

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame


class AlpacaDataError(RuntimeError):
    """Raised when authenticated Run 2 market data is unavailable or malformed."""


def _credentials() -> tuple[str, str]:
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise AlpacaDataError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for Run 2 data")
    return key, secret


def _as_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def _frame(result: object, symbol: str) -> pd.DataFrame:
    frame = getattr(result, "df", None)
    if frame is None or frame.empty:
        raise AlpacaDataError(f"Alpaca returned no daily bars for {symbol}")
    frame = frame.copy()
    if isinstance(frame.index, pd.MultiIndex):
        try:
            frame = frame.xs(symbol, level="symbol")
        except (KeyError, ValueError):
            frame = frame.xs(symbol, level=0)
    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    frame = frame.rename(columns=rename)
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [column for column in required if column not in frame]
    if missing:
        raise AlpacaDataError(f"Alpaca bars for {symbol} are missing {missing}")
    frame = frame[required].apply(pd.to_numeric, errors="coerce").dropna()
    if frame.empty:
        raise AlpacaDataError(f"Alpaca returned no complete daily bars for {symbol}")
    index = pd.to_datetime(frame.index, utc=True).tz_localize(None)
    frame.index = pd.DatetimeIndex(index, name="Date")
    return frame.sort_index()


def fetch_alpaca_daily(
    symbol: str,
    asset: str,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
) -> pd.DataFrame:
    """Return adjusted stock or spot-crypto daily bars from Alpaca.

    Stocks use the IEX feed so the function works with Alpaca's basic data
    subscription. Crypto uses Alpaca's US spot feed. Both return the project's
    canonical OHLCV shape with a timezone-naive UTC index.
    """
    key, secret = _credentials()
    start = _as_utc(since)
    end = _as_utc(until) or datetime.now(timezone.utc)
    if asset == "stock":
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
            feed=DataFeed.IEX,
        )
        result = StockHistoricalDataClient(key, secret).get_stock_bars(request)
    elif asset == "crypto":
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        result = CryptoHistoricalDataClient(key, secret).get_crypto_bars(request)
    else:
        raise AlpacaDataError(f"Unknown asset {asset!r}")
    return _frame(result, symbol)
