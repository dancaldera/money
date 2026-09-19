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

MANIFEST_DIR = ROOT / "config" / "experiments"
# Discovered at collection time, so a new experiment manifest cannot ship
# without the load/safety pin below running against it.
EXPERIMENTS = sorted(path.stem for path in MANIFEST_DIR.glob("*.yaml"))
KNOWN = {
    "scale-1x",
    "scale-2x",
    "scale-2x-recover",
    "scale-4x",
    "scale-4x-halt10",
    "scale-4x-recover",
    "scale-4x-recover-10d",
    "scale-4x-recover-60d",
    "exp-slots-2x",
    "exp-slots-all",
    "exp-slots-all-2x",
    "exp-slots-all-recover",
    "exp-stocks-only",
    "exp-crypto-only",
}


def test_every_shipped_experiment_manifest_is_discovered():
    """A new manifest must be picked up by the parametrized pin below."""
    assert KNOWN <= set(EXPERIMENTS)


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
    if stem == "scale-1x":
        # Deliberately a copy of the frozen values under another run_id: the
        # strict loader accepts it (nothing changed), which is exactly what
        # makes it the reference row of the deployment ladders.
        assert load_run_config(path).portfolio == run2_config().portfolio
        return
    # Any other manifest changes a frozen value, so a live command (strict
    # loader) must refuse it.
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)


# --- registered live runs (opened by human decision) --------------------------- #


def _run3_values(raw):
    """Mutate a run2 manifest into run3's blessed parameter set."""
    raw.update(run_id="run3")
    raw["portfolio"].update(
        max_positions=17,
        max_gross_exposure=10625,
        max_crypto_positions=8,
        max_crypto_exposure=5000,
        max_stock_positions=9,
        max_stock_exposure=5625,
        halt_recovery_drawdown_pct=2.5,
        halt_recovery_days=20,
    )
    return raw


def test_registered_live_run_loads_with_its_own_baseline(tmp_path):
    """A registered live run (run3) passes the strict loader at its own values."""
    path = _manifest(tmp_path, _run3_values)
    cfg = load_run_config(path)
    assert cfg.run_id == "run3"
    assert cfg.portfolio.max_positions == 17
    assert cfg.portfolio.halt_recovery_drawdown_pct == 2.5


def test_registered_live_run_still_pins_its_values(tmp_path):
    """A registered run's manifest may never drift from its baseline."""
    path = _manifest(
        tmp_path,
        lambda raw: _run3_values(raw)["strategy"].update(stop_loss_pct=9),
    )
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)


def test_live_values_under_another_run_id_stay_non_live(tmp_path):
    """run3's exact value set must not become loadable under a different id."""
    path = _manifest(tmp_path, lambda raw: _run3_values(raw).update(run_id="exp-lookalike"))
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)
