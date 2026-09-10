"""Coverage for trading.run2.reporting (metrics + rendering)."""

from __future__ import annotations

import pandas as pd
import pytest

from trading.run2.reporting import _closed_trade_pnls, collect_run_report, render_run_report

from .run2_helpers import add_fill, initialized_ledger


def _decision(ledger, cfg, decision_id, symbol="AAPL", price="100", portfolio="baseline"):
    return ledger.record_decision(
        {
            "decision_id": decision_id,
            "run_id": cfg.run_id,
            "portfolio": portfolio,
            "symbol": symbol,
            "asset": "stock",
            "strategy": cfg.strategy.name,
            "bar_end": "2026-08-23T00:00:00+00:00",
            "signal": "BUY",
            "signal_price": price,
            "notional": "625",
            "action": "buy_intent",
            "status": "filled",
            "config_hash": cfg.fingerprint,
        }
    )


def _seed_rich_baseline(ledger, cfg):
    _decision(ledger, cfg, "d-buy")
    ledger.record_fill({"fill_id": "b1", "run_id": cfg.run_id, "portfolio": "baseline",
                        "broker_order_id": "o1", "decision_id": "d-buy", "symbol": "AAPL",
                        "asset": "stock", "side": "buy", "qty": "2", "price": "100",
                        "transaction_time": "2026-08-24T14:00:00+00:00"})
    ledger.record_fill({"fill_id": "s1", "run_id": cfg.run_id, "portfolio": "baseline",
                        "broker_order_id": "o2", "symbol": "AAPL", "asset": "stock",
                        "side": "sell", "qty": "1", "price": "110",
                        "transaction_time": "2026-08-24T15:00:00+00:00"})
    ledger.record_fill({"fill_id": "s2", "run_id": cfg.run_id, "portfolio": "baseline",
                        "broker_order_id": "o3", "symbol": "AAPL", "asset": "stock",
                        "side": "sell", "qty": "1", "price": "120",
                        "transaction_time": "2026-08-24T16:00:00+00:00"})
    # Orphan sell with no position is ignored, not a crash.
    ledger.record_fill({"fill_id": "orphan", "run_id": cfg.run_id, "portfolio": "baseline",
                        "symbol": "MSFT", "asset": "stock", "side": "sell", "qty": "1",
                        "price": "100", "transaction_time": "2026-08-24T17:00:00+00:00"})
    ledger.record_fee({"fee_id": "fee-1", "run_id": cfg.run_id, "broker_order_id": "o1",
                       "activity_type": "CFEE", "amount": "1.0",
                       "occurred_at": "2026-08-24"})
    ledger.record_fee({"fee_id": "fee-2", "run_id": cfg.run_id, "broker_order_id": None,
                       "activity_type": "FEE", "amount": "0.5",
                       "occurred_at": "2026-08-24"})
    for i, equity in enumerate([100_000.0, 101_000.0, 100_500.0]):
        ledger.record_equity({"run_id": cfg.run_id, "portfolio": "baseline",
                              "captured_at": f"2026-08-2{4 + i}T00:00:00+00:00",
                              "equity": str(equity), "cash": "90000",
                              "gross_exposure": "1000", "drawdown_pct": 0.0,
                              "source": "test"})


def _seed_shadow_round_trip(ledger, cfg):
    ledger.record_fill({"fill_id": "sh-b1", "run_id": cfg.run_id, "portfolio": "shadow_regime",
                        "symbol": "BTC/USD", "asset": "crypto", "side": "buy", "qty": "0.01",
                        "price": "60000", "transaction_time": "2026-08-24T14:00:00+00:00",
                        "simulated": 1})
    ledger.record_fill({"fill_id": "sh-s1", "run_id": cfg.run_id, "portfolio": "shadow_regime",
                        "symbol": "BTC/USD", "asset": "crypto", "side": "sell", "qty": "0.01",
                        "price": "61000", "transaction_time": "2026-08-24T15:00:00+00:00",
                        "simulated": 1})
    ledger.record_fill({"fill_id": "sh-b2", "run_id": cfg.run_id, "portfolio": "shadow_regime",
                        "symbol": "AAPL", "asset": "stock", "side": "buy", "qty": "1",
                        "price": "100", "transaction_time": "2026-08-24T14:00:00+00:00",
                        "simulated": 1})
    ledger.record_fill({"fill_id": "sh-s2", "run_id": cfg.run_id, "portfolio": "shadow_regime",
                        "symbol": "AAPL", "asset": "stock", "side": "sell", "qty": "1",
                        "price": "90", "transaction_time": "2026-08-24T15:00:00+00:00",
                        "simulated": 1})


def test_closed_trade_pnls_net_fees_and_partials(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _seed_rich_baseline(ledger, cfg)
        pnls = _closed_trade_pnls(ledger, cfg, "baseline")
        # (110-100) + (120-100) - $1 linked fee = $29.
        assert pnls == [29.0]
        _seed_shadow_round_trip(ledger, cfg)
        shadow = _closed_trade_pnls(ledger, cfg, "shadow_regime")
        assert len(shadow) == 2 and shadow[0] > 0 > shadow[1]
    finally:
        ledger.close()


def test_collect_and_render_full_report(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _seed_rich_baseline(ledger, cfg)
        _seed_shadow_round_trip(ledger, cfg)
        add_fill(ledger, cfg, "ETH/USD", "crypto", "buy", 1, 3000, "09",
                 portfolio="shadow_regime")
        benchmark = pd.Series([100_000.0, 101_000.0],
                              index=pd.to_datetime(["2026-08-24", "2026-08-25"]))
        report = collect_run_report(cfg, ledger, marks={"AAPL": 105.0}, benchmark=benchmark)
        assert report["status"] == "active"
        assert report["fees"] == 1.5
        baseline = report["portfolios"]["baseline"]
        assert baseline["completed_trades"] == 1
        assert baseline["win_rate_pct"] == 100.0
        assert baseline["mean_fill_gap_pct"] == 0.0  # buy filled at signal price
        assert baseline["sharpe"] != 0.0
        assert report["portfolios"]["shadow_regime_news"]["completed_trades"] == 0
        assert report["portfolios"]["shadow_regime_news"]["equity"] == cfg.starting_equity
        assert report["primary_benchmark"]["return_pct"] == pytest.approx(1.0)
        text = render_run_report(report)
        assert "run2" in text and "Sharpe" in text and "Primary benchmark" in text
    finally:
        ledger.close()


def test_collect_without_benchmark_and_with_halt(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        report = collect_run_report(cfg, ledger)
        assert report["primary_benchmark"] is None
        assert all(p["completed_trades"] == 0 for p in report["portfolios"].values())
        ledger.halt(cfg.run_id, "drawdown_halt:5.1%")
        halted = collect_run_report(cfg, ledger)
        assert "HALT" in render_run_report(halted)
    finally:
        ledger.close()
