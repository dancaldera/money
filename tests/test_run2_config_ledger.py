from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest
import yaml

from trading.run2.config import RunConfigError, load_run_config
from trading.run2.ledger import LedgerError

from .run2_helpers import add_fill, initialized_ledger, run2_config


def test_frozen_manifest_has_expected_limits():
    cfg = run2_config()
    assert cfg.starting_equity == 100_000
    assert (cfg.strategy.fast_window, cfg.strategy.slow_window) == (10, 30)
    assert cfg.portfolio.position_notional == 625
    assert cfg.portfolio.max_gross_exposure == 5_000
    assert cfg.execution.paper_only is True and cfg.execution.use_margin is False
    assert len(cfg.symbols) == 17
    assert len(cfg.fingerprint) == 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["execution"].update(paper_only=False),
        lambda raw: raw["execution"].update(use_margin=True),
        lambda raw: raw["strategy"].update(name="rsi_meanrev"),
        lambda raw: raw["portfolio"].update(max_gross_exposure=4_000),
    ],
)
def test_unsafe_manifests_are_rejected(tmp_path, mutate):
    raw = deepcopy(run2_config().raw)
    mutate(raw)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(RunConfigError):
        load_run_config(path)


def test_ledger_is_idempotent_and_reconstructs_fill_cost_basis(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        assert add_fill(ledger, cfg, "BTC/USD", "crypto", "buy", 2, 100, "01")
        assert not add_fill(ledger, cfg, "BTC/USD", "crypto", "buy", 2, 100, "01")
        add_fill(ledger, cfg, "BTC/USD", "crypto", "sell", 0.5, 120, "02")
        position = ledger.positions(cfg.run_id)["BTC/USD"]
        assert position.qty == 1.5
        assert position.avg_entry == 100
        assert position.cost_basis == 150
    finally:
        ledger.close()


def test_initialized_manifest_cannot_be_silently_changed(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with pytest.raises(LedgerError):
            ledger.assert_manifest(replace(cfg, fingerprint="0" * 64))
    finally:
        ledger.close()


def test_decision_and_fee_events_are_idempotent(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    row = {
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
        "status": "pending",
        "config_hash": cfg.fingerprint,
    }
    try:
        first = ledger.record_decision(row)
        second = ledger.record_decision(row)
        assert first == second and len(ledger.decisions(cfg.run_id)) == 1
        fee = {
            "fee_id": "fee-1",
            "run_id": cfg.run_id,
            "activity_type": "CFEE",
            "amount": "1.25",
            "occurred_at": "2026-08-24",
            "raw_json": json.dumps({"id": "fee-1"}),
        }
        assert ledger.record_fee(fee) and not ledger.record_fee(fee)
        assert ledger.total_fees(cfg.run_id) == 1.25
    finally:
        ledger.close()
