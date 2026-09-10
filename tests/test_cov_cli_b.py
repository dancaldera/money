"""CLI coverage B: run2 loading, run commands, parser wiring, main()."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import trading.cli as cli_mod
from trading.live import BrokerError
from trading.run2.service import RunSafetyError

from .run2_helpers import ohlcv, run2_config


class _LedgerFake:
    def __init__(self, decisions=None):
        self._decisions = decisions or []
        self.closed = False
        self.path = "/tmp/fake-ledger.sqlite"

    def decisions(self, *a, **k):
        return self._decisions

    def assert_manifest(self, cfg):
        return {"started_at": "2026-01-01T00:00:00+00:00"}

    def close(self):
        self.closed = True


def _patch_run2(monkeypatch, run_cfg=None, ledger=None, service=None):
    run_cfg = run_cfg if run_cfg is not None else run2_config()
    ledger = ledger if ledger is not None else _LedgerFake()
    service = service if service is not None else SimpleNamespace()
    monkeypatch.setattr(cli_mod, "_run2", lambda *a, **k: (run_cfg, ledger, service))
    return run_cfg, ledger, service


# --- _run2 / _run2_bars --------------------------------------------------------------- #
def test_run2_loader_defaults_and_broker(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "RESULTS_DIR", tmp_path)
    args = SimpleNamespace(run_config=None, run_id=None)
    run_cfg, ledger, service = cli_mod._run2(args)
    try:
        assert run_cfg.run_id == "run2" and service.broker is None
        assert str(ledger.path).startswith(str(tmp_path))
    finally:
        ledger.close()
    monkeypatch.setattr(cli_mod, "PaperBroker", lambda: "fake-broker")
    run_cfg, ledger, service = cli_mod._run2(args, with_broker=True)
    try:
        assert service.broker == "fake-broker"
    finally:
        ledger.close()


def test_run2_loader_rejects_wrong_run_id(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "RESULTS_DIR", tmp_path)
    args = SimpleNamespace(run_config=None, run_id="other")
    with pytest.raises(RunSafetyError, match="does not match manifest"):
        cli_mod._run2(args)


def test_run2_bars_recent_and_full_history(monkeypatch):
    keys = []

    def fake_cache(key, fn, refresh):
        keys.append((key, refresh))
        return fn()

    frame = ohlcv([100.0, 101.0, 102.0])
    monkeypatch.setattr(cli_mod, "load_or_fetch", fake_cache)
    monkeypatch.setattr(cli_mod, "fetch_alpaca_daily", lambda *a: frame)
    monkeypatch.setattr(cli_mod, "drop_forming_bar", lambda f, tf: f)
    cfg = run2_config()
    bars = cli_mod._run2_bars(cfg, {}, recent=True)
    assert set(bars) == {s for s, _ in cfg.symbols} and "SPY" not in bars
    assert all(refresh for _, refresh in keys)
    keys.clear()
    bars = cli_mod._run2_bars(cfg, {}, refresh=True, recent=False)
    assert "SPY" in bars and len(bars) == len(cfg.symbols) + 1


# --- paper-scan / paper-stops run2 paths ------------------------------------------------ #
def test_paper_scan_run2_rejects_unfrozen_strategy(monkeypatch):
    ledger = _LedgerFake()
    _patch_run2(monkeypatch, ledger=ledger)
    args = SimpleNamespace(run_id="run2", strategy="rsi_meanrev", dry_run=False)
    with pytest.raises(SystemExit, match="frozen to strategy"):
        cli_mod.cmd_paper_scan(args, {})
    assert ledger.closed


def _scan_rows():
    return [
        {"symbol": "AAPL", "asset": "stock", "signal": "BUY", "action": "buy_intent",
         "reason": "allowed", "status": "pending", "portfolio": "baseline"},
        {"symbol": "AAPL", "asset": "stock", "signal": "BUY", "action": "none",
         "reason": "missing_regime", "status": "recorded", "portfolio": "shadow_regime"},
    ]


def test_paper_scan_run2_dry_and_live(monkeypatch, capsys):
    service = SimpleNamespace(scan=lambda bars, record: _scan_rows())
    ledger = _LedgerFake(decisions=[{"id": 1}])
    _patch_run2(monkeypatch, ledger=ledger, service=service)
    monkeypatch.setattr(cli_mod, "_run2_bars", lambda *a, **k: {"AAPL": "bars"})
    args = SimpleNamespace(run_id="run2", strategy="sma_cross", dry_run=True)
    cli_mod.cmd_paper_scan(args, {})
    assert "would be created" in capsys.readouterr().out
    args.dry_run = False
    cli_mod.cmd_paper_scan(args, {})
    assert "1 baseline intent(s) pending" in capsys.readouterr().out
    assert ledger.closed


def test_paper_stops_run2_path(monkeypatch, capsys):
    service = SimpleNamespace(
        reconcile=lambda: {"new_fills": 1, "new_fees": 0, "halted": False},
        check_stops=lambda dry_run: [
            {"symbol": "AAPL", "plpc": -9.5, "action": "stopped"},
            {"symbol": "MSFT", "action": "none"},
        ],
    )
    _patch_run2(monkeypatch, service=service)
    cli_mod.cmd_paper_stops(SimpleNamespace(run_id="run2", dry_run=False), {})
    out = capsys.readouterr().out
    assert "P&L=-9.50%" in out and "fills=1" in out


# --- run-init / health --------------------------------------------------------------------- #
def test_cmd_run_init_resume_and_fresh(monkeypatch, capsys):
    service = SimpleNamespace(resume=lambda: {
        "equity": 99_000.0, "cash": 98_000.0, "drawdown_pct": 1.0,
        "imported": [{"symbol": "AAPL", "asset": "stock", "qty": "1", "avg_entry": "100"}],
        "warnings": ["position_outside_universe:SPY"],
    })
    ledger = _LedgerFake()
    _patch_run2(monkeypatch, ledger=ledger, service=service)
    cli_mod.cmd_run_init(SimpleNamespace(resume=True), {})
    out = capsys.readouterr().out
    assert "Resumed" in out and "imported position: AAPL" in out and "warning:" in out
    service.initialize = lambda: {"equity": 100_000.0}
    cli_mod.cmd_run_init(SimpleNamespace(resume=False), {})
    assert "Initialized frozen paper run" in capsys.readouterr().out


def test_cmd_run_health(monkeypatch, capsys):
    service = SimpleNamespace(health=lambda: {"run_id": "run2", "halted": False})
    _patch_run2(monkeypatch, service=service)
    cli_mod.cmd_run_health(SimpleNamespace(), {})
    assert "halted: False" in capsys.readouterr().out


# --- collect-context ---------------------------------------------------------------------------- #
class _CollectorFake:
    def __init__(self, *a):
        pass

    def collect_regimes(self, frames):
        return {"equity": {"value": 4, "flags": {"a": True}},
                "crypto": {"value": 2, "flags": {}}}

    def collect_news(self, articles):
        return {"AAPL": 0.5, "MSFT": 0.0}

    def collect_sec_filings(self, filings):
        return {"AAPL": 1, "MSFT": 0}


def test_cmd_collect_context_full_and_skipped(monkeypatch, capsys, tmp_path):
    import trading.run2.context as ctx_mod

    _patch_run2(monkeypatch)
    monkeypatch.setattr(cli_mod, "_run2_bars", lambda *a, **k: {})
    monkeypatch.setattr(cli_mod, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ctx_mod, "ContextCollector", _CollectorFake)
    monkeypatch.setattr(ctx_mod, "fetch_alpaca_news", lambda cfg: [{"h": 1}, {"h": 2}])
    monkeypatch.setattr(ctx_mod, "fetch_sec_filings", lambda cfg: [{"s": 1}])
    args = SimpleNamespace(refresh=False, skip_news=False, skip_sec=False)
    cli_mod.cmd_collect_context(args, {})
    out = capsys.readouterr().out
    assert "equity  4/4" in out and "2 article(s)" in out and "1 filing(s)" in out
    args = SimpleNamespace(refresh=True, skip_news=True, skip_sec=True)
    cli_mod.cmd_collect_context(args, {})
    out = capsys.readouterr().out
    assert "article(s)" not in out and "filing(s)" not in out


# --- execute-intents ---------------------------------------------------------------------------------- #
def test_execute_intents_dry_run_with_deferred_market(monkeypatch, capsys):
    def pending(asset, dry_run):
        return [{"action": "deferred_market_closed"}] if asset == "stock" else [
            {"decision_id": "d1", "action": "would_buy"}]

    service = SimpleNamespace(execute_pending=pending)
    _patch_run2(monkeypatch, service=service)
    args = SimpleNamespace(asset=None, dry_run=True)
    cli_mod.cmd_execute_intents(args, {})
    assert "would_buy" in capsys.readouterr().out


def test_execute_intents_explicit_asset_reraises(monkeypatch):
    def boom(asset, dry_run):
        raise RuntimeError("broker down")

    _patch_run2(monkeypatch, service=SimpleNamespace(execute_pending=boom))
    with pytest.raises(RuntimeError, match="broker down"):
        cli_mod.cmd_execute_intents(SimpleNamespace(asset="stock", dry_run=False), {})


def test_execute_intents_defers_failed_stock_leg(monkeypatch, capsys):
    def pending(asset, dry_run):
        if asset == "stock":
            raise RuntimeError("closed")
        return [{"decision_id": "d1", "action": "submitted"}]

    service = SimpleNamespace(
        execute_pending=pending,
        broker=SimpleNamespace(latest_price=lambda s, a: 100.0),
        simulate_shadow=lambda prices: [{"shadow": True, "prices": prices}],
    )
    rows = [{"symbol": "BTC/USD", "asset": "crypto"}, {"symbol": "AAPL", "asset": "stock"}]
    ledger = _LedgerFake(decisions=rows)
    _patch_run2(monkeypatch, ledger=ledger, service=service)
    cli_mod.cmd_execute_intents(SimpleNamespace(asset=None, dry_run=False), {})
    out = capsys.readouterr().out
    assert "stock execution deferred: closed" in out
    assert "BTC/USD" in out and "AAPL" in out


def test_execute_intents_skips_shadow_pricing_for_deferred_assets(monkeypatch, capsys):
    def pending(asset, dry_run):
        if asset == "stock":
            return [{"action": "deferred_market_closed"}]
        return [{"decision_id": "d1", "action": "submitted"}]

    service = SimpleNamespace(
        execute_pending=pending,
        broker=SimpleNamespace(latest_price=lambda s, a: 100.0),
        simulate_shadow=lambda prices: [{"shadow": True, "prices": prices}],
    )
    rows = [{"symbol": "BTC/USD", "asset": "crypto"}, {"symbol": "AAPL", "asset": "stock"}]
    ledger = _LedgerFake(decisions=rows)
    _patch_run2(monkeypatch, ledger=ledger, service=service)
    cli_mod.cmd_execute_intents(SimpleNamespace(asset=None, dry_run=False), {})
    shadow_line = capsys.readouterr().out.split("'shadow': True")[-1]
    assert "BTC/USD" in shadow_line and "AAPL" not in shadow_line


def test_execute_intents_no_eligible_intents(monkeypatch, capsys):
    service = SimpleNamespace(execute_pending=lambda asset, dry_run: [],
                              simulate_shadow=lambda prices: [])
    _patch_run2(monkeypatch, ledger=_LedgerFake(), service=service)
    cli_mod.cmd_execute_intents(SimpleNamespace(asset="crypto", dry_run=False), {})
    assert "no eligible intents" in capsys.readouterr().out


# --- reconcile / run-report / portfolio-backtest -------------------------------------------------------- #
def test_cmd_reconcile_run(monkeypatch, capsys):
    service = SimpleNamespace(reconcile=lambda: {"new_fills": 2, "halted": False})
    _patch_run2(monkeypatch, service=service)
    cli_mod.cmd_reconcile_run(SimpleNamespace(), {})
    assert "new_fills: 2" in capsys.readouterr().out


def test_cmd_run_report_json_and_text(monkeypatch, capsys):
    import trading.run2.portfolio as portfolio_mod
    import trading.run2.reporting as reporting_mod

    _patch_run2(monkeypatch)
    frame = ohlcv([100.0, 101.0])
    monkeypatch.setattr(cli_mod, "_run2_bars", lambda *a, **k: {"AAPL": frame, "SPY": frame})
    monkeypatch.setattr(portfolio_mod, "primary_benchmark", lambda *a, **k: "bench")
    monkeypatch.setattr(reporting_mod, "collect_run_report",
                        lambda cfg, ledger, marks, bench: {"m": marks, "b": bench})
    monkeypatch.setattr(reporting_mod, "render_run_report", lambda report: "REPORT")
    cli_mod.cmd_run_report(SimpleNamespace(json=True), {})
    assert '"b": "bench"' in capsys.readouterr().out
    cli_mod.cmd_run_report(SimpleNamespace(json=False), {})
    assert "REPORT" in capsys.readouterr().out


def test_cmd_portfolio_backtest_empty_and_trading(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli_mod, "RESULTS_DIR", tmp_path)
    cfg = run2_config()
    _patch_run2(monkeypatch, run_cfg=cfg, ledger=_LedgerFake())
    monkeypatch.setattr(cli_mod, "_run2_bars", lambda *a, **k: {})
    cli_mod.cmd_portfolio_backtest(SimpleNamespace(refresh=False), {})
    assert "benchmark_return_pct: 0.0" in capsys.readouterr().out
    assert (tmp_path / "run2" / "portfolio-backtest" / "summary.json").exists()
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0, 20.0])
    monkeypatch.setattr(cli_mod, "_run2_bars", lambda *a, **k: {"AAPL": frame, "SPY": frame})
    cli_mod.cmd_portfolio_backtest(SimpleNamespace(refresh=True), {})
    assert "artifacts:" in capsys.readouterr().out


# --- parser + main ---------------------------------------------------------------------------------------------- #
def test_build_parser_wires_every_command():
    parser = cli_mod.build_parser()
    cases = [
        ["backtest", "--symbol", "A", "--asset", "stock", "--strategy", "sma_cross"],
        ["scan", "--strategy", "sma_cross"],
        ["validate"],
        ["dashboard"],
        ["signal", "--symbol", "A", "--asset", "stock"],
        ["paper-status"],
        ["paper-run", "--symbol", "A", "--asset", "stock", "--strategy", "sma_cross"],
        ["paper-close", "--symbol", "A"],
        ["paper-scan", "--strategy", "sma_cross"],
        ["paper-stops"],
        ["run-init"],
        ["health"],
        ["collect-context"],
        ["execute-intents"],
        ["reconcile"],
        ["run-report"],
        ["portfolio-backtest"],
        ["email-report"],
    ]
    funcs = {parser.parse_args(argv).func.__name__ for argv in cases}
    assert len(funcs) == 18


def test_main_success_and_error_mapping(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "get_signal",
                        lambda *a, **k: {"symbol": "A", "exchange": "E", "recommendation": "BUY",
                                         "buy": 1, "sell": 0, "neutral": 0})
    monkeypatch.setattr("sys.argv", ["money", "signal", "--symbol", "A", "--asset", "stock"])
    cli_mod.main()
    assert "RECOMMENDATION" in capsys.readouterr().out

    def boom():
        raise BrokerError("no keys")

    monkeypatch.setattr(cli_mod, "PaperBroker", boom)
    monkeypatch.setattr("sys.argv", ["money", "paper-status"])
    with pytest.raises(SystemExit, match="Paper trading error"):
        cli_mod.main()

    def run2_boom(*a, **k):
        raise RunSafetyError("unsafe")

    monkeypatch.setattr(cli_mod, "_run2", run2_boom)
    monkeypatch.setattr("sys.argv", ["money", "run-init"])
    with pytest.raises(SystemExit, match="Run 2 error"):
        cli_mod.main()

    def value_boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(cli_mod, "get_signal", value_boom)
    monkeypatch.setattr("sys.argv", ["money", "signal", "--symbol", "A", "--asset", "stock"])
    with pytest.raises(ValueError, match="unexpected"):
        cli_mod.main()
