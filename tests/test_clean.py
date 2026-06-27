"""Tests for the shared OHLCV cleaner used by both data fetchers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.data.clean import drop_incomplete_rows

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


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
