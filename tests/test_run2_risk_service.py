from __future__ import annotations

import pandas as pd
import pytest

from trading.run2.risk import check_entry, correlation_matches
from trading.run2.ledger import utc_now
from trading.run2.service import Run2Service
from trading.run2.service import RunSafetyError

from .run2_helpers import add_fill, fresh_buy_frame, initialized_ledger, ohlcv


class FakeBroker:
    def __init__(self, *, current=100.0, equity=10_000.0):
        self.current = current
        self.equity = equity
        self.closed = []
        self.buys = []

    def latest_price(self, symbol, asset):
        return self.current

    def clock(self):
        return {"is_open": True}

    def buy_limit(self, symbol, notional, asset, limit_price, client_order_id):
        self.buys.append((symbol, limit_price))
        return {"id": "order-1", "status": "accepted", "submitted_at": "2026-08-24T00:00:00Z"}

    def close(self, symbol):
        self.closed.append(symbol)
        return "close-1"

    def positions(self):
        return []

    def account(self):
        return {"id": "paper", "equity": self.equity, "cash": self.equity, "buying_power": self.equity}

    def all_orders(self, after=None):
        return []


def _feature(ledger, cfg, scope, name, value, symbol=None):
    observations = ', "observations": 31' if name == "negative_news_z" else ""
    ledger.record_feature(
        {
            "run_id": cfg.run_id,
            "scope": scope,
            "symbol": symbol,
            "observed_at": "2026-08-23T00:00:00+00:00",
            "captured_at": utc_now(),
            "source": "test",
            "name": name,
            "value_json": f'{{"value": {value}{observations}}}',
            "payload_hash": f"{scope}-{name}-{symbol}-{value}",
        }
    )


