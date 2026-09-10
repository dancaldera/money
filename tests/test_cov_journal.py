"""Coverage for trading.reporting.journal."""

from __future__ import annotations

import csv

import pandas as pd
import pytest

from trading.reporting import journal as journal_mod
from trading.reporting.journal import record_paper_action, record_run, summarize


@pytest.fixture
def tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(journal_mod, "JOURNAL_PATH", tmp_path / "journal.csv")
    monkeypatch.setattr(journal_mod, "PAPER_JOURNAL_PATH", tmp_path / "paper_journal.csv")
    return tmp_path


def test_summarize_tolerates_missing_keys():
    out = summarize({})
    assert out["trades"] == 0 and out["sharpe"] == 0.0
    out = summarize(pd.Series({"Return [%]": float("nan")}))
    assert out["return_pct"] == 0.0


def test_record_run_appends_with_header_once(tmp_journal):
    stats = pd.Series({
        "# Trades": 5, "Win Rate [%]": 60.0, "Return [%]": 12.5,
        "Buy & Hold Return [%]": 8.0, "Max. Drawdown [%]": -3.2,
        "Sharpe Ratio": 1.234, "Equity Final [$]": 11250.0,
    })
    row1 = record_run({"symbol": "AAPL", "asset": "stock", "strategy": "sma_cross",
                       "timeframe": "1d", "since": "2022-01-01"}, stats)
    row2 = record_run({}, stats)  # missing meta -> empty strings
    assert row1["symbol"] == "AAPL" and row2["symbol"] == ""
    with (tmp_journal / "journal.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["trades"] == "5"


def test_record_paper_action_appends(tmp_journal):
    row = record_paper_action({"symbol": "AAPL", "asset": "stock", "strategy": "sma_cross",
                               "signal": "BUY", "holding": False, "action": "bought",
                               "order_id": None, "last_price": 150.0})
    assert row["order_id"] == ""
    record_paper_action({})
    with (tmp_journal / "paper_journal.csv").open() as f:
        assert len(list(csv.DictReader(f))) == 2
