"""Coverage for trading.run2.service decision/execution/reconcile gaps."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading.run2.ledger import RunLedger, utc_now
from trading.run2.service import Run2Service, RunSafetyError, _bar_end, _feature_payload

from .run2_helpers import add_fill, fresh_buy_frame, initialized_ledger, ohlcv, run2_config
from .test_run2_risk_service import FakeBroker, _feature


def _sell_frame():
    return ohlcv([20.0] * 30 + [21.0, 5.0])


def _decision(ledger, cfg, *, portfolio="baseline", symbol="AAPL", action="buy_intent",
              status="pending", signal="BUY", price="100", decision_id=None):
    row = {
        "run_id": cfg.run_id, "portfolio": portfolio, "symbol": symbol, "asset": "stock",
        "strategy": cfg.strategy.name, "bar_end": "2026-08-23T00:00:00+00:00",
        "signal": signal, "signal_price": price, "notional": "625",
        "action": action, "status": status, "config_hash": cfg.fingerprint,
    }
    if decision_id:
        row["decision_id"] = decision_id
    return ledger.record_decision(row)


# --- small helpers ---------------------------------------------------------------- #
def test_bar_end_handles_tz_aware_index():
    frame = fresh_buy_frame()
    frame.index = frame.index.tz_localize("UTC")
    assert _bar_end(frame).endswith("+00:00")
    assert _bar_end(fresh_buy_frame()).endswith("+00:00")


def test_feature_payload_accepts_naive_capture_and_rejects_bad_json(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        fresh_naive = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        ledger.record_feature({
            "run_id": cfg.run_id, "scope": "equity", "symbol": None,
            "observed_at": "2026-08-23T00:00:00+00:00", "captured_at": fresh_naive,
            "source": "test", "name": "regime_score", "value_json": '{"value": 4}',
            "payload_hash": "naive-capture",
        })
        assert _feature_payload(ledger, cfg, "equity", "regime_score") == {"value": 4}
        ledger.record_feature({
            "run_id": cfg.run_id, "scope": "equity", "symbol": None,
            "observed_at": "2026-08-24T00:00:00+00:00", "captured_at": utc_now(),
            "source": "test", "name": "broken", "value_json": "{nope",
            "payload_hash": "bad-json",
        })
        assert _feature_payload(ledger, cfg, "equity", "broken") is None
    finally:
        ledger.close()


# --- initialize / resume guards ------------------------------------------------------ #
def test_initialize_requires_broker(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with pytest.raises(RunSafetyError, match="paper broker is required"):
            Run2Service(cfg, ledger).initialize()
    finally:
        ledger.close()


def test_initialize_rejects_wrong_equity_or_cash(tmp_path):
    cfg = run2_config()
    for equity, cash in [(50_000.0, 100_000.0), (100_000.0, 50_000.0)]:
        ledger = RunLedger(tmp_path / f"init-{equity}-{cash}.sqlite")
        broker = FakeBroker(equity=equity)
        broker.account = lambda: {"id": "p", "equity": equity, "cash": cash,
                                  "buying_power": cash}
        try:
            with pytest.raises(RunSafetyError, match="must be"):
                Run2Service(cfg, ledger, broker).initialize()
        finally:
            ledger.close()


def test_initialize_success_records_snapshot(tmp_path):
    cfg = run2_config()
    ledger = RunLedger(tmp_path / "fresh.sqlite")
    try:
        account = Run2Service(cfg, ledger, FakeBroker(equity=cfg.starting_equity)).initialize()
        assert account["equity"] == cfg.starting_equity
        assert ledger.run(cfg.run_id)["broker_account_id"] == "paper"
        assert len(ledger.equity_history(cfg.run_id)) == 1
    finally:
        ledger.close()


def test_resume_requires_broker_and_skips_empty_positions(tmp_path):
    cfg = run2_config()
    ledger = RunLedger(tmp_path / "resume.sqlite")
    try:
        with pytest.raises(RunSafetyError, match="paper broker is required"):
            Run2Service(cfg, ledger).resume()
        broker = FakeBroker()
        broker.positions = lambda: [{"symbol": "AAPL", "qty": 0, "avg_entry": 100}]
        result = Run2Service(cfg, ledger, broker).resume()
        assert result["imported"] == []
    finally:
        ledger.close()


# --- scan gates ----------------------------------------------------------------------- #
def test_scan_regime_gate_blocks_weak_regime(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _feature(ledger, cfg, "equity", "regime_score", 1)
        rows = Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        shadow = next(r for r in rows if r["portfolio"] == "shadow_regime" and r["symbol"] == "AAPL")
        assert shadow["action"] == "none" and shadow["reason"] == "regime_gate:1"
    finally:
        ledger.close()


def test_scan_news_shadow_requires_fresh_negative_load(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _feature(ledger, cfg, "equity", "regime_score", 4)
        rows = Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        news = next(r for r in rows if r["portfolio"] == "shadow_regime_news" and r["symbol"] == "AAPL")
        assert news["reason"] == "missing_news"
    finally:
        ledger.close()


def test_scan_news_shadow_blocks_negative_spike(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _feature(ledger, cfg, "equity", "regime_score", 4)
        _feature(ledger, cfg, "news", "negative_news_z", 3.0, "AAPL")
        rows = Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        news = next(r for r in rows if r["portfolio"] == "shadow_regime_news" and r["symbol"] == "AAPL")
        assert news["action"] == "none" and news["reason"] == "negative_news:3.00"
    finally:
        ledger.close()


def test_scan_sell_intent_and_holding_reason(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "01")
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "02",
                 portfolio="shadow_regime")
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "03",
                 portfolio="shadow_regime_news")
        rows = Run2Service(cfg, ledger).scan({"AAPL": _sell_frame()})
        aapl = [r for r in rows if r["symbol"] == "AAPL"]
        assert {r["action"] for r in aapl} == {"sell_intent"}
        assert {r["reason"] for r in aapl} == {"reverse_cross"}
        rows = Run2Service(cfg, ledger).scan({"AAPL": ohlcv([100.0] * 40)})
        held = next(r for r in rows if r["portfolio"] == "baseline" and r["symbol"] == "AAPL")
        assert held["action"] == "none" and held["reason"] == "holding"
    finally:
        ledger.close()


# --- execute_pending ------------------------------------------------------------------- #
def test_execute_requires_broker_unless_dry_run(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with pytest.raises(RunSafetyError, match="paper broker is required"):
            Run2Service(cfg, ledger).execute_pending("stock")
    finally:
        ledger.close()


def test_execute_dry_run_reports_would_buy(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        out = Run2Service(cfg, ledger).execute_pending("stock", dry_run=True)
        assert out[0]["action"] == "would_buy"
    finally:
        ledger.close()


def test_execute_sell_intent_dry_and_live(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "01")
        Run2Service(cfg, ledger).scan({"AAPL": _sell_frame()})
        out = Run2Service(cfg, ledger).execute_pending("stock", dry_run=True)
        assert out[0]["action"] == "would_close"
        broker = FakeBroker()
        out = Run2Service(cfg, ledger, broker).execute_pending("stock")
        assert out[0]["action"] == "submitted" and broker.closed == ["AAPL"]
    finally:
        ledger.close()


# --- check_stops -------------------------------------------------------------------------- #
def test_check_stops_requires_broker(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with pytest.raises(RunSafetyError, match="paper broker is required"):
            Run2Service(cfg, ledger).check_stops()
    finally:
        ledger.close()


def test_check_stops_skips_missing_or_healthy_positions(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "01")
        broker = FakeBroker()
        assert Run2Service(cfg, ledger, broker).check_stops()[0]["reason"] == "missing_broker_position"
        broker.positions = lambda: [{"symbol": "AAPL", "current_price": None}]
        assert Run2Service(cfg, ledger, broker).check_stops()[0]["reason"] == "missing_broker_position"
        broker.positions = lambda: [{"symbol": "AAPL", "current_price": 99.0}]
        out = Run2Service(cfg, ledger, broker).check_stops()
        assert out[0]["action"] == "none"
    finally:
        ledger.close()


def test_check_stops_dry_run_would_stop(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "01")
        broker = FakeBroker()
        broker.positions = lambda: [{"symbol": "AAPL", "current_price": 90.0}]
        out = Run2Service(cfg, ledger, broker).check_stops(dry_run=True)
        assert out[0]["action"] == "would_stop"
        assert broker.closed == []
    finally:
        ledger.close()


# --- simulate_shadow ------------------------------------------------------------------------- #
def _armed_shadows(ledger, cfg):
    _feature(ledger, cfg, "equity", "regime_score", 4)
    _feature(ledger, cfg, "news", "negative_news_z", 0, "AAPL")


def test_simulate_shadow_fills_buys_and_records_equity(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _armed_shadows(ledger, cfg)
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        out = Run2Service(cfg, ledger).simulate_shadow({"AAPL": 20.0})
        assert {(r["portfolio"], r["side"]) for r in out} == {
            ("shadow_regime", "buy"), ("shadow_regime_news", "buy")}
        assert ledger.positions(cfg.run_id, "shadow_regime")["AAPL"].qty > 0
        assert len(ledger.equity_history(cfg.run_id, "shadow_regime")) == 1
    finally:
        ledger.close()


def test_simulate_shadow_skips_intents_without_a_price(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _armed_shadows(ledger, cfg)
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        # Pending intents exist, but no price for AAPL -> skipped silently.
        assert Run2Service(cfg, ledger).simulate_shadow({"MSFT": 100.0}) == []
        assert ledger.decisions(cfg.run_id, "shadow_regime", "pending") != []
    finally:
        ledger.close()


def test_simulate_shadow_expires_adverse_gap(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        _armed_shadows(ledger, cfg)
        Run2Service(cfg, ledger).scan({"AAPL": fresh_buy_frame()})
        out = Run2Service(cfg, ledger).simulate_shadow({"AAPL": 21.0})  # > 20 * 1.02
        assert out == []
        rows = ledger.decisions(cfg.run_id, "shadow_regime", "expired")
        assert len(rows) == 1 and rows[0]["reason"].startswith("adverse_gap")
    finally:
        ledger.close()


def test_simulate_shadow_sells_and_rejects_positionless(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 6.25, 100, "01",
                 portfolio="shadow_regime")
        _decision(ledger, cfg, portfolio="shadow_regime", action="sell_intent",
                  signal="SELL", decision_id="sh-sell")
        _decision(ledger, cfg, portfolio="shadow_regime_news", action="sell_intent",
                  signal="SELL", decision_id="sh-sell-nopos")
        out = Run2Service(cfg, ledger).simulate_shadow({"AAPL": 105.0})
        assert {(r["portfolio"], r["side"]) for r in out} == {("shadow_regime", "sell")}
        rows = ledger.decisions(cfg.run_id, "shadow_regime_news", "rejected")
        assert rows[0]["reason"] == "no_shadow_position"
    finally:
        ledger.close()


# --- reconcile ------------------------------------------------------------------------------- #
def test_reconcile_requires_broker(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with pytest.raises(RunSafetyError, match="paper broker is required"):
            Run2Service(cfg, ledger).reconcile()
    finally:
        ledger.close()


def test_reconcile_halts_on_unknown_orders_and_mismatches(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker(equity=cfg.starting_equity)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 1, 100, "01")
        broker.activities = lambda kind, after=None: (
            [{"id": "fill-x", "order_id": "ghost-order", "symbol": "AAPL", "side": "buy",
              "qty": "5", "price": "100", "transaction_time": "2026-08-24T14:00:00Z"}]
            if kind == "FILL" else []
        )
        broker.all_orders = lambda after=None: [{"id": "ghost-order", "status": "filled",
                                                 "filled_qty": 5}]
        broker.positions = lambda: [{"symbol": "AAPL", "qty": 999}]
        result = Run2Service(cfg, ledger, broker).reconcile()
        assert result["unknown_orders"] != []
        assert result["quantity_mismatches"] == ["AAPL"]
        assert result["halted"] is True
        assert ledger.is_halted(cfg.run_id)
    finally:
        ledger.close()


def test_reconcile_mirrors_terminal_order_states(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker(equity=cfg.starting_equity)
    try:
        d1 = _decision(ledger, cfg, status="submitted", decision_id="d-cancel")
        ledger.record_order({"run_id": cfg.run_id, "decision_id": d1, "portfolio": "baseline",
                             "client_order_id": "c1", "broker_order_id": "o-cancel",
                             "symbol": "AAPL", "asset": "stock", "side": "buy",
                             "status": "accepted"})
        d2 = _decision(ledger, cfg, status="submitted", decision_id="d-reject",
                       symbol="MSFT")
        ledger.record_order({"run_id": cfg.run_id, "decision_id": d2, "portfolio": "baseline",
                             "client_order_id": "c2", "broker_order_id": "o-reject",
                             "symbol": "MSFT", "asset": "stock", "side": "buy",
                             "status": "accepted"})
        ledger.record_order({"run_id": cfg.run_id, "decision_id": None, "portfolio": "baseline",
                             "client_order_id": "c3", "broker_order_id": "o-orphan",
                             "symbol": "NVDA", "asset": "stock", "side": "buy",
                             "status": "accepted"})
        broker.activities = lambda kind, after=None: []
        broker.all_orders = lambda after=None: [
            {"id": "o-cancel", "status": "canceled", "filled_qty": 0},
            {"id": "o-reject", "status": "rejected", "filled_qty": 0},
            {"id": "o-orphan", "status": "filled", "filled_qty": 1},
        ]
        result = Run2Service(cfg, ledger, broker).reconcile()
        by_id = {d["decision_id"]: d for d in ledger.decisions(cfg.run_id)}
        assert by_id["d-cancel"]["status"] == "expired"
        assert by_id["d-reject"]["status"] == "rejected"
        assert result["halted"] is False
    finally:
        ledger.close()


def test_reconcile_derives_fee_from_qty_and_price(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    broker = FakeBroker(equity=cfg.starting_equity)
    try:
        broker.activities = lambda kind, after=None: (
            [{"id": "fee-x", "order_id": "o1", "symbol": "AAPL", "net_amount": "0",
              "qty": "2", "price": "1.5", "date": "2026-08-24"}]
            if kind == "CFEE" else []
        )
        result = Run2Service(cfg, ledger, broker).reconcile()
        assert result["new_fees"] == 1
        assert ledger.total_fees(cfg.run_id) == 3.0
    finally:
        ledger.close()


def test_record_account_snapshot_halts_past_drawdown(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        service = Run2Service(cfg, ledger)
        service.record_account_snapshot({"equity": cfg.starting_equity, "cash": cfg.starting_equity})
        row = service.record_account_snapshot({"equity": 94_000.0, "cash": 94_000.0})
        assert float(row["drawdown_pct"]) == pytest.approx(6.0)
        assert ledger.is_halted(cfg.run_id)
    finally:
        ledger.close()
