"""Offline tests for the yfinance-backed fetchers (yf.download is mocked)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.data import yahoo
from trading.data.crypto import _yf_symbol, fetch_crypto
from trading.data.equities import fetch_equity

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _yf_frame(rows: int, trailing_nan: bool = False, start: str = "2022-01-01") -> pd.DataFrame:
    """Mimic a yfinance daily download, optionally with an incomplete last bar."""
    idx = pd.date_range(start, periods=rows, freq="D")
    df = pd.DataFrame({c: np.linspace(100, 200, rows) for c in _COLUMNS}, index=idx)
    if trailing_nan:
        df.loc[idx[-1], ["Open", "High", "Low", "Close"]] = np.nan
    return df


def _patch(monkeypatch, frame):
    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: frame)


def test_fetch_equity_drops_incomplete_trailing_bar(monkeypatch):
    _patch(monkeypatch, _yf_frame(30, trailing_nan=True))
    df = fetch_equity("AAPL")
    assert len(df) == 29  # the NaN bar is gone
    assert not df.isna().any().any()


def test_fetch_equity_keeps_complete_data(monkeypatch):
    _patch(monkeypatch, _yf_frame(30))
    assert len(fetch_equity("AAPL")) == 30


def test_fetch_equity_raises_when_all_rows_incomplete(monkeypatch):
    bad = _yf_frame(5)
    bad[["Open", "High", "Low", "Close"]] = np.nan
    _patch(monkeypatch, bad)
    with pytest.raises(RuntimeError):
        fetch_equity("AAPL")


def test_crypto_symbol_mapping():
    assert _yf_symbol("BTC/USD") == "BTC-USD"
    assert _yf_symbol("eth/usd") == "ETH-USD"


def test_fetch_crypto_uses_mapped_ticker(monkeypatch):
    seen = {}

    def fake_download(ticker, *a, **k):
        seen["ticker"] = ticker
        return _yf_frame(40)

    monkeypatch.setattr(yahoo.yf, "download", fake_download)
    df = fetch_crypto("BTC/USD")
    assert seen["ticker"] == "BTC-USD"
    assert len(df) == 40


def test_truncation_warning_emitted(monkeypatch, capsys):
    # Data starts well after the requested since -> a WARN should hit stderr.
    _patch(monkeypatch, _yf_frame(30, start="2024-07-07"))
    fetch_crypto("BTC/USD", since="2022-01-01")
    assert "capping history" in capsys.readouterr().err
