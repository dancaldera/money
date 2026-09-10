"""Coverage for trading.reporting.dashboard collection and rendering gaps."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pandas as pd
import pytest

from trading.reporting import dashboard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    results, data, repo = tmp_path / "results", tmp_path / "data", tmp_path / "repo"
    results.mkdir()
    data.mkdir()
    (repo / "scripts").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "RESULTS_DIR", results)
    monkeypatch.setattr(dashboard, "DATA_DIR", data)
    monkeypatch.setattr(dashboard, "REPO_DIR", repo)
    return {"results": results, "data": data, "repo": repo}


@pytest.fixture
def no_broker(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("trading.live.PaperBroker", boom)


# --- collectors ---------------------------------------------------------------- #
def test_deployed_strategy_reads_wrapper_or_unknown(dirs):
    assert dashboard._deployed_strategy() == "unknown"
    (dirs["repo"] / "scripts" / "daily_paper_run.sh").write_text('STRATEGY="rsi_meanrev"\n')
    assert dashboard._deployed_strategy() == "rsi_meanrev"
    (dirs["repo"] / "scripts" / "daily_paper_run.sh").write_text("# no strategy here\n")
    assert dashboard._deployed_strategy() == "unknown"


def test_heartbeats_ok_stale_and_corrupt(dirs):
    now = int(time.time())
    (dirs["results"] / ".last_success_paperscan").write_text(str(now))
    (dirs["results"] / ".last_success_stopmonitor").write_text(str(now - 10 * 3600))
    (dirs["results"] / ".last_success_context").write_text("bogus")
    by_label = {h["label"]: h for h in dashboard._heartbeats()}
    assert by_label["Daily signal scan"]["ok"] is True
    assert by_label["Stop-loss monitor"]["ok"] is False
    assert by_label["Shadow context collection"]["when"] == "never"


def test_account_snapshot_or_none(monkeypatch):
    class FakeBroker:
        def account(self):
            return {"equity": 1.0}

        def positions(self):
            return [{"symbol": "AAPL"}]

    monkeypatch.setattr("trading.live.PaperBroker", FakeBroker)
    assert dashboard._account() == {"equity": 1.0, "positions": [{"symbol": "AAPL"}]}
    monkeypatch.setattr("trading.live.PaperBroker", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert dashboard._account() is None


def test_backtests_empty_when_strategy_absent(dirs):
    pd.DataFrame([{"timestamp": "2026-01-01", "symbol": "AAPL", "strategy": "rsi_meanrev",
                   "return_pct": 1.0, "buy_hold_pct": 0.0, "sharpe": 0.1}]
                 ).to_csv(dirs["results"] / "journal.csv", index=False)
    assert dashboard._backtests("sma_cross") == []


def test_coverage_reads_parquet_and_skips_corrupt(dirs):
    df = pd.DataFrame({"Close": [1.0, float("nan")]},
                      index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    df.to_parquet(dirs["data"] / "stock_AAPL.parquet")
    (dirs["data"] / "bogus.parquet").write_bytes(b"not parquet")
    rows = dashboard._coverage()
    assert len(rows) == 1 and rows[0]["nans"] == 1 and rows[0]["rows"] == 2


def test_activity_empty_and_stop_monitor_only(dirs):
    pd.DataFrame(columns=["action", "timestamp", "strategy", "symbol", "signal",
                          "holding"]).to_csv(dirs["results"] / "paper_journal.csv", index=False)
    assert dashboard._activity()["last_run"] == []
    pd.DataFrame([{"timestamp": "2026-08-24T10:00:00+00:00", "symbol": "AAPL",
                   "strategy": "stop-monitor", "signal": "STOP", "holding": True,
                   "action": "stopped"}]).to_csv(dirs["results"] / "paper_journal.csv", index=False)
    act = dashboard._activity()
    assert act["last_run"][0]["action"] == "stopped"
    assert act["last_when"] is not None


def test_activity_collapses_recent_scan_and_tolerates_bad_timestamps(dirs):
    pd.DataFrame([
        {"timestamp": "2026-08-24T10:00:00+00:00", "symbol": "AAPL", "strategy": "sma_cross",
         "signal": "BUY", "holding": False, "action": "bought"},
        {"timestamp": "2026-08-24T10:02:00+00:00", "symbol": "MSFT", "strategy": "sma_cross",
         "signal": "HOLD", "holding": False, "action": "none"},
    ]).to_csv(dirs["results"] / "paper_journal.csv", index=False)
    act = dashboard._activity()
    assert act["counts"] == {"bought": 1, "none": 1}
    assert len(act["last_run"]) == 2
    pd.DataFrame([{"timestamp": "bogus", "symbol": "AAPL", "strategy": "sma_cross",
                   "signal": "HOLD", "holding": False, "action": "none"}
                  ]).to_csv(dirs["results"] / "paper_journal.csv", index=False)
    assert dashboard._activity()["last_when"] is None


def test_run2_summary_states(dirs, no_broker):
    assert dashboard._run2_summary() is None  # no manifest
    config_dir = dirs["repo"] / "config"
    config_dir.mkdir()
    shutil.copy(ROOT / "config" / "run2.yaml", config_dir / "run2.yaml")
    assert dashboard._run2_summary() is None  # manifest but no ledger yet
    from trading.run2 import RunLedger, load_run_config

    cfg = load_run_config(config_dir / "run2.yaml")
    ledger = RunLedger(dirs["results"] / cfg.run_id / "ledger.sqlite")
    ledger.initialize_run(cfg, "acct")
    ledger.close()
    summary = dashboard._run2_summary()
    assert summary["run_id"] == "run2" and "baseline" in summary["portfolios"]
    (config_dir / "run2.yaml").write_text("strategy: [broken")
    assert dashboard._run2_summary() is None  # unreadable manifest stays best-effort


# --- collect_context insight branches ------------------------------------------- #
def test_collect_context_full_insights(dirs, monkeypatch):
    (dirs["repo"] / "scripts" / "daily_paper_run.sh").write_text('STRATEGY="sma_cross"\n')

    class FakeBroker:
        def account(self):
            return {"equity": 100_000.0, "cash": 90_000.0, "buying_power": 180_000.0}

        def positions(self):
            return [{"symbol": "AAPL", "qty": 1, "market_value": 100.0,
                     "unrealized_pl": 5.0, "unrealized_plpc": 5.0}]

    monkeypatch.setattr("trading.live.PaperBroker", FakeBroker)
    pd.DataFrame([{"timestamp": "2026-01-01", "symbol": "AAPL", "strategy": "sma_cross",
                   "return_pct": 10.0, "buy_hold_pct": 4.0, "sharpe": 0.5}]
                 ).to_csv(dirs["results"] / "journal.csv", index=False)
    pd.DataFrame([{"timestamp": "2026-08-24T10:00:00+00:00", "symbol": "AAPL",
                   "strategy": "sma_cross", "signal": "BUY", "holding": False,
                   "action": "bought"}]).to_csv(dirs["results"] / "paper_journal.csv", index=False)
    df = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    fresh = dirs["data"] / "fresh.parquet"
    df.to_parquet(fresh)
    stale = dirs["data"] / "stale.parquet"
    pd.DataFrame({"Close": [1.0, float("nan")]}, index=df.index).to_parquet(stale)
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    ctx = dashboard.collect_context()
    assert ctx["strategy"] == "sma_cross"
    assert any("stale" in s for s in ctx["insights"])
    assert any("Lifetime paper activity" in s for s in ctx["insights"])
    fresh.unlink()
    stale.unlink()
    df.to_parquet(fresh)  # all fresh and NaN-free now
    ctx = dashboard.collect_context()
    assert any("fresh and NaN-free" in s for s in ctx["insights"])


# --- renderers -------------------------------------------------------------------- #
def test_account_card_positions_and_empty():
    acct = {"equity": 100_000.0, "cash": 90_000.0,
            "positions": [{"symbol": "AAPL", "market_value": 100.0,
                           "unrealized_pl": 5.0, "unrealized_plpc": 5.0}]}
    html = dashboard._account_card(acct)
    assert "AAPL" in html and "$100,000" in html
    assert "no open positions" in dashboard._account_card({**acct, "positions": []})


def test_empty_cards():
    assert "No backtest runs" in dashboard._backtests_card([], "sma_cross")
    assert "No cached data" in dashboard._coverage_card([])
    assert "No paper activity" in dashboard._activity_card({"last_run": []})
    assert "Not initialized" in dashboard._run2_card(None)


def test_run2_card_with_halted_report():
    report = {"status": "halted", "halt_reason": "drawdown_halt:5.1%",
              "portfolios": {"baseline": {"return_pct": -1.5, "completed_trades": 3,
                                          "max_drawdown_pct": -5.1}}}
    html = dashboard._run2_card(report)
    assert "halted" in html and "drawdown_halt" in html


def test_write_dashboard_writes_file(dirs, no_broker):
    path = dashboard.write_dashboard()
    assert path == dirs["results"] / "dashboard.html"
    assert "<!DOCTYPE html>" in path.read_text()
    custom = dashboard.write_dashboard(dirs["results"] / "sub" / "dash.html")
    assert custom.exists()
