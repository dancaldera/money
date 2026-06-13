"""Tests for trade reconciliation (stop-loss, dedup) with a fake broker."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading.live.trader import evaluate


def _oversold_frame() -> pd.DataFrame:
    # Strictly declining -> RSI 0 -> rsi_meanrev signals BUY.
    close = np.linspace(200, 50, 60)
    idx = pd.date_range("2021-01-01", periods=len(close), freq="D")
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0},
        index=idx,
    )


class FakeBroker:
    def __init__(self, holding=False, pending=False, plpc=None):
        self._holding = holding
        self._pending = pending
        self._plpc = plpc
        self.calls = []

    def is_holding(self, symbol):
        return self._holding

    def has_open_order(self, symbol):
        return self._pending

    def position_plpc(self, symbol):
        return self._plpc

    def buy_notional(self, symbol, notional, asset):
        self.calls.append(("buy", symbol))
        return "order-buy"

    def close(self, symbol):
        self.calls.append(("close", symbol))
        return "order-close"


def test_buy_when_oversold_and_flat():
    b = FakeBroker(holding=False)
    res = evaluate(b, "AAPL", "stock", "rsi_meanrev", _oversold_frame(), notional=1000)
    assert res["signal"] == "BUY"
    assert res["action"] == "bought"
    assert ("buy", "AAPL") in b.calls


def test_no_duplicate_buy_when_pending():
    b = FakeBroker(holding=False, pending=True)
    res = evaluate(b, "AAPL", "stock", "rsi_meanrev", _oversold_frame(), notional=1000)
    assert res["action"] == "none"
    assert b.calls == []


def test_stop_loss_overrides_oversold_signal():
    # Holding at a 10% loss while RSI still says BUY -> stop-loss should close it.
    b = FakeBroker(holding=True, plpc=-10.0)
    res = evaluate(
        b, "AAPL", "stock", "rsi_meanrev", _oversold_frame(),
        notional=1000, stop_loss_pct=8,
    )
    assert res["signal"] == "BUY"      # strategy still wants to hold/buy
    assert res["action"] == "stopped"  # but the stop-loss wins
    assert ("close", "AAPL") in b.calls


def test_no_stop_when_loss_within_threshold():
    b = FakeBroker(holding=True, plpc=-3.0)
    res = evaluate(
        b, "AAPL", "stock", "rsi_meanrev", _oversold_frame(),
        notional=1000, stop_loss_pct=8,
    )
    assert res["action"] == "none"
    assert b.calls == []
