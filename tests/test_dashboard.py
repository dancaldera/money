"""Tests for the local HTML dashboard builder (no network, no broker)."""

from __future__ import annotations

import pandas as pd
import pytest

from trading.reporting import dashboard


@pytest.fixture
def empty_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(dashboard, "DATA_DIR", tmp_path / "data")
    (tmp_path / "results").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def test_readers_are_graceful_when_files_missing(empty_dirs):
    assert dashboard._backtests("sma_cross") == []
    assert dashboard._coverage() == []
    assert dashboard._activity() == {"counts": {}, "last_run": [], "last_when": None}
    # Heartbeats always return one entry per job, flagged not-ok when absent.
    hbs = dashboard._heartbeats()
    assert len(hbs) == 4 and all(h["ok"] is False for h in hbs)


def test_backtests_picks_latest_per_symbol(empty_dirs):
    df = pd.DataFrame([
        {"timestamp": "2026-01-01T00:00:00", "symbol": "AAPL", "strategy": "sma_cross",
         "return_pct": 10.0, "buy_hold_pct": 4.0, "sharpe": 0.5},
        {"timestamp": "2026-02-01T00:00:00", "symbol": "AAPL", "strategy": "sma_cross",
         "return_pct": 20.0, "buy_hold_pct": 5.0, "sharpe": 0.6},  # newer
    ])
    df.to_csv(dashboard.RESULTS_DIR / "journal.csv", index=False)
    bts = dashboard._backtests("sma_cross")
    assert len(bts) == 1
    assert bts[0]["return_pct"] == 20.0
    assert bts[0]["alpha"] == 15.0  # 20 - 5


def test_render_page_contains_all_sections():
    ctx = {
        "generated": "2026-06-27 00:00 UTC",
        "strategy": "sma_cross",
        "heartbeats": [{"label": "Daily signal scan", "when": "now", "age_h": 1.0, "ok": True}],
        "account": None,
        "backtests": [{"symbol": "AAPL", "return_pct": 20.0, "buy_hold_pct": 5.0, "alpha": 15.0, "sharpe": 0.6}],
        "coverage": [{"name": "stock_AAPL", "rows": 1000, "first": "2022-01-03", "last": "2026-06-25", "nans": 0, "age_h": 1.0}],
        "activity": {"counts": {"bought": 3}, "last_run": [{"symbol": "AAPL", "signal": "HOLD", "holding": True, "action": "none"}], "last_when": "2026-06-26 23:00"},
        "insights": ["test insight"],
    }
    html = dashboard.render_page(ctx)
    assert html.startswith("<!DOCTYPE html>")
    for marker in ["Paper account", "System health", "Backtests", "Insights", "Data coverage", "Last scan", "sma_cross"]:
        assert marker in html
