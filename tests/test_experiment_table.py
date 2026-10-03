"""Tests for ``scripts/analysis_experiment_table.py``.

The script backs the comparison tables in ``docs/experiments.md``: it reads the
gitignored ``results/<run_id>/portfolio-backtest/`` artifacts, so its primitives
are pinned here on synthetic fixtures instead of the real artifacts. The
regression that matters is the *clock guard*: a sweep replayed across a cache
refresh must be flagged, because rows measured on different bars are not a
comparison (the docs carry two ladders for exactly that reason).
"""

from __future__ import annotations

import importlib.util
import json
import math

import pytest

from .run2_helpers import ROOT

_SPEC = importlib.util.spec_from_file_location(
    "analysis_experiment_table", ROOT / "scripts" / "analysis_experiment_table.py"
)
assert _SPEC and _SPEC.loader
table = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(table)

SUMMARY = {
    "return_pct": 10.0,
    "max_drawdown_pct": -2.5,
    "completed_trades": 200.0,
    "win_rate_pct": 30.0,
    "expectancy": 20.0,
    "sharpe": 0.8,
    "profit_factor": 1.8,
    "deflated_sharpe_probability": 0.9,
}


def _replay(tmp_path, run_id, *, summary=None, dates=("2022-01-01", "2026-01-01")):
    art = tmp_path / "results" / run_id / "portfolio-backtest"
    art.mkdir(parents=True)
    (art / "summary.json").write_text(json.dumps(summary or SUMMARY))
    (art / "equity.csv").write_text(
        "date,equity\n" + "\n".join(f"{d},100000.0" for d in dates) + "\n"
    )
    return art


def _manifest(tmp_path, run_id, *, fast=10, slow=30):
    cfg = tmp_path / "configs"
    cfg.mkdir(exist_ok=True)
    (cfg / f"{run_id}.yaml").write_text(
        f"run_id: {run_id}\nstrategy:\n  fast_window: {fast}\n  slow_window: {slow}\n"
    )
    return cfg


def test_row_derives_total_and_return_per_drawdown(tmp_path):
    _replay(tmp_path, "exp-x")
    cfg = _manifest(tmp_path, "exp-x")
    row = table.row(tmp_path / "results", cfg, "exp-x")
    assert row["windows"] == "10/30"
    assert row["return_per_dd"] == pytest.approx(4.0)
    assert row["total"] == pytest.approx(4000.0)  # trades x expectancy
    assert (row["first"], row["last"]) == ("2022-01-01", "2026-01-01")


def test_row_survives_a_missing_manifest(tmp_path):
    _replay(tmp_path, "exp-gone")
    cfg = tmp_path / "configs"
    cfg.mkdir()
    row = table.row(tmp_path / "results", cfg, "exp-gone")
    assert row["windows"] == "-"
    assert row["return_pct"] == pytest.approx(10.0)


def test_load_summary_raises_instead_of_reporting_zero(tmp_path):
    with pytest.raises(FileNotFoundError, match="no replay artifact"):
        table.load_summary(tmp_path / "results", "exp-missing")


def test_zero_drawdown_does_not_divide_by_zero(tmp_path):
    _replay(tmp_path, "exp-flat", summary={**SUMMARY, "max_drawdown_pct": 0.0})
    cfg = _manifest(tmp_path, "exp-flat", fast=5, slow=20)
    row = table.row(tmp_path / "results", cfg, "exp-flat")
    assert math.isnan(row["return_per_dd"])
    assert "nan" in table.format_table([row])  # still renders instead of crashing


def test_clock_warning_flags_a_sweep_measured_on_different_bars(tmp_path):
    _replay(tmp_path, "exp-a")
    _replay(tmp_path, "exp-b", dates=("2022-01-01", "2026-02-01"))
    cfg = _manifest(tmp_path, "exp-a")
    rows = [
        table.row(tmp_path / "results", cfg, "exp-a"),
        table.row(tmp_path / "results", cfg, "exp-b"),
    ]
    assert table.clock_warning(rows) is not None
    assert "different clocks" in table.clock_warning(rows)

    same = [rows[0], dict(rows[0], run_id="exp-c")]
    assert table.clock_warning(same) is None


def test_main_prints_table_sorted_by_return(tmp_path, capsys):
    _replay(tmp_path, "exp-lo", summary={**SUMMARY, "return_pct": 1.0})
    _replay(tmp_path, "exp-hi", summary={**SUMMARY, "return_pct": 9.0})
    cfg = _manifest(tmp_path, "exp-lo")
    _manifest(tmp_path, "exp-hi")
    code = table.main(
        [
            "exp-lo",
            "exp-hi",
            "--results-dir",
            str(tmp_path / "results"),
            "--configs-dir",
            str(cfg),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.index("exp-hi") < out.index("exp-lo")
    assert "WARNING" not in out
