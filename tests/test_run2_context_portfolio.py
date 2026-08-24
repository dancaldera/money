from __future__ import annotations

import numpy as np
import pandas as pd

from trading.run2 import context
from trading.run2.context import (
    ContextCollector,
    ContextError,
    FinBertScorer,
    compute_regime_scores,
    fetch_sec_filings,
    robust_z,
)
import pytest
from trading.run2.portfolio import (
    block_bootstrap_mean_ci,
    deflated_sharpe_probability,
    primary_benchmark,
    simulate_portfolio,
)

from .run2_helpers import initialized_ledger, ohlcv, run2_config


def test_regime_scores_are_deterministic_on_synthetic_history():
    cfg = run2_config()
    days = 320
    base = np.linspace(100, 200, days)
    frames = {symbol: ohlcv(base * (1 + i / 100)) for i, (symbol, _asset) in enumerate(cfg.symbols)}
    frames["SPY"] = ohlcv(base)
    vix = pd.Series(np.r_[np.linspace(30, 20, days - 1), 10], index=frames["SPY"].index)
    oas = pd.Series(np.r_[np.linspace(8, 5, days - 1), 2], index=frames["SPY"].index)
    scores = compute_regime_scores(cfg, frames, vix, oas)
    assert scores["equity"]["value"] == 4
    assert 0 <= scores["crypto"]["value"] <= 4
    assert set(scores["crypto"]["flags"]) == {
        "btc_above_sma200", "watchlist_breadth", "btc_vol_not_extreme", "correlation_not_extreme"
    }


def test_news_collection_deduplicates_articles_and_stores_shadow_feature(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    article = {
        "headline": "Apple demand weakens",
        "summary": "Analyst cuts forecast",
        "url": "https://example.test/a",
        "created_at": "2026-08-24T00:00:00+00:00",
        "symbols": ["AAPL"],
    }
    try:
        collector = ContextCollector(cfg, ledger, tmp_path / "raw")
        loads = collector.collect_news(
            [article, dict(article)],
            scorer=lambda _text: {"negative": 0.8, "positive": 0.1, "neutral": 0.1},
            now=pd.Timestamp("2026-08-24T01:00:00Z").to_pydatetime(),
        )
        assert loads["AAPL"] == 0.8
        feature = ledger.latest_feature(cfg.run_id, "news", "negative_news_z", "AAPL")
        assert feature is not None
        assert len(list((tmp_path / "raw").rglob("*.json"))) > 0
    finally:
        ledger.close()


def test_finbert_wrapper_normalizes_transformers_output_shapes():
    scorer = FinBertScorer.__new__(FinBertScorer)
    scorer.pipe = lambda _text: [[
        {"label": "negative", "score": 0.7},
        {"label": "positive", "score": 0.2},
        {"label": "neutral", "score": 0.1},
    ]]
    assert scorer("headline")["negative"] == 0.7


def test_sec_filings_use_allowlist_acceptance_time_and_official_url(monkeypatch):
    cfg = run2_config()
    ticker_payload = {"0": {"ticker": "AAPL", "cik_str": 320193}}
    submission = {
        "name": "Apple Inc.",
        "filings": {"recent": {
            "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
            "form": ["8-K", "4"],
            "acceptanceDateTime": ["2026-08-24T12:00:00-04:00", "2026-08-24T13:00:00-04:00"],
            "filingDate": ["2026-08-24", "2026-08-24"],
            "reportDate": ["2026-08-24", "2026-08-24"],
            "primaryDocument": ["aapl-8k.htm", "xslF345X05/form4.xml"],
        }},
    }
    monkeypatch.setattr(
        context,
        "_sec_json",
        lambda url, _ua: ticker_payload if "company_tickers" in url else submission,
    )
    filings = fetch_sec_filings(
        cfg,
        start=pd.Timestamp("2026-08-24T00:00:00Z").to_pydatetime(),
        user_agent="money test@example.com",
    )
    assert len(filings) == 1 and filings[0]["form"] == "8-K"
    assert filings[0]["accepted_at"].endswith("+00:00")
    assert filings[0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")


def test_sec_requires_declared_contact(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ContextError, match="SEC_USER_AGENT"):
        fetch_sec_filings(run2_config(), user_agent="anonymous bot")


def test_robust_news_z_is_resistant_to_single_outlier():
    history = pd.Series([1, 1.1, 0.9, 1.0, 1.2, 50])
    assert robust_z(2.0, history) > 2


def test_simulator_expires_an_adverse_next_bar_gap():
    cfg = run2_config()
    close = [10.0] * 30 + [9.0, 20.0, 20.0]
    opens = close.copy()
    opens[-1] = 21.0  # 5% above the 20 signal close; stock cap is 2%.
    sim = simulate_portfolio(cfg, {"AAPL": ohlcv(close, opens=opens)})
    assert "expired_gap" in set(sim.decisions["action"])
    assert sim.trades.empty


def test_simulator_counts_pending_entries_against_crypto_cap():
    cfg = run2_config()
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0])
    frames = {symbol: frame for symbol in cfg.crypto_symbols}
    sim = simulate_portfolio(cfg, frames)
    buys = sim.trades[sim.trades["side"] == "buy"]
    assert len(buys) == cfg.portfolio.max_crypto_positions
    assert buys["fee"].sum() > 0


def test_simulator_completed_trade_pnl_includes_entry_and_exit_costs():
    cfg = run2_config()
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0])
    frame.loc[frame.index[-1], "Low"] = 10.0
    sim = simulate_portfolio(cfg, {"AAPL": frame})
    sell = sim.trades[sim.trades["side"] == "sell"].iloc[0]
    assert sell["reason"] == "stop"
    assert sell["realized_pl"] < -50.0  # $50 gross stop loss plus both-side costs.


def test_benchmark_accepts_utc_run_start_with_naive_bar_indexes():
    cfg = run2_config()
    frames = {symbol: ohlcv(np.linspace(100, 120, 90)) for symbol, _asset in cfg.symbols}
    benchmark = primary_benchmark(cfg, frames, start="2026-01-05T00:00:00+00:00")
    assert not benchmark.empty and benchmark.index.tz is None
    assert benchmark.iloc[-1] > cfg.starting_equity


def test_uncertainty_statistics_are_bounded_and_reproducible():
    values = pd.Series([1, -0.5, 2, 0.5, -1, 1.5])
    assert block_bootstrap_mean_ci(values, samples=100, seed=3) == block_bootstrap_mean_ci(
        values, samples=100, seed=3
    )
    probability = deflated_sharpe_probability(values / 100, trials=3)
    assert 0 <= probability <= 1
