"""Tests for the halt-episode analysis.

``scripts/analysis_halt_episodes.py`` backs the claim that the opt-in recovery
rule is cheap — it prices a replay's drawdown halt in **sessions flat** and says
which branch of the rule ended each one. The script re-uses
``trading.run2.risk.halt_action``, so these tests pin the state machine it drives
(latch vs drawdown recovery vs calendar recovery) on synthetic equity curves and
a synthetic manifest; the script itself reads the gitignored ``results/``.
"""

from __future__ import annotations

import importlib.util
from copy import deepcopy

import pandas as pd
import pytest
import yaml

from trading.run2.config import load_run_config

from .run2_helpers import ROOT, run2_config

_SPEC = importlib.util.spec_from_file_location(
    "analysis_halt_episodes", ROOT / "scripts" / "analysis_halt_episodes.py"
)
assert _SPEC and _SPEC.loader
halts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(halts)


def _cfg(tmp_path, **portfolio):
    raw = deepcopy(run2_config().raw)
    raw.update(run_id="exp-halt-test")
    raw["portfolio"].update(portfolio)
    path = tmp_path / "exp.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_run_config(path, strict=False)


def _equity(values, start="2026-01-01"):
    index = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series([float(v) for v in values], index=index)


def test_a_latch_never_resumes(tmp_path):
    """No recovery keys (run2/run3 default): the desk freezes at the halt."""
    cfg = _cfg(tmp_path)
    assert cfg.portfolio.halt_recovery_drawdown_pct is None
    equity = _equity([100.0] * 5 + [94.0] + [94.0] * 40)

    episodes = halts.halt_episodes(equity, cfg)

    assert len(episodes) == 1
    row = episodes.iloc[0]
    assert row["halt_date"] == pd.Timestamp("2026-01-06")
    assert pd.isna(row["resume_date"]) and row["resumed_by"] == "never"
    assert row["halt_dd_pct"] == pytest.approx(-6.0)
    assert row["flat_days"] == 40


def test_drawdown_recovery_fires_when_the_desk_is_not_flat(tmp_path):
    """The dd branch only has a chance if equity is still moving."""
    cfg = _cfg(tmp_path, halt_recovery_drawdown_pct=2.5)
    equity = _equity([100.0] * 5 + [94.5, 98.0] + [98.0] * 3)

    row = halts.halt_episodes(equity, cfg).iloc[0]

    assert row["resumed_by"] == "dd"
    assert row["resume_date"] == pd.Timestamp("2026-01-07")
    assert row["flat_days"] == 1


def test_calendar_recovery_resumes_a_flat_desk(tmp_path):
    """A halted desk is flat, so drawdown never falls back: the cooldown decides."""
    cfg = _cfg(tmp_path, halt_recovery_drawdown_pct=2.5, halt_recovery_days=20)
    equity = _equity([100.0] * 5 + [94.0] * 41)

    row = halts.halt_episodes(equity, cfg).iloc[0]

    assert row["resumed_by"] == "calendar"
    assert row["halt_date"] == pd.Timestamp("2026-01-06")
    assert row["resume_date"] == pd.Timestamp("2026-01-26")  # 20 calendar days
    assert row["flat_days"] == 20
    assert row["deepest_dd_pct"] == pytest.approx(-6.0)


def test_a_path_that_never_halts_reports_no_episode(tmp_path):
    cfg = _cfg(tmp_path, halt_recovery_drawdown_pct=2.5, halt_recovery_days=20)
    episodes = halts.halt_episodes(_equity([100.0] * 5 + [97.5] * 10), cfg)
    assert episodes.empty


def test_a_second_halt_after_a_resume_is_a_new_episode(tmp_path):
    """The resume re-baselines the high water, exactly like the replay."""
    cfg = _cfg(tmp_path, halt_recovery_drawdown_pct=2.5, halt_recovery_days=5)
    equity = _equity(
        [100.0] * 3  # peak at 100
        + [94.0] * 6  # halt; resumed by the 5d cooldown (high water re-baselines to 94)
        + [89.0] * 6  # a fresh 5%+ drawdown from the new peak is a second halt
    )

    episodes = halts.halt_episodes(equity, cfg)

    assert len(episodes) == 2
    assert list(episodes["resumed_by"]) == ["calendar", "calendar"]
    assert episodes.iloc[0]["halt_date"] == pd.Timestamp("2026-01-04")
    assert episodes.iloc[0]["resume_date"] == pd.Timestamp("2026-01-09")
    assert episodes.iloc[1]["halt_date"] == pd.Timestamp("2026-01-10")
    assert episodes.iloc[1]["resume_date"] == pd.Timestamp("2026-01-15")
    assert list(episodes["flat_days"]) == [5, 5]


def test_load_equity_reads_an_artifact_equity_csv(tmp_path):
    artifact = tmp_path / "portfolio-backtest"
    artifact.mkdir()
    (artifact / "equity.csv").write_text("date,equity\n2026-01-01,100000.0\n2026-01-02,101000.0\n")

    series = halts.load_equity(artifact)

    assert list(series.index) == [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")]
    assert series.iloc[-1] == 101000.0
