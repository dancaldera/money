"""Small-unit coverage: runner, clean, yahoo, live signals/trader, tradingview, registry."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading.backtest import run_backtest
from trading.data.clean import drop_forming_bar, truncation_warning
from trading.data import yahoo as yahoo_mod
from trading.live.signals import get_signal_fn, rsi_meanrev_signal
from trading.live.trader import evaluate
from trading.signals import tradingview as tv_mod
from trading.strategies import get_strategy


def _frame(close) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.date_range("2021-01-01", periods=len(close), freq="D")
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0},
        index=idx,
    )


# --- backtest/runner.py: plot_path branch ------------------------------------ #
def test_run_backtest_writes_plot_when_requested(tmp_path):
    df = _frame(np.linspace(100, 160, 60) + np.sin(np.arange(60)) * 5)
    plot_path = tmp_path / "charts" / "run.html"
    stats = run_backtest(df, get_strategy("sma_cross"), plot_path=plot_path)
    assert plot_path.exists()
    assert stats["# Trades"] >= 0


# --- data/clean.py: tz-aware branches ---------------------------------------- #
def test_truncation_warning_handles_tz_aware_index():
    df = _frame([100.0, 101.0, 102.0])
    df.index = pd.to_datetime(df.index, utc=True)
    msg = truncation_warning(df, "2020-01-01", "BTC/USD")
    assert msg is not None and "BTC/USD" in msg


def test_drop_forming_bar_drops_tz_aware_forming_bar():
    now = datetime(2021, 1, 10, 12, tzinfo=timezone.utc)
    df = _frame(np.linspace(100, 110, 10))  # last bar is 2021-01-10
    df.index = pd.to_datetime(df.index, utc=True)
    out = drop_forming_bar(df, "1d", now=now)
    assert len(out) == len(df) - 1


# --- data/yahoo.py ------------------------------------------------------------ #
def test_yahoo_empty_download_raises(monkeypatch):
    monkeypatch.setattr(yahoo_mod.yf, "download", lambda *a, **k: pd.DataFrame())
    with pytest.raises(RuntimeError, match="No data returned"):
        yahoo_mod.download_ohlcv("AAPL")


def test_yahoo_none_download_raises(monkeypatch):
    monkeypatch.setattr(yahoo_mod.yf, "download", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="No data returned"):
        yahoo_mod.download_ohlcv("AAPL")


def test_yahoo_flattens_multiindex_columns(monkeypatch):
    raw = _frame(np.linspace(100, 110, 10))
    raw.columns = pd.MultiIndex.from_product([raw.columns, ["AAPL"]])
    monkeypatch.setattr(yahoo_mod.yf, "download", lambda *a, **k: raw)
    out = yahoo_mod.download_ohlcv("AAPL")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]


# --- live/signals.py ---------------------------------------------------------- #
def test_rsi_flat_series_is_hold():
    assert rsi_meanrev_signal(_frame(np.full(60, 100.0))) == "HOLD"


def test_rsi_mid_range_is_hold():
    # Alternating gains/losses -> RSI ~50, between the 30/70 thresholds.
    close = 100 + np.tile([1.0, -1.0], 30).cumsum()
    assert rsi_meanrev_signal(_frame(close)) == "HOLD"


def test_unknown_live_strategy_exits():
    with pytest.raises(SystemExit, match="Unknown strategy"):
        get_signal_fn("nope")


def test_unknown_backtest_strategy_exits():
    with pytest.raises(SystemExit, match="Unknown strategy"):
        get_strategy("nope")


# --- live/trader.py: SELL + holding ------------------------------------------- #
class _FakeBroker:
    def __init__(self, holding=True):
        self._holding = holding
        self.calls = []

    def is_holding(self, symbol):
        return self._holding

    def has_open_order(self, symbol):
        return False

    def position_plpc(self, symbol):
        return 1.0

    def buy_notional(self, symbol, notional, asset):
        self.calls.append("buy")
        return "o1"

    def close(self, symbol):
        self.calls.append("close")
        return "o2"


def test_sell_signal_closes_open_position():
    rising = _frame(np.linspace(50, 200, 60))  # RSI 100 -> SELL
    broker = _FakeBroker(holding=True)
    res = evaluate(broker, "AAPL", "stock", "rsi_meanrev", rising, notional=1000)
    assert res["signal"] == "SELL"
    assert res["action"] == "closed"
    assert broker.calls == ["close"]


def test_sell_signal_dry_run_would_close():
    rising = _frame(np.linspace(50, 200, 60))
    broker = _FakeBroker(holding=True)
    res = evaluate(broker, "AAPL", "stock", "rsi_meanrev", rising,
                   notional=1000, dry_run=True)
    assert res["action"] == "would_close"
    assert broker.calls == []


# --- signals/tradingview.py --------------------------------------------------- #
class _FakeAnalysis:
    summary = {"RECOMMENDATION": "BUY", "BUY": 10, "SELL": 2, "NEUTRAL": 5}


class _FakeHandler:
    last_kwargs = None

    def __init__(self, **kwargs):
        _FakeHandler.last_kwargs = kwargs

    def get_analysis(self):
        return _FakeAnalysis()


@pytest.fixture
def fake_ta(monkeypatch):
    monkeypatch.setattr(tv_mod, "TA_Handler", _FakeHandler)
    return _FakeHandler


def test_tradingview_crypto_defaults(fake_ta):
    out = tv_mod.get_signal("BTC/USD", "crypto")
    assert out["symbol"] == "BTCUSD"
    assert out["exchange"] == "BINANCE"
    assert out["recommendation"] == "BUY"
    assert fake_ta.last_kwargs["screener"] == "crypto"
    assert fake_ta.last_kwargs["interval"] == "1d"


def test_tradingview_stock_weekly_explicit_exchange(fake_ta):
    out = tv_mod.get_signal("aapl", "stock", interval="1w", exchange="nyse")
    assert out["symbol"] == "AAPL"
    assert out["exchange"] == "NYSE"
    assert fake_ta.last_kwargs["screener"] == "america"
    assert fake_ta.last_kwargs["interval"] == "1W"


def test_tradingview_unknown_interval_falls_back_to_daily(fake_ta):
    tv_mod.get_signal("AAPL", "stock", interval="5m")
    assert fake_ta.last_kwargs["interval"] == "1d"
