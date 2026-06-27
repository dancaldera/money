"""Offline tests for the equity fetcher's NaN handling (yf.download is mocked)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.data import equities
from trading.data.equities import fetch_equity

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _yf_frame(rows: int, trailing_nan: bool) -> pd.DataFrame:
    """Mimic a yfinance daily download, optionally with an incomplete last bar."""
    idx = pd.date_range("2022-01-01", periods=rows, freq="D")
    data = {c: np.linspace(100, 200, rows) for c in _COLUMNS}
    df = pd.DataFrame(data, index=idx)
    if trailing_nan:
        # yfinance appends a current-session bar with volume but NaN OHLC.
        df.loc[idx[-1], ["Open", "High", "Low", "Close"]] = np.nan
    return df


def test_fetch_equity_drops_incomplete_trailing_bar(monkeypatch):
    monkeypatch.setattr(equities.yf, "download", lambda *a, **k: _yf_frame(30, trailing_nan=True))
    df = fetch_equity("AAPL")
    assert len(df) == 29  # the NaN bar is gone
    assert not df.isna().any().any()


def test_fetch_equity_keeps_complete_data(monkeypatch):
    monkeypatch.setattr(equities.yf, "download", lambda *a, **k: _yf_frame(30, trailing_nan=False))
    df = fetch_equity("AAPL")
    assert len(df) == 30


def test_fetch_equity_raises_when_all_rows_incomplete(monkeypatch):
    bad = _yf_frame(5, trailing_nan=False)
    bad[["Open", "High", "Low", "Close"]] = np.nan
    monkeypatch.setattr(equities.yf, "download", lambda *a, **k: bad)
    with pytest.raises(RuntimeError):
        fetch_equity("AAPL")
