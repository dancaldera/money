"""Coverage for trading.run2.sizing: experiment-only risk-based position sizing."""

from __future__ import annotations

from dataclasses import replace
from math import sqrt

import pandas as pd
import pytest

from trading.run2.config import RunConfigError, load_run_config
from trading.run2.portfolio import simulate_portfolio
from trading.run2.risk import _candidate_notional
from trading.run2.sizing import (
    ANNUALIZATION_DAYS,
    VOL_SCALE_MAX,
    VOL_SCALE_MIN,
    entry_notional,
    realized_vol_pct,
)

from .run2_helpers import ROOT, ohlcv, run2_config


def _cfg(**portfolio):
    base = run2_config()
    return replace(base, portfolio=replace(base.portfolio, **portfolio))


# --- realized_vol_pct ---------------------------------------------------------- #
def test_vol_requires_a_window_and_enough_history():
    assert realized_vol_pct(pd.Series([10.0] * 40), 1) is None  # window < 2
    assert realized_vol_pct(pd.Series([10.0, 20.0, 30.0]), 5) is None  # short history
    assert realized_vol_pct(pd.Series([10.0, 20.0, 30.0, 40.0]), 2) is not None


def test_vol_matches_the_annualized_sample_standard_deviation():
    closes = pd.Series([100.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(41)])
    expected = float(closes.pct_change().dropna().tail(10).std(ddof=1)) * sqrt(
        ANNUALIZATION_DAYS
    ) * 100
    assert realized_vol_pct(closes, 10) == pytest.approx(expected)


def test_vol_treats_a_flat_history_as_zero_and_a_broken_one_as_unmeasurable():
    assert realized_vol_pct(pd.Series([10.0] * 40), 20) == 0.0
    # A zero price makes the return stream infinite: never tradeable as "calm".
    assert realized_vol_pct(pd.Series([100.0, 100.0, 0.0, 50.0]), 2) is None


# --- entry_notional ------------------------------------------------------------ #
def test_flat_notional_when_the_knob_is_absent_or_the_history_is_missing():
    base = run2_config()
    closes = pd.Series([100.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(41)])
    assert entry_notional(base, closes) == base.portfolio.position_notional
    assert entry_notional(_cfg(position_vol_target_pct=50), None) == 625.0


def test_degenerate_volatility_never_sizes_up():
    cfg = _cfg(position_vol_target_pct=50, position_vol_window=20)
    assert entry_notional(cfg, pd.Series([10.0] * 40)) == 625.0  # zero variance
    broken = pd.Series([100.0, 100.0, 0.0] + [50.0] * 38)
    assert entry_notional(cfg, broken) == 625.0  # unmeasurable


def test_scale_is_the_volatility_ratio_and_clamps_at_both_bounds():
    # Alternating +-1% closes: measured 20.1% annualized -> 2.48x, inside the band.
    cfg = _cfg(position_vol_target_pct=50, position_vol_window=10)
    calm = pd.Series([100.0] + [100.0 * (1.01 if i % 2 else 0.99) for i in range(40)])
    vol = realized_vol_pct(calm, 10)
    assert entry_notional(cfg, calm) == pytest.approx(625.0 * 50 / vol)

    # Alternating +-100% is far above the target: the floor holds.
    wild = pd.Series([100.0, 200.0, 100.0, 200.0])
    assert entry_notional(_cfg(position_vol_target_pct=50, position_vol_window=2), wild) == (
        625.0 * VOL_SCALE_MIN
    )

    # A near-smooth ramp measures below every plausible target: the ceiling holds,
    # so a trend that merely looks calm can never take an unbounded slot.
    smooth = pd.Series([100.0] * 30 + [100 * (1.005**k) for k in range(1, 21)])
    assert entry_notional(cfg, smooth) == 625.0 * VOL_SCALE_MAX


# --- the live gate prices the same notional the executor submits --------------- #
def test_candidate_notional_falls_back_to_flat_without_bars():
    cfg = _cfg(position_vol_target_pct=50, position_vol_window=2)
    assert _candidate_notional(cfg, "AAPL", None) == 625.0
    assert _candidate_notional(cfg, "AAPL", {"MSFT": ohlcv([100.0] * 40)}) == 625.0
    wild = ohlcv([100.0, 200.0, 100.0, 200.0])
    assert _candidate_notional(cfg, "AAPL", {"AAPL": wild}) == 625.0 * VOL_SCALE_MIN


# --- the replay sizes with it -------------------------------------------------- #
def _ramp(noise: float) -> pd.Series:
    """Noisy drift, then a clean 20-bar ramp: the up-cross lands near bar 33.

    ``noise`` is the pre-cross wiggle (a fraction of price), which is what the
    volatility estimate sees at the moment the entry is sized.
    """
    flat = [100.0 * (1 + noise * (1 if i % 2 else -1)) for i in range(33)]
    return pd.Series(flat + [100 * (1.005**k) for k in range(1, 21)])


def _entry_notionals(sim) -> dict[str, float]:
    buys = sim.trades[sim.trades["side"] == "buy"]
    return {row.symbol: row.qty * row.price for row in buys.itertuples()}


def test_replay_sizes_a_calm_symbol_larger_than_a_volatile_one():
    frame = {"AAPL": ohlcv(_ramp(0.0)), "MSFT": ohlcv(_ramp(0.05))}
    flat = _entry_notionals(simulate_portfolio(run2_config(), frame))
    assert set(flat) == {"AAPL", "MSFT"}
    assert all(value == pytest.approx(625.0) for value in flat.values())

    cfg = _cfg(position_vol_target_pct=50, position_vol_window=10)
    scaled = _entry_notionals(simulate_portfolio(cfg, frame))
    assert set(scaled) == {"AAPL", "MSFT"}
    assert scaled["AAPL"] > scaled["MSFT"]
    assert scaled["AAPL"] == pytest.approx(625.0 * VOL_SCALE_MAX)


# --- config surface ------------------------------------------------------------ #
def _manifest(tmp_path, run_id, portfolio_overrides):
    """Write a research manifest based on the live one, under a new run_id."""
    import yaml

    raw = run2_config().raw
    path = tmp_path / f"{run_id}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                **raw,
                "run_id": run_id,
                "portfolio": {**raw["portfolio"], **portfolio_overrides},
            }
        )
    )
    return path


def test_sizing_knobs_must_be_sane(tmp_path):
    with pytest.raises(RunConfigError, match="position_vol_target_pct"):
        load_run_config(_manifest(tmp_path, "exp-bad-target", {"position_vol_target_pct": 0}))
    with pytest.raises(RunConfigError, match="position_vol_window"):
        load_run_config(_manifest(tmp_path, "exp-bad-window", {"position_vol_window": 1}))


def test_live_runs_pin_the_sizing_knobs_absent(tmp_path):
    # A non-live id carrying the frozen values plus the knob: the strict loader
    # (every live command) must refuse it.
    path = _manifest(tmp_path, "exp-sized-lookalike", {"position_vol_target_pct": 50})
    with pytest.raises(RunConfigError, match="frozen values changed"):
        load_run_config(path)
    # ... and the research path loads the very same manifest.
    assert load_run_config(path, strict=False).portfolio.position_vol_target_pct == 50
