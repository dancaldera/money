"""Coverage for trading.run2.context gaps (no network: everything mocked)."""

from __future__ import annotations

import gzip
import io
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from trading.run2 import context as context_mod
from trading.run2.context import (
    ContextCollector,
    ContextError,
    FinBertScorer,
    _below_trailing_percentile,
    _breadth,
    _median_crypto_correlation,
    _sec_json,
    compute_regime_scores,
    fetch_alpaca_news,
    fetch_high_yield_oas,
    fetch_sec_filings,
    fetch_vix_history,
    robust_z,
)
from trading.run2.ledger import utc_now

from .run2_helpers import initialized_ledger, ohlcv, run2_config


# --- small helpers ------------------------------------------------------------- #
def test_percentile_requires_history():
    with pytest.raises(ContextError, match="60 observations"):
        _below_trailing_percentile(pd.Series(range(10)), 0.8)
    with pytest.raises(ContextError, match="No prior observations"):
        _below_trailing_percentile(pd.Series(range(100)), 0.8, window=0)


def test_breadth_skips_ineligible_and_requires_one():
    good = ohlcv(np.linspace(100, 200, 100))
    short = ohlcv([100.0, 101.0])
    assert _breadth({"A": short, "B": good}, ("A", "B", "MISSING")) in (0.0, 1.0)
    with pytest.raises(ContextError, match="No eligible symbols"):
        _breadth({"A": short}, ("A",))


def test_crypto_correlation_requires_aligned_data():
    with pytest.raises(ContextError, match="Insufficient aligned"):
        _median_crypto_correlation({"BTC/USD": ohlcv([100.0, 101.0])}, ("BTC/USD",))


def test_regime_scores_require_spy_and_btc_history():
    cfg = run2_config()
    with pytest.raises(ContextError, match="sufficient history"):
        compute_regime_scores(cfg, {"SPY": ohlcv([1.0, 2.0])},
                              pd.Series([1.0]), pd.Series([1.0]))


def test_fetch_vix_and_oas_parse_vendor_csvs(monkeypatch):
    vix_df = pd.DataFrame({"DATE": ["2026-01-01", "2026-01-02"], "CLOSE": [20.0, 21.0]})
    oas_df = pd.DataFrame({"observation_date": ["2026-01-01", "2026-01-02"],
                           "BAMLH0A0HYM2": [5.0, 5.1]})
    calls = {"n": 0}

    def fake_read_csv(url):
        calls["n"] += 1
        return vix_df if "VIX" in url else oas_df

    monkeypatch.setattr(pd, "read_csv", fake_read_csv)
    vix = fetch_vix_history()
    oas = fetch_high_yield_oas()
    assert calls["n"] == 2
    assert list(vix) == [20.0, 21.0] and vix.name == "VIX"
    assert list(oas) == [5.0, 5.1] and oas.name == "high_yield_oas"


def test_robust_z_edge_cases():
    with pytest.raises(ContextError, match="without history"):
        robust_z(1.0, pd.Series([], dtype=float))
    flat = pd.Series([1.0, 1.0, 1.0])
    assert robust_z(1.0, flat) == 0.0
    assert robust_z(2.0, flat) == 10.0
    assert robust_z(0.0, flat) == -10.0


def test_finbert_init_requires_optional_dependency():
    import importlib.util

    if importlib.util.find_spec("transformers") is not None:
        pytest.skip("transformers is installed here")
    with pytest.raises(ContextError, match="not installed"):
        FinBertScorer("m", "r")