def test_scan_records_baseline_and_both_shadow_arms(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _feature(ledger, cfg, "equity", "regime_score", 4)
        _feature(ledger, cfg, "news", "negative_news_z", 0, "AAPL")
        service = Run2Service(cfg, ledger)
        rows = service.scan({"AAPL": fresh_buy_frame()})
        aapl = [row for row in rows if row["symbol"] == "AAPL"]
        assert {row["portfolio"] for row in aapl} == set(cfg.research.arms)
        assert all(row["action"] == "buy_intent" for row in aapl)
        assert len(ledger.decisions(cfg.run_id)) == 3
    finally:
        ledger.close()


def test_initialization_requires_a_clean_funded_paper_account(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    ledger.close()
    # Use a fresh uninitialized file because the helper intentionally initializes one.
    from trading.run2.ledger import RunLedger

    clean_ledger = RunLedger(tmp_path / "fresh.sqlite")
    broker = FakeBroker(equity=cfg.starting_equity)
    broker.all_orders = lambda after=None: [{"id": "old-order"}]
    try:
        with pytest.raises(RunSafetyError, match="no order history"):
            Run2Service(cfg, clean_ledger, broker).initialize()
        assert clean_ledger.run(cfg.run_id) is None
    finally:
        clean_ledger.close()


def test_dry_run_scan_never_mutates_ledger(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()}, record=False)
        assert ledger.decisions(cfg.run_id) == []
    finally:
        ledger.close()


def test_news_shadow_waits_for_prospective_warmup(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _feature(ledger, cfg, "equity", "regime_score", 4)
        ledger.record_feature(
            {
                "run_id": cfg.run_id,
                "scope": "news",
                "symbol": "AAPL",
                "observed_at": "2026-08-23T00:00:00+00:00",
                "captured_at": utc_now(),
                "source": "test",
                "name": "negative_news_z",
                "value_json": '{"value": 0, "observations": 5}',
                "payload_hash": "warmup",
            }
        )
        rows = Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        news = next(row for row in rows if row["portfolio"] == "shadow_regime_news" and row["symbol"] == "AAPL")
        assert news["action"] == "none" and news["reason"] == "news_warmup:5"
    finally:
        ledger.close()


def test_stale_regime_snapshot_is_never_reused(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        ledger.record_feature(
            {
                "run_id": cfg.run_id,
                "scope": "equity",
                "symbol": None,
                "observed_at": "2020-01-01T00:00:00+00:00",
                "captured_at": "2020-01-01T00:00:00+00:00",
                "source": "test",
                "name": "regime_score",
                "value_json": '{"value": 4}',
                "payload_hash": "stale",
            }
        )
        rows = Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        regime = next(row for row in rows if row["portfolio"] == "shadow_regime" and row["symbol"] == "AAPL")
        assert regime["action"] == "none" and regime["reason"] == "missing_regime"
    finally:
        ledger.close()


def test_adverse_gap_expires_without_submitting(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker(current=103.0)
    try:
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        out = Run2Service(cfg, ledger, broker).execute_pending("stock")
        assert out[0]["action"] == "expired_gap"
        assert broker.buys == []
    finally:
        ledger.close()


def test_halt_invalidates_a_preexisting_buy_intent(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker(current=100.0)
    try:
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        ledger.halt(cfg.run_id, "drawdown_halt")
        out = Run2Service(cfg, ledger, broker).execute_pending("stock")
        assert out[0]["action"] == "expired_halt"
        assert broker.buys == []
    finally:
        ledger.close()


def test_closed_stock_market_is_a_safe_deferral(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker()
    broker.clock = lambda: {"is_open": False}
    try:
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        assert Run2Service(cfg, ledger, broker).execute_pending("stock") == [
            {"action": "deferred_market_closed"}
        ]
        assert broker.buys == []
    finally:
        ledger.close()


def test_stop_uses_fill_derived_entry_not_broker_cost_basis(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker()
    broker.positions = lambda: [
        {"symbol": "AAPL", "qty": 6.25, "avg_entry": 0, "current_price": 91.9}
    ]
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "01")
        out = Run2Service(cfg, ledger, broker).check_stops()
        assert out[0]["action"] == "stopped"
        assert broker.closed == ["AAPL"]
    finally:
        ledger.close()


def test_correlation_cap_counts_only_sufficient_aligned_history(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 1, 100, "01")
        add_fill(ledger, cfg, "MSFT", "stock", "buy", 1, 100, "02")
        trend = ohlcv(range(100, 180))
        bars = {"AAPL": trend, "MSFT": trend * 2, "NVDA": trend * 3}
        held = ledger.positions(cfg.run_id)
        assert correlation_matches("NVDA", held, bars, 60, 0.8) == 2
        assert check_entry(cfg, ledger, "baseline", "NVDA", "stock", bars).reason == "correlation_cap:2"
    finally:
        ledger.close()


def test_reconcile_imports_fill_updates_order_and_measures_gap(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker(equity=cfg.starting_equity + 10)
    decision = {
        "decision_id": "decision-1",
        "run_id": cfg.run_id,
        "portfolio": "baseline",
        "symbol": "AAPL",
        "asset": "stock",
        "strategy": "sma_cross",
        "bar_end": "2026-08-23T00:00:00+00:00",
        "signal": "BUY",
        "signal_price": "100",
        "notional": "625",
        "action": "buy_intent",
        "status": "submitted",
        "config_hash": cfg.fingerprint,
    }
    ledger.record_decision(decision)
    ledger.record_order(
        {
            "run_id": cfg.run_id,
            "decision_id": "decision-1",
            "portfolio": "baseline",
            "client_order_id": "client-1",
            "broker_order_id": "order-1",
            "symbol": "AAPL",
            "asset": "stock",
            "side": "buy",
            "status": "accepted",
        }
    )
    broker.activities = lambda kind, after=None: ([{
        "id": "fill-1", "order_id": "order-1", "symbol": "AAPL", "side": "buy",
        "qty": "6.18811881", "price": "101", "transaction_time": "2026-08-24T14:00:00Z",
    }] if kind == "FILL" else [])
    broker.all_orders = lambda after=None: [{
        "id": "order-1", "status": "filled", "filled_qty": 6.18811881,
    }]
    broker.account = lambda: {
        "equity": cfg.starting_equity + 10,
        "cash": cfg.starting_equity - 625,
        "buying_power": cfg.starting_equity - 625,
    }
    broker.positions = lambda: [{
        "symbol": "AAPL", "qty": 6.18811881, "current_price": 101,
    }]
    try:
        result = Run2Service(cfg, ledger, broker).reconcile()
        assert result["halted"] is False and result["new_fills"] == 1
        assert ledger.decisions(cfg.run_id)[0]["status"] == "filled"
        assert ledger.fill_gaps(cfg.run_id, "baseline") == pytest.approx([1.0])
    finally:
        ledger.close()
