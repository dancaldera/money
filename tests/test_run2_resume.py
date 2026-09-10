"""Mid-history resume: bind the frozen run to a paper account that already traded."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading.run2.ledger import RunLedger
from trading.run2.service import Run2Service, RunSafetyError

from .run2_helpers import run2_config


class ResumeBroker:
    """Fake Alpaca paper account with history (the reason resume exists)."""

    def __init__(
        self,
        *,
        equity=99_978.96,
        cash=99_375.0,
        positions=None,
        orders=None,
        activities=None,
    ):
        self._equity = equity
        self._cash = cash
        self._positions = positions or []
        self._orders = orders or []
        self._activities = activities or []

    def account(self):
        return {
            "id": "paper-account",
            "equity": self._equity,
            "cash": self._cash,
            "buying_power": self._cash,
        }

    def positions(self):
        return self._positions

    def all_orders(self, after=None):
        # Mirror Alpaca: `after` filters history, so orders placed before the run
        # started are invisible to reconcile — that is why resume can bind mid-history.
        return [] if after is not None else self._orders

    def activities(self, activity_type, after=None):
        return [] if after is not None else self._activities

    def clock(self):
        return {"is_open": True}


AAPL_POSITION = {
    "symbol": "AAPL",
    "qty": 1.902846704,
    "avg_entry": 328.45,
    "current_price": 317.40,
    "asset_class": "us_equity",
}


def _service(tmp_path, broker):
    cfg = run2_config()
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    return cfg, ledger, Run2Service(cfg, ledger, broker)


def test_resume_imports_open_position(tmp_path):
    cfg, ledger, service = _service(tmp_path, ResumeBroker(positions=[AAPL_POSITION]))
    try:
        result = service.resume()
        assert [imp["symbol"] for imp in result["imported"]] == ["AAPL"]
        assert result["imported"][0]["asset"] == "stock"
        assert result["warnings"] == []
        run = ledger.run(cfg.run_id)
        assert run["config_hash"] == cfg.fingerprint
        assert run["broker_account_id"] == "paper-account"
        position = ledger.positions(cfg.run_id)["AAPL"]
        assert abs(position.qty - Decimal("1.902846704")) < Decimal("1e-9")
        assert abs(position.avg_entry - Decimal("328.45")) < Decimal("1e-9")
        fills = ledger.fills(cfg.run_id)
        assert len(fills) == 1 and fills[0]["simulated"] == 1
        snap = ledger.equity_history(cfg.run_id)[-1]
        assert abs(float(snap["equity"]) - 99_978.96) < 0.01
    finally:
        ledger.close()


def test_resume_refuses_already_initialized(tmp_path):
    cfg, ledger, service = _service(tmp_path, ResumeBroker(positions=[AAPL_POSITION]))
    try:
        service.resume()
        with pytest.raises(RunSafetyError, match="already initialized"):
            service.resume()
    finally:
        ledger.close()


def test_resume_warns_on_position_outside_universe(tmp_path):
    broker = ResumeBroker(
        equity=99_000.0,
        cash=98_400.0,
        positions=[
            {
                "symbol": "SPY",
                "qty": 0.5,
                "avg_entry": 600.0,
                "current_price": 610.0,
                "asset_class": "us_equity",
            }
        ],
    )
    cfg, ledger, service = _service(tmp_path, broker)
    try:
        result = service.resume()
        assert result["imported"][0]["asset"] == "stock"
        assert any("position_outside_universe:SPY" in w for w in result["warnings"])
    finally:
        ledger.close()


def test_reconcile_after_resume_is_clean(tmp_path):
    """Broker history predates the run, so reconcile finds nothing unknown and the
    run keeps trading with the fail-closed gates intact."""
    broker = ResumeBroker(
        positions=[AAPL_POSITION],
        orders=[{"id": "past-order-from-previous-run"}],
    )
    cfg, ledger, service = _service(tmp_path, broker)
    try:
        service.resume()
        report = service.reconcile()
        assert report["unknown_orders"] == []
        assert report["quantity_mismatches"] == []
        assert report["halted"] is False
        assert abs(report["equity"] - 99_978.96) < 0.01
    finally:
        ledger.close()


def test_health_is_read_only_and_reports_state(tmp_path):
    cfg, ledger, service = _service(tmp_path, ResumeBroker(positions=[AAPL_POSITION]))
    try:
        service.resume()
        info = service.health()
        assert info["run_id"] == cfg.run_id
        assert info["halted"] is False
        assert info["status"] == "active"
        assert info["positions"] == 1
        assert abs(info["equity"] - 99_978.96) < 0.01
        # read-only: no new fills, no new orders
        assert len(ledger.fills(cfg.run_id)) == 1
    finally:
        ledger.close()


def test_health_refuses_uninitialized_run(tmp_path):
    cfg = run2_config()
    ledger = RunLedger(tmp_path / "empty.sqlite")
    try:
        service = Run2Service(cfg, ledger)
        with pytest.raises(Exception, match="not initialized"):
            service.health()
    finally:
        ledger.close()
