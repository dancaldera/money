"""Coverage for every RunConfigError branch in trading.run2.config."""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from trading.run2.config import RunConfig, RunConfigError, load_run_config

from .run2_helpers import run2_config


def _write_bad_manifest(tmp_path, mutate):
    raw = deepcopy(run2_config().raw)
    mutate(raw)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _set_crypto_symbols(raw, symbols):
    raw["crypto"]["symbols"] = list(symbols)


VALID_CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "LINK/USD", "DOGE/USD", "AVAX/USD", "AAVE/USD"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda raw: raw.pop("strategy"), "Missing config.strategy"),
        (lambda raw: raw["strategy"].update(name="sma_cross", fast_window=30, slow_window=30), "SMA windows"),
        (lambda raw: raw["strategy"].update(timeframe="1h"), "frozen to closed daily bars"),
        (lambda raw: raw.update(run_id="has space"), "no whitespace"),
        (lambda raw: raw.update(starting_equity=0), "starting_equity must be positive"),
        (lambda raw: raw["portfolio"].update(max_gross_exposure=200_000), "cannot exceed starting equity"),
        (lambda raw: raw["portfolio"].update(drawdown_halt_pct=0), "drawdown_halt_pct"),
        (lambda raw: raw["portfolio"].update(correlation_threshold=2), "correlation_threshold"),
        (lambda raw: _set_crypto_symbols(raw, []), "non-empty"),
        (lambda raw: _set_crypto_symbols(raw, ["AAPL", *VALID_CRYPTO[1:]]), "unique"),
        (lambda raw: raw["research"].update(arms=["baseline"]), "research.arms"),
        (lambda raw: raw["research"].update(sec_lookback_hours=0), "SEC lookback"),
        (lambda raw: raw["research"].update(feature_max_age_hours=0), "feature_max_age_hours"),
        (lambda raw: raw["strategy"].update(stop_loss_pct=9), "frozen values changed"),
        (lambda raw: raw["strategy"].update(require_fresh_cross_after_stop=False), "fresh SMA cross"),
        (lambda raw: _set_crypto_symbols(raw, ["BTC/USD", *VALID_CRYPTO[1:-1], "XRP/USD"]), "17-symbol baseline"),
    ],
)
def test_manifest_validation_branches(tmp_path, mutate, match):
    with pytest.raises(RunConfigError, match=match):
        load_run_config(_write_bad_manifest(tmp_path, mutate))


def test_non_mapping_manifest_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(RunConfigError, match="YAML mapping"):
        load_run_config(path)


def test_gap_limit_and_symbols_shape():
    cfg: RunConfig = run2_config()
    assert cfg.gap_limit_pct("crypto") == cfg.execution.crypto_gap_limit_pct
    assert cfg.gap_limit_pct("stock") == cfg.execution.stock_gap_limit_pct
    assert len(cfg.symbols) == len(cfg.crypto_symbols) + len(cfg.stock_symbols)
