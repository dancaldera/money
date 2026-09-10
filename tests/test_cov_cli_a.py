"""CLI coverage A: helpers, backtest/scan/validate, dashboard, email, signals, paper."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import trading.cli as cli_mod
from trading.live import BrokerError


def _cfg(**over):
    base = {
        "defaults": {"timeframe": "1d", "since": "2022-01-01", "cash": 10_000,
                     "commission": 0.002},
        "crypto": {"symbols": ["BTC/USD"], "tradingview_exchange": "COINBASE"},
        "stocks": {"symbols": ["AAPL"]},
        "paper": {"notional": 1000, "stop_loss_pct": 8},
    }
    base.update(over)
    return base


def _row(**over):
    base = {"trades": 5, "win_rate_pct": 60.0, "return_pct": 12.5,
            "buy_hold_pct": 8.0, "max_drawdown_pct": -3.2, "sharpe": 1.234,
            "final_equity": 11250.0}
    base.update(over)
    return base


# --- helpers ---------------------------------------------------------------------- #
def test_load_config_present_and_missing(tmp_path, monkeypatch):
    assert "defaults" in cli_mod.load_config()
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", tmp_path / "nope.yaml")
    assert cli_mod.load_config() == {}


def test_get_candles_dispatches_and_rejects(monkeypatch):
    monkeypatch.setattr(cli_mod, "load_or_fetch", lambda key, fn, refresh: fn())
    monkeypatch.setattr(cli_mod, "fetch_crypto", lambda *a: "crypto-df")
    monkeypatch.setattr(cli_mod, "fetch_equity", lambda *a: "stock-df")
    assert cli_mod.get_candles("BTC/USD", "crypto", "1d", "2022-01-01", {}, False) == "crypto-df"
    assert cli_mod.get_candles("AAPL", "stock", "1d", "2022-01-01", {}, True) == "stock-df"
    with pytest.raises(SystemExit, match="Unknown asset"):
        cli_mod.get_candles("X", "option", "1d", "2022-01-01", {}, False)


def test_recent_bars_lookback_and_dispatch(monkeypatch):
    seen = {}

    def fetch(symbol, timeframe, since):
        seen[timeframe] = since
        return "bars"

    monkeypatch.setattr(cli_mod, "fetch_crypto", fetch)
    monkeypatch.setattr(cli_mod, "fetch_equity", fetch)
    monkeypatch.setattr(cli_mod, "drop_forming_bar", lambda bars, tf: (bars, tf))
    assert cli_mod.recent_bars("BTC/USD", "crypto", "1d", {}) == ("bars", "1d")
    assert seen["1d"] == (date.today() - timedelta(days=400)).isoformat()
    assert cli_mod.recent_bars("AAPL", "stock", "1h", {}) == ("bars", "1h")
    assert seen["1h"] == (date.today() - timedelta(days=45)).isoformat()
    with pytest.raises(SystemExit, match="Unknown asset"):
        cli_mod.recent_bars("X", "option", "1d", {})


def test_one_backtest_plot_and_plain(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "get_candles", lambda *a: "df")
    monkeypatch.setattr(cli_mod, "run_backtest", lambda *a, **k: "stats")
    monkeypatch.setattr(cli_mod, "record_run", lambda meta, stats: {"m": meta, "s": stats})
    monkeypatch.setattr(cli_mod, "RESULTS_DIR", tmp_path)
    row, plot = cli_mod._one_backtest("BTC/USD", "crypto", "sma_cross", "1d",
                                      "2022-01-01", 10_000, 0.002, {}, False, plot=True)
    assert row["m"]["symbol"] == "BTC/USD" and str(plot).endswith(".html")
    row, plot = cli_mod._one_backtest("AAPL", "stock", "sma_cross", "1d",
                                      "2022-01-01", 10_000, 0.002, {}, False, plot=False)
    assert plot is None


def test_fmt_row():
    assert "trades=5" in cli_mod._fmt_row(_row())


# --- cmd_backtest / cmd_scan ----------------------------------------------------------- #
def test_cmd_backtest_with_and_without_plot(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_one_backtest", lambda *a, plot: (_row(), "/tmp/x.html" if plot else None))
    args = SimpleNamespace(symbol="AAPL", asset="stock", strategy="sma_cross",
                           timeframe=None, since=None, cash=None, refresh=False, no_plot=False)
    cli_mod.cmd_backtest(args, _cfg())
    assert "chart:" in capsys.readouterr().out
    args.no_plot = True
    cli_mod.cmd_backtest(args, _cfg())
    assert "chart:" not in capsys.readouterr().out


def test_cmd_scan_mixed_success_and_error(monkeypatch, capsys):
    def fake(symbol, *a, **k):
        if symbol == "BTC/USD":
            raise RuntimeError("offline")
        return _row(return_pct=3.0), None

    monkeypatch.setattr(cli_mod, "_one_backtest", fake)
    args = SimpleNamespace(strategy="sma_cross", asset=None, timeframe=None,
                           since=None, refresh=False)
    cli_mod.cmd_scan(args, _cfg())
    out = capsys.readouterr().out
    assert "ERROR: offline" in out and "Best return: AAPL" in out


def test_cmd_scan_asset_filter_and_no_targets(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_one_backtest", lambda *a, **k: (_row(), None))
    args = SimpleNamespace(strategy="sma_cross", asset="crypto", timeframe="1h",
                           since="2024-01-01", refresh=True)
    cli_mod.cmd_scan(args, _cfg())
    assert "BTC/USD" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No symbols"):
        cli_mod.cmd_scan(args, {})


def test_cmd_scan_all_errors_skips_best(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(cli_mod, "_one_backtest", boom)
    args = SimpleNamespace(strategy="sma_cross", asset="stock", timeframe=None,
                           since=None, refresh=False)
    cli_mod.cmd_scan(args, _cfg())
    assert "Best return" not in capsys.readouterr().out


# --- cmd_validate -------------------------------------------------------------------------- #
def _validate_df():
    return pd.DataFrame({"Close": range(150)},
                        index=pd.date_range("2024-01-01", periods=150, freq="D"))


def test_cmd_validate_robust_skip_and_error(monkeypatch, capsys):
    calls = {"n": 0}

    def fake_window(df, strategy, cash, commission):
        calls["n"] += 1
        if calls["n"] <= 2:
            return None  # BTC: insufficient history in both windows
        return {"alpha": 5.0, "return_pct": 6.0, "buy_hold_pct": 1.0}

    monkeypatch.setattr(cli_mod, "get_candles",
                        lambda symbol, *a: (_ for _ in ()).throw(RuntimeError("bad"))
                        if symbol == "MSFT" else _validate_df())
    monkeypatch.setattr(cli_mod, "_window_alpha", fake_window)
    cfg = _cfg(stocks={"symbols": ["AAPL", "MSFT"]})
    args = SimpleNamespace(strategy="sma_cross", asset=None, timeframe=None,
                           train_start=None, split="2024-03-01", refresh=False)
    cli_mod.cmd_validate(args, cfg)
    out = capsys.readouterr().out
    assert "insufficient history" in out and "ERROR: bad" in out
    assert "Best out-of-sample: sma_cross" in out


def test_cmd_validate_verdicts_and_best(monkeypatch, capsys):
    # strategies run alphabetically: rsi_meanrev first, then sma_cross.
    script = iter([
        {"alpha": -1.0}, {"alpha": 2.0},   # rsi train/test -> test only
        {"alpha": 3.0}, {"alpha": -1.0},   # sma train/test -> train only
    ])
    monkeypatch.setattr(cli_mod, "get_candles", lambda *a: _validate_df())
    monkeypatch.setattr(cli_mod, "_window_alpha", lambda *a: next(script))
    args = SimpleNamespace(strategy=None, asset="crypto", timeframe="1d",
                           train_start="2024-01-01", split="2024-03-01", refresh=False)
    cli_mod.cmd_validate(args, _cfg())
    out = capsys.readouterr().out
    assert "test only" in out and "train only" in out
    assert "Best out-of-sample: rsi_meanrev" in out


def test_cmd_validate_underperforms_and_empty_summary(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "get_candles", lambda *a: _validate_df())
    monkeypatch.setattr(cli_mod, "_window_alpha",
                        lambda *a: {"alpha": -2.0, "return_pct": 0, "buy_hold_pct": 2.0})
    args = SimpleNamespace(strategy="sma_cross", asset="stock", timeframe=None,
                           train_start=None, split="2024-03-01", refresh=False)
    cli_mod.cmd_validate(args, _cfg())
    out = capsys.readouterr().out
    assert "underperforms" in out and "no edge here yet" in out
    monkeypatch.setattr(cli_mod, "_window_alpha", lambda *a: None)
    cli_mod.cmd_validate(args, _cfg())  # no test alphas -> no verdict block
    with pytest.raises(SystemExit, match="No symbols"):
        cli_mod.cmd_validate(args, {})


# --- cmd_dashboard / cmd_email_report / cmd_signal -------------------------------------------- #
def test_cmd_dashboard_open_and_no_open(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli_mod, "write_dashboard", lambda: tmp_path / "d.html")
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    cli_mod.cmd_dashboard(SimpleNamespace(no_open=False), {})
    assert len(opened) == 1 and "browser" in capsys.readouterr().out
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/open" if name == "open" else None)
    cli_mod.cmd_dashboard(SimpleNamespace(no_open=True), {})
    assert "/usr/bin/open" in capsys.readouterr().out
    monkeypatch.setattr("shutil.which", lambda name: None)
    cli_mod.cmd_dashboard(SimpleNamespace(no_open=True), {})
    assert "d.html" in capsys.readouterr().out


def test_cmd_email_report_digest_and_alert(monkeypatch):
    monkeypatch.setattr(cli_mod.email_report_mod, "run_digest", lambda dry_run: 0)
    cli_mod.cmd_email_report(SimpleNamespace(alert_title=None, dry_run=True), {})
    monkeypatch.setattr(cli_mod.email_report_mod, "run_alert_from_stdin", lambda title: 0)
    cli_mod.cmd_email_report(SimpleNamespace(alert_title="oops", dry_run=False), {})
    monkeypatch.setattr(cli_mod.email_report_mod, "run_digest", lambda dry_run: 2)
    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_email_report(SimpleNamespace(alert_title=None, dry_run=False), {})
    assert exc.value.code == 2


def test_cmd_signal_uses_config_exchange(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "get_signal",
                        lambda symbol, asset, interval, exchange: {
                            "symbol": symbol, "exchange": exchange,
                            "recommendation": "BUY", "buy": 1, "sell": 0, "neutral": 0})
    cli_mod.cmd_signal(SimpleNamespace(symbol="BTC/USD", asset="crypto", timeframe=None), _cfg())
    assert "COINBASE" in capsys.readouterr().out
    cli_mod.cmd_signal(SimpleNamespace(symbol="AAPL", asset="stock", timeframe="1h"), _cfg())
    assert "RECOMMENDATION: BUY" in capsys.readouterr().out


# --- paper commands ------------------------------------------------------------------------------- #
class _PaperBrokerFake:
    def __init__(self, positions=None, holding=False):
        self._positions = positions or []
        self._holding = holding
        self.closed = []

    def account(self):
        return {"equity": 100_000.0, "cash": 90_000.0, "buying_power": 180_000.0}

    def positions(self):
        return self._positions

    def is_holding(self, symbol):
        return self._holding

    def close(self, symbol):
        self.closed.append(symbol)
        return "order-1"


def test_cmd_paper_status_empty_and_filled(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake())
    cli_mod.cmd_paper_status(SimpleNamespace(), {})
    assert "none" in capsys.readouterr().out
    positions = [{"symbol": "AAPL", "qty": 1.5, "market_value": None,
                  "unrealized_pl": -5.0, "unrealized_plpc": -3.0}]
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake(positions=positions))
    cli_mod.cmd_paper_status(SimpleNamespace(), {})
    assert "AAPL" in capsys.readouterr().out


def test_cmd_paper_run_dry_and_live(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake())
    monkeypatch.setattr(cli_mod, "recent_bars", lambda *a: "bars")
    res = {"last_price": 150.0, "signal": "BUY", "holding": False, "plpc": None,
           "action": "would_buy", "order_id": None}
    monkeypatch.setattr(cli_mod, "evaluate", lambda *a, **k: res)
    args = SimpleNamespace(symbol="AAPL", asset="stock", strategy="sma_cross",
                           timeframe=None, notional=None, stop_loss=None, dry_run=True)
    cli_mod.cmd_paper_run(args, _cfg())
    assert "dry run" in capsys.readouterr().out
    res.update(plpc=-2.5, action="bought", order_id="o1")
    args.dry_run = False
    args.notional = 500.0
    args.stop_loss = 5.0
    cli_mod.cmd_paper_run(args, _cfg())
    out = capsys.readouterr().out
    assert "P&L -2.50%" in out and "order id:   o1" in out


def test_cmd_paper_close(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake(holding=False))
    cli_mod.cmd_paper_close(SimpleNamespace(symbol="AAPL"), {})
    assert "No open paper position" in capsys.readouterr().out
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake(holding=True))
    cli_mod.cmd_paper_close(SimpleNamespace(symbol="AAPL"), {})
    assert "Closed paper position" in capsys.readouterr().out


def test_config_symbol_index():
    index = cli_mod._config_symbol_index(_cfg())
    assert index["BTCUSD"] == ("BTC/USD", "crypto")
    assert index["AAPL"] == ("AAPL", "stock")


def test_cmd_paper_stops_guards(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake())
    args = SimpleNamespace(run_id=None, stop_loss=0, dry_run=False)
    cli_mod.cmd_paper_stops(args, {"paper": {"stop_loss_pct": 0}})
    assert "disabled" in capsys.readouterr().out
    args.stop_loss = 8
    cli_mod.cmd_paper_stops(args, _cfg())
    assert "no open positions" in capsys.readouterr().out


def test_cmd_paper_stops_breach_and_hold(monkeypatch, capsys):
    positions = [
        {"symbol": "AAPL", "unrealized_plpc": -10.0, "current_price": 90.0},
        {"symbol": "UNKNOWN", "unrealized_plpc": 5.0, "current_price": None},
    ]
    broker = _PaperBrokerFake(positions=positions)
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: broker)
    logged = []
    monkeypatch.setattr(cli_mod, "record_paper_action", logged.append)
    args = SimpleNamespace(run_id=None, stop_loss=None, dry_run=False)
    cli_mod.cmd_paper_stops(args, _cfg())
    out = capsys.readouterr().out
    assert broker.closed == ["AAPL"] and "<-- STOP" in out
    assert logged[0]["asset"] == "stock" and logged[1]["asset"] == ""
    assert logged[1]["last_price"] == ""
    broker.closed.clear()
    args.dry_run = True
    cli_mod.cmd_paper_stops(args, _cfg())
    assert broker.closed == [] and "would be closed" in capsys.readouterr().out


def test_cmd_paper_scan_legacy(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: _PaperBrokerFake())

    def bars(symbol, *a):
        if symbol == "BTC/USD":
            raise RuntimeError("offline")
        return "bars"

    monkeypatch.setattr(cli_mod, "recent_bars", bars)
    monkeypatch.setattr(cli_mod, "evaluate",
                        lambda *a, **k: {"symbol": "AAPL", "signal": "BUY",
                                         "holding": False, "action": "bought"})
    monkeypatch.setattr(cli_mod, "record_paper_action", lambda res: None)
    args = SimpleNamespace(run_id=None, strategy="sma_cross", asset=None,
                           timeframe=None, notional=None, stop_loss=None, dry_run=False)
    cli_mod.cmd_paper_scan(args, _cfg())
    out = capsys.readouterr().out
    assert "ERROR: offline" in out and "1 order(s) placed" in out
    args.dry_run = True
    cli_mod.cmd_paper_scan(args, _cfg())
    assert "would be placed" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No symbols"):
        cli_mod.cmd_paper_scan(args, {})


def test_paper_broker_error_surfaces(monkeypatch):
    def boom():
        raise BrokerError("no keys")

    monkeypatch.setattr(cli_mod, "PaperBroker", boom)
    with pytest.raises(BrokerError):
        cli_mod.cmd_paper_status(SimpleNamespace(), {})
