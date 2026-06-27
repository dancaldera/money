"""Tests for the shared OHLCV cleaner used by both data fetchers."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading.data.clean import drop_forming_bar, drop_incomplete_rows, truncation_warning

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _daily(dates: list[str]) -> pd.DataFrame:
    idx = pd.to_datetime(dates)
    return pd.DataFrame({c: np.arange(len(idx), dtype=float) for c in _COLUMNS}, index=idx)


def test_drops_rows_with_nan_ohlc():
    # Last row mimics a partial/in-progress bar: volume present, OHLC missing.
    df = pd.DataFrame(
        {
            "Open": [1.0, 2.0, np.nan],
            "High": [1.0, 2.0, np.nan],
            "Low": [1.0, 2.0, np.nan],
            "Close": [1.0, 2.0, np.nan],
            "Volume": [10.0, 20.0, 30.0],
        }
    )
    out = drop_incomplete_rows(df, _COLUMNS, "X")
    assert len(out) == 2
    assert not out.isna().any().any()


def test_keeps_complete_rows_untouched():
    df = pd.DataFrame({c: [1.0, 2.0] for c in _COLUMNS})
    assert len(drop_incomplete_rows(df, _COLUMNS, "X")) == 2


def test_raises_when_everything_incomplete():
    df = pd.DataFrame({c: [np.nan, np.nan] for c in _COLUMNS})
    with pytest.raises(RuntimeError):
        drop_incomplete_rows(df, _COLUMNS, "X")


# --- drop_forming_bar ------------------------------------------------------

_NOW = datetime(2026, 6, 27, 17, 30, tzinfo=timezone.utc)


def test_forming_daily_bar_dropped():
    df = _daily(["2026-06-25", "2026-06-26", "2026-06-27"])  # last bar is "today"
    out = drop_forming_bar(df, "1d", now=_NOW)
    assert len(out) == 2
    assert out.index[-1] == pd.Timestamp("2026-06-26")


def test_closed_daily_bar_kept():
    df = _daily(["2026-06-25", "2026-06-26"])  # last bar already closed (yesterday)
    out = drop_forming_bar(df, "1d", now=_NOW)
    assert len(out) == 2


def test_forming_hourly_bar_dropped():
    idx = pd.to_datetime(["2026-06-27 15:00", "2026-06-27 16:00", "2026-06-27 17:00"])
    df = pd.DataFrame({c: [1.0, 2.0, 3.0] for c in _COLUMNS}, index=idx)
    out = drop_forming_bar(df, "1h", now=_NOW)  # 17:30 -> 17:00 bar still forming
    assert out.index[-1] == pd.Timestamp("2026-06-27 16:00")


def test_empty_frame_is_returned_unchanged():
    empty = pd.DataFrame({c: [] for c in _COLUMNS})
    assert drop_forming_bar(empty, "1d", now=_NOW).empty


# --- truncation_warning ----------------------------------------------------

def test_truncation_warning_when_history_capped():
    df = _daily(["2024-07-07", "2024-07-08"])
    msg = truncation_warning(df, "2022-01-01", "BTC/USD")
    assert msg is not None and "capping history" in msg


def test_no_truncation_warning_when_history_complete():
    df = _daily(["2022-01-03", "2022-01-04"])
    assert truncation_warning(df, "2022-01-01", "AAPL") is None


def test_no_truncation_warning_without_since_or_data():
    assert truncation_warning(_daily(["2024-01-01"]), None, "X") is None
    assert truncation_warning(_daily([]), "2022-01-01", "X") is None
