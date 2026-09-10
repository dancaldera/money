"""Coverage for trading.run2.risk gates and trading.run2.ledger gaps."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pandas as pd
import pytest

from trading.run2.ledger import LedgerError, RunLedger
from trading.run2.risk import check_entry, correlation_matches, drawdown_pct

from .run2_helpers import add_fill, initialized_ledger, ohlcv, run2_config


def _pending_buy(ledger, cfg, symbol, asset, notional="625"):
    return ledger.record_decision(
        {
            "run_id": cfg.run_id,
            "portfolio": "baseline",
            "symbol": symbol,
            "asset": asset,
            "strategy": cfg.strategy.name,
            "bar_end": "2026-08-23T00:00:00+00:00",
            "signal": "BUY",
            "signal_price": "100",
            "notional": notional,
            "action": "buy_intent",
            "status": "pending",
            "config_hash": cfg.fingerprint,
        }
    )


# --- risk.correlation_matches branches ---------------------------------------- #
def test_correlation_matches_edge_cases():
    assert correlation_matches("X", {}, {}, 60, 0.8) == 0  # candidate not in bars
    frame = ohlcv(range(100, 200))
    held = {"AAPL": object()}
    assert correlation_matches("AAPL", held, {"AAPL": frame}, 60, 0.8) == 0  # already held
    # Held symbol without bars is skipped; too-short aligned history is skipped.
    assert correlation_matches("NVDA", {"MSFT": object()}, {"NVDA": frame}, 60, 0.8) == 0
    short = ohlcv([100.0, 101.0, 102.0])
    assert correlation_matches("NVDA", {"AAPL": object()},
                               {"NVDA": short, "AAPL": short}, 60, 0.8) == 0


# --- risk.check_entry gates ---------------------------------------------------- #
def test_entry_rejected_when_halted(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        ledger.halt(cfg.run_id, "test")
        assert check_entry(cfg, ledger, "baseline", "AAPL", "stock").reason == "run_halted"
    finally:
        ledger.close()


def test_entry_rejected_when_holding_or_pending(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 1, 100, "01")
        assert check_entry(cfg, ledger, "baseline", "AAPL", "stock").reason == "already_holding"
        _pending_buy(ledger, cfg, "MSFT", "stock")
        assert check_entry(cfg, ledger, "baseline", "MSFT", "stock").reason == "buy_already_pending"
    finally:
        ledger.close()


def test_entry_rejected_at_max_positions(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        for i, symbol in enumerate(cfg.stock_symbols[:8]):
            add_fill(ledger, cfg, symbol, "stock", "buy", 1, 100, f"{i:02d}")
        result = check_entry(cfg, ledger, "baseline", "NFLX", "stock")
        assert result.allowed is False and result.reason == "max_positions"
    finally:
        ledger.close()


def test_entry_rejected_at_max_gross_exposure(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        for i, symbol in enumerate(cfg.stock_symbols[:4]):
            add_fill(ledger, cfg, symbol, "stock", "buy", 2, 625, f"{i:02d}")
        result = check_entry(cfg, ledger, "baseline", "NFLX", "stock")
        assert result.reason == "max_gross_exposure"
    finally:
        ledger.close()


def test_entry_rejected_at_crypto_caps(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        for i, symbol in enumerate(cfg.crypto_symbols[:4]):
            add_fill(ledger, cfg, symbol, "crypto", "buy", 1, 625, f"{i:02d}")
        result = check_entry(cfg, ledger, "baseline", "ETH/USD", "crypto")
        assert result.reason in {"already_holding", "max_crypto_positions"}
        result = check_entry(cfg, ledger, "baseline", "AVAX/USD", "crypto")
        assert result.reason == "max_crypto_positions"
    finally:
        ledger.close()


def test_entry_rejected_at_crypto_exposure(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "BTC/USD", "crypto", "buy", 2, 625, "01")
        add_fill(ledger, cfg, "ETH/USD", "crypto", "buy", 2, 625, "02")
        result = check_entry(cfg, ledger, "baseline", "SOL/USD", "crypto")
        assert result.reason == "max_crypto_exposure"
    finally:
        ledger.close()


def test_entry_rejected_at_stock_caps(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        for i, symbol in enumerate(cfg.stock_symbols[:6]):
            add_fill(ledger, cfg, symbol, "stock", "buy", 1, 625, f"{i:02d}")
        result = check_entry(cfg, ledger, "baseline", "NFLX", "stock")
        assert result.reason == "max_stock_positions"
    finally:
        ledger.close()


def test_entry_rejected_at_stock_exposure(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        for i, symbol in enumerate(cfg.stock_symbols[:3]):
            add_fill(ledger, cfg, symbol, "stock", "buy", 2, 625, f"{i:02d}")
        result = check_entry(cfg, ledger, "baseline", "NFLX", "stock")
        assert result.reason == "max_stock_exposure"
    finally:
        ledger.close()


def test_drawdown_pct_guards_empty_high_water():
    assert drawdown_pct(50.0, 0.0) == 0.0
    assert drawdown_pct(90.0, 100.0) == pytest.approx(10.0)


# --- ledger gaps --------------------------------------------------------------- #
def test_ledger_context_manager_closes(tmp_path):
    cfg = run2_config()
    with RunLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.initialize_run(cfg, "acct")
        assert ledger.run(cfg.run_id) is not None


def test_ledger_migrates_early_schema_without_decision_id(tmp_path):
    cfg = run2_config()
    path = tmp_path / "ledger.sqlite"
    ledger = RunLedger(path)
    ledger.initialize_run(cfg, "acct")
    ledger.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE fills DROP COLUMN decision_id")
    conn.commit()
    conn.close()
    migrated = RunLedger(path)
    try:
        cols = {row["name"] for row in migrated.conn.execute("PRAGMA table_info(fills)")}
        assert "decision_id" in cols
    finally:
        migrated.close()


def test_ledger_transaction_commits_and_rolls_back(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        with ledger.transaction() as conn:
            conn.execute("UPDATE runs SET status='active' WHERE run_id=?", (cfg.run_id,))
        with pytest.raises(RuntimeError, match="boom"):
            with ledger.transaction() as conn:
                conn.execute("UPDATE runs SET status='halted' WHERE run_id=?", (cfg.run_id,))
                raise RuntimeError("boom")
        assert ledger.run(cfg.run_id)["status"] == "active"
    finally:
        ledger.close()


def test_initialize_run_is_idempotent_but_freezes_manifest(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        ledger.initialize_run(cfg, "other-acct")  # same hash -> no-op
        assert ledger.run(cfg.run_id)["broker_account_id"] == "paper-account"
        with pytest.raises(LedgerError, match="changed after initialization"):
            ledger.initialize_run(replace(cfg, fingerprint="0" * 64))
    finally:
        ledger.close()


def test_positions_drop_fully_closed_symbols(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        add_fill(ledger, cfg, "AAPL", "stock", "buy", 1, 100, "01")
        add_fill(ledger, cfg, "AAPL", "stock", "sell", 1, 110, "02")
        assert ledger.positions(cfg.run_id) == {}
    finally:
        ledger.close()


def test_fees_returns_recorded_rows(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        assert ledger.fees(cfg.run_id) == []
        ledger.record_fee({"fee_id": "f1", "run_id": cfg.run_id, "activity_type": "CFEE",
                           "amount": "2.5", "occurred_at": "2026-08-24"})
        rows = ledger.fees(cfg.run_id)
        assert [r["fee_id"] for r in rows] == ["f1"]
    finally:
        ledger.close()


def test_latest_feature_symbol_scoping(tmp_path):
    cfg, ledger = initialized_ledger(tmp_path)
    try:
        assert ledger.latest_feature(cfg.run_id, "news", "negative_news_z", "AAPL") is None
        assert ledger.latest_feature(cfg.run_id, "equity", "regime_score") is None
    finally:
        ledger.close()
