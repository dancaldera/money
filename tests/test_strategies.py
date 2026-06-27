"""End-to-end sanity tests that run on synthetic data (no network)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.backtest import run_backtest
from trading.reporting import summarize
from trading.strategies import STRATEGIES, get_strategy


def _synthetic_ohlcv(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """A noisy upward-trending random walk with realistic OHLC relationships."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.5, 5.0, size=n).cumsum()
    close = 100 + steps - steps.min() + 10  # keep strictly positive
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    high = close + rng.uniform(0, 3, size=n)
    low = close - rng.uniform(0, 3, size=n)
    open_ = close + rng.uniform(-2, 2, size=n)
    vol = rng.uniform(1_000, 5_000, size=n)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


@pytest.fixture(scope="module")
def data() -> pd.DataFrame:
    return _synthetic_ohlcv()


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_strategy_runs_and_reports(data, name):
    strategy = get_strategy(name)
    stats = run_backtest(data, strategy, cash=10_000, commission=0.002)
    summary = summarize(stats)

    # The backtest produced a usable W&L summary.
    assert summary["trades"] >= 0
    assert 0.0 <= summary["win_rate_pct"] <= 100.0
    assert np.isfinite(summary["return_pct"])
    assert np.isfinite(summary["final_equity"])


def test_cash_autoscales_for_high_priced_asset():
    """A high-priced asset with small cash should still execute trades."""
    df = _synthetic_ohlcv()
    df = df * 1000  # push price into the tens-of-thousands range (BTC-like)
    stats = run_backtest(df, get_strategy("sma_cross"), cash=1_000)
    assert summarize(stats)["trades"] > 0


def test_window_alpha_reports_alpha_and_guards_short_data(data):
    from trading.cli import _window_alpha

    strat = get_strategy("sma_cross")
    res = _window_alpha(data, strat, cash=10_000, commission=0.002)
    assert res is not None
    # alpha is exactly return minus buy & hold.
    assert res["alpha"] == round(res["return_pct"] - res["buy_hold_pct"], 2)
    # Too few bars -> no score (None) rather than a misleading number.
    assert _window_alpha(data.iloc[:30], strat, cash=10_000, commission=0.002) is None
