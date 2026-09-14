"""The experiment path: research-only manifests with non-frozen parameters.

The audited live run must stay untouched: any manifest that reuses the live
run_id, or that a live command loads with the strict loader, must fail.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from trading.run2.config import LIVE_RUN_ID, RunConfigError, load_run_config

from .run2_helpers import ROOT, run2_config

EXPERIMENTS = ["scale-2x", "scale-4x", "scale-4x-halt10"]


def _manifest(tmp_path, mutate, name="exp.yaml"):
    raw = deepcopy(run2_config().raw)
    mutate(raw)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw))
    return path


def test_rescaled_manifest_is_strict_by_default(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: raw["portfolio"].update(position_notional=1250, max_gross_exposure=10000),
    )
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)


def test_experiment_manifest_loads_rescaled_portfolio(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: (
            raw.update(run_id="exp-test"),
            raw["portfolio"].update(position_notional=1250, max_gross_exposure=10000),
        ),
    )
    cfg = load_run_config(path, strict=False)
    assert cfg.run_id == "exp-test"
    assert cfg.portfolio.position_notional == 1250
    assert cfg.portfolio.max_gross_exposure == 10000
    assert cfg.fingerprint != run2_config().fingerprint


def test_experiment_manifest_may_change_strategy_windows(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: (raw.update(run_id="exp-windows"), raw["strategy"].update(fast_window=20, slow_window=50)),
    )
    cfg = load_run_config(path, strict=False)
    assert (cfg.strategy.fast_window, cfg.strategy.slow_window) == (20, 50)


def test_experiment_manifest_cannot_reuse_live_run_id(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: raw["portfolio"].update(position_notional=1250, max_gross_exposure=10000),
    )
    with pytest.raises(RunConfigError, match="must not reuse the live run_id"):
        load_run_config(path, strict=False)


def test_experiment_manifest_still_enforces_safety(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: (
            raw.update(run_id="exp-margin"),
            raw["execution"].update(use_margin=True),
        ),
    )
    with pytest.raises(RunConfigError, match="paper-only with margin disabled"):
        load_run_config(path, strict=False)


def test_experiment_manifest_still_enforces_exposure_consistency(tmp_path):
    path = _manifest(
        tmp_path,
        lambda raw: (
            raw.update(run_id="exp-overgross"),
            raw["portfolio"].update(position_notional=2500, max_gross_exposure=10000),
        ),
    )
    with pytest.raises(RunConfigError, match="exceeds max_gross_exposure"):
        load_run_config(path, strict=False)


@pytest.mark.parametrize("stem", EXPERIMENTS)
def test_shipped_experiment_manifests_load(stem):
    """Every committed experiment manifest must load on the research path."""
    path = ROOT / "config" / "experiments" / f"{stem}.yaml"
    cfg = load_run_config(path, strict=False)
    assert cfg.run_id != LIVE_RUN_ID
    assert cfg.execution.paper_only and not cfg.execution.use_margin
    assert cfg.portfolio.position_notional * cfg.portfolio.max_positions <= cfg.portfolio.max_gross_exposure
    # A live command (strict loader) must refuse the same manifest.
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)
