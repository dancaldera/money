from __future__ import annotations

import pandas as pd

from trading.data.alpaca import _frame


def test_alpaca_multindex_bars_are_normalized_to_project_shape():
    index = pd.MultiIndex.from_product(
        [["AAPL"], pd.to_datetime(["2026-08-21T04:00:00Z", "2026-08-22T04:00:00Z"])],
        names=["symbol", "timestamp"],
    )
    class Result:
        df = pd.DataFrame(
            {"open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [10, 20]},
            index=index,
        )

    out = _frame(Result(), "AAPL")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert out.index.tz is None and out.index.name == "Date"