def test_finbert_init_builds_pinned_pipeline(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("transformers")
    seen = {}

    def pipeline(task, **kwargs):
        seen.update(task=task, **kwargs)
        return "pipe"

    fake.pipeline = pipeline
    monkeypatch.setitem(sys.modules, "transformers", fake)
    scorer = FinBertScorer("the-model", "the-revision")
    assert scorer.pipe == "pipe"
    assert (seen["task"], seen["model"], seen["revision"]) == (
        "text-classification", "the-model", "the-revision")


def test_finbert_call_accepts_bare_dict():
    scorer = FinBertScorer.__new__(FinBertScorer)
    scorer.pipe = lambda _text: {"label": "NEGATIVE", "score": 0.9}
    assert scorer("x") == {"negative": 0.9}


# --- collect_regimes ------------------------------------------------------------ #
def test_collect_regimes_persists_macro_and_scores(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        days = 260
        base = np.linspace(100, 200, days)
        frames = {
            "SPY": ohlcv(base),
            "BTC/USD": ohlcv(base),
            "ETH/USD": ohlcv(base * 1.01),
            "AAPL": ohlcv(base * 1.02),
        }
        idx = frames["SPY"].index
        vix = pd.Series(np.r_[np.linspace(30, 20, days - 1), 10], index=idx)
        oas = pd.Series(np.r_[np.linspace(8, 5, days - 1), 2], index=idx)
        collector = ContextCollector(cfg, ledger, tmp_path / "raw")
        scores = collector.collect_regimes(frames, vix=vix, high_yield_oas=oas)
        assert set(scores) == {"equity", "crypto"}
        assert ledger.latest_feature(cfg.run_id, "macro", "vix_history") is not None
        assert ledger.latest_feature(cfg.run_id, "equity", "regime_score") is not None
    finally:
        ledger.close()


# --- collect_news: unknown symbols + deep history ------------------------------- #
def _seed_negative_loads(ledger, cfg, symbol, n, end="2026-08-23"):
    for i in range(n):
        day = (pd.Timestamp(end) - pd.Timedelta(days=n - 1 - i)).date().isoformat()
        ledger.record_feature(
            {
                "run_id": cfg.run_id,
                "scope": "news",
                "symbol": symbol,
                "observed_at": day,
                "captured_at": f"{day}T01:00:00+00:00",
                "source": "derived:finbert",
                "name": "negative_load",
                "value_json": json.dumps({"value": float(i % 7)}),
                "payload_hash": f"seed-{symbol}-{day}",
            }
        )


def test_collect_news_skips_unknown_symbols_and_scores_deep_history(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _seed_negative_loads(ledger, cfg, "AAPL", 65)
        collector = ContextCollector(cfg, ledger, tmp_path / "raw")
        articles = [
            {"headline": "Mystery coin moons", "summary": "", "url": "https://x.test/1",
             "created_at": "2026-08-24T00:00:00+00:00", "symbols": ["UNKNOWN"]},
            {"headline": "Apple steady", "summary": "", "url": "https://x.test/2",
             "created_at": "2026-08-24T00:00:00+00:00", "symbols": ["AAPL"]},
        ]
        loads = collector.collect_news(
            articles,
            scorer=lambda _t: {"negative": 0.5},
            now=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
        )
        assert loads["AAPL"] == 0.5
        feature = ledger.latest_feature(cfg.run_id, "news", "negative_news_z", "AAPL")
        assert json.loads(feature["value_json"])["observations"] == 60
    finally:
        ledger.close()


# --- collect_sec_filings --------------------------------------------------------- #
def test_collect_sec_filings_counts_and_skips_outside_universe(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        collector = ContextCollector(cfg, ledger, tmp_path / "raw")
        counts = collector.collect_sec_filings([
            {"symbol": "AAPL", "form": "8-K", "accession_number": "0001",
             "url": "https://sec/a", "accepted_at": "2026-08-24T12:00:00+00:00"},
            {"symbol": "SPY", "form": "8-K", "accession_number": "0002",
             "url": "https://sec/b", "accepted_at": "2026-08-24T12:00:00+00:00"},
        ])
        assert counts["AAPL"] == 1 and counts["MSFT"] == 0
        assert ledger.latest_feature(cfg.run_id, "sec", "filing", "AAPL") is not None
        assert ledger.latest_feature(cfg.run_id, "sec", "filing_count", "AAPL") is not None
    finally:
        ledger.close()


# --- _sec_json ------------------------------------------------------------------- #
def _urlopen_response(payload: bytes, encoding: str | None = None):
    response = MagicMock()
    response.read.return_value = payload
    response.headers.get.return_value = encoding
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_sec_json_handles_plain_gzip_and_malformed(monkeypatch):
    monkeypatch.setattr(context_mod, "urlopen",
                        lambda *a, **k: _urlopen_response(b'{"a": 1}'))
    assert _sec_json("https://x", "ua") == {"a": 1}
    monkeypatch.setattr(context_mod, "urlopen",
                        lambda *a, **k: _urlopen_response(gzip.compress(b'{"b": 2}'), "gzip"))
    assert _sec_json("https://x", "ua") == {"b": 2}
    monkeypatch.setattr(context_mod, "urlopen",
                        lambda *a, **k: _urlopen_response(b"[1, 2]"))
    with pytest.raises(ContextError, match="malformed JSON"):
        _sec_json("https://x", "ua")


# --- fetch_sec_filings branches ---------------------------------------------------- #
def _sec_submission(forms, acceptances, documents=None):
    n = len(forms)
    return {
        "name": "Apple Inc.",
        "filings": {"recent": {
            "accessionNumber": [f"0000320193-26-00000{i}" for i in range(n)],
            "form": forms,
            "acceptanceDateTime": acceptances,
            "filingDate": ["2026-08-24"] * n,
            "reportDate": ["2026-08-24"] * n,
            "primaryDocument": documents if documents is not None else ["doc.htm"] * n,
        }},
    }


def test_sec_filings_filters_stale_and_tolerates_ragged_rows(monkeypatch):
    cfg = run2_config()
    tickers = {"0": {"ticker": "AAPL", "cik_str": 320193}}
    submission = _sec_submission(
        ["8-K", "8-K"],
        ["2026-08-20T12:00:00", "2026-08-24T12:00:00"],  # naive ET timestamps
        documents=["only-one.htm"],  # second row is ragged -> skipped
    )
    monkeypatch.setattr(context_mod, "_sec_json",
                        lambda url, _ua: tickers if "company_tickers" in url else submission)
    monkeypatch.setattr(context_mod.time, "sleep", lambda *_a: None)
    filings = fetch_sec_filings(cfg, start=datetime(2026, 8, 24, 0, 0),  # naive start
                                user_agent="money test@example.com")
    assert filings == []  # stale one filtered, fresh one ragged
    submission["filings"]["recent"]["primaryDocument"] = ["a.htm", "b.htm"]
    filings = fetch_sec_filings(cfg, start=datetime(2026, 8, 24, 0, 0),
                                user_agent="money test@example.com")
    assert len(filings) == 1 and filings[0]["accepted_at"].endswith("+00:00")


# --- fetch_alpaca_news ------------------------------------------------------------- #
class _FakeNewsClient:
    pages = []

    def __init__(self, *args, **kwargs):
        self.calls = 0

    def get_news(self, request):
        page = _FakeNewsClient.pages[min(self.calls, len(_FakeNewsClient.pages) - 1)]
        self.calls += 1
        return page


def test_fetch_alpaca_news_paginates_and_stops(monkeypatch):
    import alpaca.data.historical.news as news_mod

    monkeypatch.setattr(news_mod, "NewsClient", _FakeNewsClient)
    _FakeNewsClient.pages = [
        {"news": [{"headline": "a"}], "next_page_token": "t1"},
        {"news": [{"headline": "b"}], "next_page_token": None},
    ]
    articles = fetch_alpaca_news(run2_config())
    assert [a["headline"] for a in articles] == ["a", "b"]


def test_fetch_alpaca_news_stops_on_repeat_token_or_raw_list(monkeypatch):
    import alpaca.data.historical.news as news_mod

    monkeypatch.setattr(news_mod, "NewsClient", _FakeNewsClient)
    _FakeNewsClient.pages = [
        {"news": [{"headline": "a"}], "next_page_token": "t1"},
        {"news": [{"headline": "b"}], "next_page_token": "t1"},
    ]
    assert len(fetch_alpaca_news(run2_config())) == 2
    _FakeNewsClient.pages = [[{"headline": "raw"}]]
    assert fetch_alpaca_news(run2_config()) == []
