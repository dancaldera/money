from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json

import pytest
import yaml

from trading.run2.config import RunConfigError, load_run_config
from trading.run2.ledger import INKIND_FEE_PREFIX, LedgerError

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


# --- one crypto charge, two stored rows: the fee metric must count it once -------------------- #
def _fee_row(cfg, fee_id, symbol, amount, *, activity_type="CFEE", raw=None):
    return {
        "fee_id": fee_id,
        "run_id": cfg.run_id,
        "symbol": symbol,
        "activity_type": activity_type,
        "amount": str(amount),
        "occurred_at": "2026-09-22T00:36:26Z",
        "raw_json": json.dumps({"activity_type": activity_type} if raw is None else raw),
    }


def test_total_fees_counts_an_in_kind_crypto_fee_once(tmp_path):
    """The live AAVE/BTC/LINK pairs: the ledger books the crypto taker fee from the
    fill's net qty (fee_id inkind-…, the copy an order is attached to) and Alpaca
    separately reports the same charge as a CFEE activity whose net_amount is 0 —
    no USD moved, the fee was taken inside the asset received. Summing both made
    run3 report $9.14 of fees against a true $4.57, doubling the cost hurdle that
    sizes the crypto breadth decision."""
    cfg, ledger = initialized_ledger(tmp_path)
    booked = Decimal("1.51441313589351")  # gross qty x price x 25bps
    reported = Decimal("1.51442041402")  # the broker's copy, off by the rounded net qty
    try:
        ledger.record_fee(_fee_row(cfg, f"{INKIND_FEE_PREFIX}f-btc", "BTC/USD", booked))
        ledger.record_fee(
            _fee_row(cfg, "20260922000000000::btc", "BTCUSD", reported,
                     raw={"net_amount": "0", "qty": "-0.000017518"})
        )
        assert ledger.total_fees(cfg.run_id) == booked

        # Two buys of one symbol stay two fees: a booking absorbs one copy each.
        for suffix in ("a", "b"):
            ledger.record_fee(
                _fee_row(cfg, f"{INKIND_FEE_PREFIX}f-link-{suffix}", "LINK/USD", Decimal("1.5"))
            )
        ledger.record_fee(
            _fee_row(cfg, "20260922000000000::link", "LINKUSD", Decimal("1.5"), raw={"net_amount": "0"})
        )
        assert ledger.total_fees(cfg.run_id) == booked + Decimal("3.0")
    finally:
        ledger.close()


def test_total_fees_keeps_cash_fees_and_unpaired_fees(tmp_path):
    """Only a net_amount-0 CFEE/FEE row matching a booked in-kind amount is a copy.
    A fee that moves cash (a crypto sell charged on the USD proceeds, any stock
    fee), a buy whose booking never happened, and a row off by more than rounding
    all stay in the total, so the metric can only ever lose duplicates."""
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        # a cash fee coexists with the symbol's in-kind buy booking
        ledger.record_fee(
            _fee_row(cfg, f"{INKIND_FEE_PREFIX}f-aave", "AAVE/USD", Decimal("1.53777505719375"))
        )
        ledger.record_fee(
            _fee_row(cfg, "2026::aave-cash", "AAVEUSD", Decimal("1.537775181"),
                     raw={"net_amount": "-1.537775181"})
        )
        # no booking for this symbol: the activity is the only record of the cost
        ledger.record_fee(_fee_row(cfg, "2026::doge", "DOGEUSD", Decimal("2.0"), raw={"net_amount": "0"}))
        # off by far more than rounding: a distinct fee, not a copy
        ledger.record_fee(_fee_row(cfg, f"{INKIND_FEE_PREFIX}f-eth", "ETH/USD", Decimal("1.0")))
        ledger.record_fee(_fee_row(cfg, "2026::eth", "ETHUSD", Decimal("1.5"), raw={"net_amount": "0"}))
        # a zero booking absorbs nothing
        ledger.record_fee(_fee_row(cfg, f"{INKIND_FEE_PREFIX}f-xrp-0", "XRP/USD", Decimal("0")))
        ledger.record_fee(_fee_row(cfg, f"{INKIND_FEE_PREFIX}f-xrp-1", "XRP/USD", Decimal("1.0")))
        ledger.record_fee(_fee_row(cfg, "2026::xrp", "XRPUSD", Decimal("1.0"), raw={"net_amount": "0"}))
        assert ledger.total_fees(cfg.run_id) == (
            Decimal("1.53777505719375")
            + Decimal("1.537775181")
            + Decimal("2.0")
            + Decimal("1.0")
            + Decimal("1.5")
            + Decimal("1.0")
        )
    finally:
        ledger.close()


def test_total_fees_counts_rows_that_cannot_be_a_crypto_restatement(tmp_path):
    """Rows with no usable broker payload (broken json, a non-dict payload, no
    net_amount) or a non-fee activity type are always counted."""
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        ledger.record_fee(_fee_row(cfg, f"{INKIND_FEE_PREFIX}f-sol", "SOL/USD", Decimal("1.0")))
        ledger.record_fee(
            _fee_row(cfg, "2026::sol-int", "SOLUSD", Decimal("1.0"),
                     activity_type="INT", raw={"net_amount": "0"})
        )
        for label, raw_text in (("a", "{not json"), ("b", "5"), ("c", json.dumps({"id": "x"}))):
            row = _fee_row(cfg, f"2026-{label}::avax", "AVAXUSD", Decimal("2.0"))
            row["raw_json"] = raw_text
            ledger.record_fee(row)
        assert ledger.total_fees(cfg.run_id) == Decimal("8.0")
    finally:
        ledger.close()
