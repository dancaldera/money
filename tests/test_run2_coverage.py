"""Tests for scan coverage (``trading.run2.coverage``).

``paper-scan`` only evaluates the newest complete bar, so a bar that is never the
newest one when a run looks is never evaluated at all. These tests pin the
detection that makes such a loss visible instead of silent, and pin the flat
``money health`` line shape that ``scripts/health_check.sh`` parses.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pandas as pd
import pytest

import trading.cli as cli_mod
import trading.run2.coverage as coverage
from trading.run2.coverage import (
    ScopeCoverage,
    bar_key,
    cached_bars,
    coverage_report,
    health_lines,
    scope_coverage,
    symbol_coverage,
)

DAY = pd.Timedelta(days=1)


def _frame(end: str, periods: int = 5) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(end), periods=periods, freq="D")
    return pd.DataFrame({"Close": [100.0] * periods}, index=index)


# --- bar_key --------------------------------------------------------------------------- #
def test_bar_key_normalises_ledger_offset_and_naive_index():
    assert bar_key("2026-09-12T00:00:00+00:00") == bar_key(pd.Timestamp("2026-09-12"))
    assert bar_key("2026-09-12T00:00:00+00:00").isoformat() == "2026-09-12T00:00:00"


# --- symbol coverage ------------------------------------------------------------------- #
def test_symbol_coverage_flags_a_skipped_bar_and_a_lag():
    frame = _frame("2026-09-12")  # 09-08 .. 09-12
    recorded = ["2026-09-08T00:00:00+00:00", "2026-09-12T00:00:00+00:00"]
    cov = symbol_coverage("BTC/USD", "crypto", frame, recorded)
    # 09-09 .. 09-11 were never evaluated; the newest closed bar is recorded.
    assert [g.day for g in cov.gaps] == [9, 10, 11]
    assert cov.behind is False
    assert cov.recent_gap is True
    assert bar_key(cov.newest_closed) == bar_key("2026-09-12")

    lagging = symbol_coverage("BTC/USD", "crypto", frame, ["2026-09-11T00:00:00+00:00"])
    assert lagging.behind is True  # newest closed bar (09-12) not evaluated yet


def test_symbol_coverage_ignores_history_before_the_first_decision():
    frame = _frame("2026-09-12", periods=40)
    cov = symbol_coverage("BTC/USD", "crypto", frame, ["2026-09-12T00:00:00+00:00"])
    assert cov.gaps == ()


def test_symbol_coverage_degrades_without_data_or_decisions():
    empty = symbol_coverage("BTC/USD", "crypto", None, [])
    assert empty.newest_closed is None and empty.newest_recorded is None
    assert empty.behind is False and empty.gaps == ()
    assert symbol_coverage("BTC/USD", "crypto", _frame("2026-09-12"), []).behind is True


def test_recent_gap_ignores_an_ancient_skip():
    frame = _frame("2026-09-12", periods=10)
    recorded = [f"2026-09-0{day}T00:00:00+00:00" for day in (3, 4)] + [
        "2026-09-10T00:00:00+00:00",
        "2026-09-11T00:00:00+00:00",
        "2026-09-12T00:00:00+00:00",
    ]
    cov = symbol_coverage("BTC/USD", "crypto", frame, recorded)
    assert [g.day for g in cov.gaps] == [5, 6, 7, 8, 9]  # old skips still on record
    assert cov.recent_gap is False  # ... but they do not alert forever


# --- scope aggregation ----------------------------------------------------------------- #
def test_scope_coverage_reports_the_symbol_that_lagged():
    frame = _frame("2026-09-12")
    symbols = [("AAPL", "stock"), ("BTC/USD", "crypto")]
    bars = {"AAPL": frame, "BTC/USD": frame}
    recorded = {
        "AAPL": ["2026-09-12T00:00:00+00:00", "2026-09-11T00:00:00+00:00"],
        "BTC/USD": ["2026-09-10T00:00:00+00:00", "2026-09-12T00:00:00+00:00"],
    }
    report = coverage_report(symbols, bars, recorded)
    assert sorted(report) == ["crypto", "equity"]
    assert report["equity"].behind is False
    assert report["crypto"].gaps == (bar_key("2026-09-11"),)

    lagging = scope_coverage(
        symbols,
        [
            symbol_coverage("AAPL", "stock", frame, recorded["AAPL"]),
            symbol_coverage("BTC/USD", "crypto", frame, ["2026-09-10T00:00:00+00:00"]),
        ],
    )
    assert lagging["crypto"].behind is True
    assert lagging["equity"].behind is False


def test_health_line_shape_is_parseable_by_the_watchdog():
    scope = ScopeCoverage(
        scope="crypto",
        symbols=8,
        newest_closed=bar_key("2026-09-12"),
        newest_recorded=bar_key("2026-09-11"),
        gaps=(bar_key("2026-09-10"),),
        recent=(bar_key("2026-09-12"), bar_key("2026-09-11"), bar_key("2026-09-10")),
    )
    line = scope.health_line()
    assert line.startswith("  coverage_crypto: behind ")
    for fragment in ("behind=1", "gaps=1", "recent_gaps=1", "missing=2026-09-10T00:00:00"):
        assert fragment in line
    # health_check.sh extracts the scope with: sed -n 's/^ *coverage_\([a-z]*\): .*behind=1.*/\1/p'
    assert line.split(":")[0].strip() == "coverage_crypto"


def test_health_lines_cover_every_scope_sorted():
    symbols = [("AAPL", "stock"), ("BTC/USD", "crypto")]
    frame = _frame("2026-09-12")
    lines = health_lines(symbols, {"AAPL": frame}, {"AAPL": ["2026-09-12T00:00:00+00:00"]})
    assert [line.split()[0] for line in lines] == ["coverage_crypto:", "coverage_equity:"]


# --- cached bars ----------------------------------------------------------------------- #
def _write(tmp_path, name: str, frame: pd.DataFrame, mtime: float) -> None:
    path = tmp_path / name
    frame.to_parquet(path)
    os.utime(path, (mtime, mtime))


def test_cached_bars_takes_the_newest_file_and_drops_the_forming_bar(tmp_path):
    today = pd.Timestamp.now("UTC").normalize().tz_localize(None)
    stale = _frame(str((today - DAY * 5).date()))
    fresh = _frame(str(today.date()), periods=5)  # last bar is still forming (UTC)
    _write(tmp_path, "alpaca_stock_AAPL_1d_2024-01-01.parquet", stale, mtime=1_000_000)
    _write(tmp_path, "alpaca_stock_AAPL_1d_2025-01-01.parquet", fresh, mtime=2_000_000)

    frames = cached_bars([("AAPL", "stock"), ("MSFT", "stock")], data_dir=tmp_path)
    assert list(frames) == ["AAPL"]
    assert frames["AAPL"].index[-1] == today - DAY  # forming bar dropped
    assert cached_bars([("MSFT", "stock")], data_dir=tmp_path) == {}


def test_cached_bars_reads_the_default_data_dir(monkeypatch, tmp_path):
    frame = _frame("2026-09-12")
    _write(tmp_path, "alpaca_crypto_BTC_USD_1d_2025-01-01.parquet", frame, mtime=1_000_000)
    monkeypatch.setattr(coverage, "DATA_DIR", tmp_path)
    frames = cached_bars([("BTC/USD", "crypto")])
    assert frames["BTC/USD"].index[-1] == pd.Timestamp("2026-09-12")


# --- CLI wiring ------------------------------------------------------------------------ #
class _LedgerFake:
    def __init__(self, decisions):
        self._decisions = decisions
        self.closed = False

    def decisions(self, *a, **k):
        return self._decisions

    def close(self):
        self.closed = True


def _patch_run2(monkeypatch, ledger):
    run_cfg = SimpleNamespace(run_id="run2", symbols=(("AAPL", "stock"),))
    service = SimpleNamespace(health=lambda: {"run_id": "run2", "halted": False})
    monkeypatch.setattr(cli_mod, "_run2", lambda *a, **k: (run_cfg, ledger, service))
    return ledger


def test_cmd_run_health_prints_coverage_lines(monkeypatch, capsys, tmp_path):
    frame = _frame("2026-09-12")
    _write(tmp_path, "alpaca_stock_AAPL_1d_2025-01-01.parquet", frame, mtime=1_000_000)
    monkeypatch.setattr(coverage, "DATA_DIR", tmp_path)
    ledger = _patch_run2(
        monkeypatch, _LedgerFake([{"symbol": "AAPL", "bar_end": "2026-09-12T00:00:00+00:00"}])
    )
    cli_mod.cmd_run_health(SimpleNamespace(), {})
    out = capsys.readouterr().out
    assert "halted: False" in out
    assert "coverage_equity: ok newest_closed=2026-09-12T00:00:00" in out
    assert "behind=0" in out and "gaps=0" in out
    assert ledger.closed is True


def test_cmd_run_health_degrades_when_coverage_fails(monkeypatch, capsys):
    def _boom(*a, **k):
        raise RuntimeError("no cache")

    monkeypatch.setattr(coverage, "cached_bars", _boom)
    _patch_run2(monkeypatch, _LedgerFake([]))
    cli_mod.cmd_run_health(SimpleNamespace(), {})
    out = capsys.readouterr().out
    assert "coverage: unavailable (RuntimeError)" in out
