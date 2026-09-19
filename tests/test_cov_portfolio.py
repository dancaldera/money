"""Coverage for trading.run2.portfolio simulator gaps and statistics."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from trading.run2.portfolio import (
    SimPosition,
    _correlation_matches_on_day,
    block_bootstrap_mean_ci,
    deflated_sharpe_probability,
    primary_benchmark,
    simulate_portfolio,
)

from .run2_helpers import ohlcv, run2_config


# --- primary_benchmark branches ------------------------------------------------ #
def _full_frames(n=90):
    cfg = run2_config()
    return {symbol: ohlcv(np.linspace(100, 120, n)) for symbol, _ in cfg.symbols}


def test_benchmark_localizes_naive_start_to_aware_panel():
    frames = {s: f.tz_localize("UTC") for s, f in _full_frames().items()}
    out = primary_benchmark(run2_config(), frames, start="2026-01-05")
    assert not out.empty


def test_benchmark_empty_when_start_beyond_data():
    out = primary_benchmark(run2_config(), _full_frames(), start="2030-01-01")
    assert out.empty


def test_benchmark_empty_without_both_legs():
    cfg = run2_config()
    frames = {s: ohlcv(np.linspace(100, 120, 90)) for s in cfg.crypto_symbols}
    assert primary_benchmark(cfg, frames).empty


def test_benchmark_applies_cash_yield_curve():
    frames = _full_frames()
    dates = next(iter(frames.values())).index
    cash_yield = pd.Series(4.0, index=dates)  # 4% annual on the cash leg
    out = primary_benchmark(run2_config(), frames, cash_yield=cash_yield)
    assert not out.empty and out.iloc[-1] > run2_config().starting_equity


# --- _correlation_matches_on_day ------------------------------------------------ #
def test_day_correlation_edge_cases():
    frame = ohlcv(np.linspace(100, 200, 100))
    day = pd.Timestamp(frame.index[-1])
    assert _correlation_matches_on_day("X", {"A": 1}, {"A": frame}, day, 60, 0.8) == 0
    held = {"MISSING": SimPosition("MISSING", "stock", 1, 100, 90, 0)}
    assert _correlation_matches_on_day("AAPL", held, {"AAPL": frame}, day, 60, 0.8) == 0
    short = ohlcv([100.0, 101.0, 102.0])
    day0 = pd.Timestamp(short.index[-1])
    held2 = {"B": SimPosition("B", "stock", 1, 100, 90, 0)}
    assert _correlation_matches_on_day("A", held2, {"A": short, "B": short}, day0, 60, 0.8) == 0
    indexed = {"A": frame, "B": frame.copy()}
    assert _correlation_matches_on_day("A", held2, indexed, day, 60, 0.8) == 1


# --- simulate_portfolio: natural round trip ------------------------------------ #
def test_simulator_executes_sell_intent_for_profit():
    cfg = run2_config()
    close = [10.0] * 30 + [9.0, 20.0] + [30.0] * 40 + [28.0, 28.0]
    opens = list(close)
    opens[32] = 20.0  # execution-day open matches the 20 signal close (no gap)
    sim = simulate_portfolio(cfg, {"AAPL": ohlcv(close, opens=opens)})
    assert "sell_intent" in set(sim.decisions["action"])
    sells = sim.trades[sim.trades["side"] == "sell"]
    assert len(sells) == 1 and sells.iloc[0]["realized_pl"] > 0
    assert sim.metrics["completed_trades"] == 1.0
    assert sim.metrics["profit_factor"] == 0.0  # no losing trades


def test_simulator_skips_days_missing_from_a_symbol_calendar():
    cfg = run2_config()
    # Start on a Thursday so the weekday-only buy signal (bar 31) lands on a
    # Friday: the pending intent then spans a weekend the symbol has no bars for.
    all_days = pd.date_range("2026-01-08", periods=70, freq="D")
    btc = ohlcv(np.full(70, 100.0), start="2026-01-08")
    weekdays = [d for d in all_days if d.weekday() < 5]
    assert weekdays[31].weekday() == 4  # signal bar is a Friday
    n = len(weekdays)
    aapl_close = [10.0] * 30 + [9.0, 20.0] + [20.0] * (n - 32)
    aapl = pd.DataFrame(
        {"Open": aapl_close, "High": np.array(aapl_close) * 1.01,
         "Low": np.array(aapl_close) * 0.99, "Close": aapl_close,
         "Volume": np.ones(n)},
        index=pd.DatetimeIndex(weekdays),
    )
    sim = simulate_portfolio(cfg, {"AAPL": aapl, "BTC/USD": btc})
    assert not sim.trades.empty  # buy executed despite weekend gaps


def test_simulator_tolerates_duplicate_bars_on_execution_days():
    cfg = run2_config()
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0, 20.0])
    duped = pd.concat([frame, frame.iloc[[2 - 0 + 30], :], frame.iloc[[33], :]]).sort_index()
    sim = simulate_portfolio(cfg, {"AAPL": duped})
    assert not sim.trades.empty


def test_simulator_skips_entry_when_cash_cannot_cover_costs():
    cfg = replace(run2_config(), starting_equity=100.0)
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0])
    sim = simulate_portfolio(cfg, {"AAPL": frame})
    assert sim.trades.empty  # $100 cannot cover $625 + fee


def test_simulator_halts_new_entries_after_drawdown():
    cfg = replace(run2_config(), starting_equity=1000.0)
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0, 20.0])
    frame.loc[frame.index[-1], "Low"] = 10.0  # stop triggers on the final bar
    sim = simulate_portfolio(cfg, {"AAPL": frame})
    assert sim.trades[sim.trades["side"] == "sell"].iloc[0]["reason"] == "stop"
    assert sim.metrics["completed_trades"] == 1.0


# --- experiment stop refinements (trailing / breakeven) -------------------------- #
def test_simulator_trailing_stop_catches_a_reversal_above_entry():
    base = run2_config()
    cfg = replace(base, strategy=replace(base.strategy, stop_trail_pct=6))
    close = [10.0] * 30 + [9.0, 20.0] + [30.0] * 10 + [28.0] * 5
    opens = list(close)
    opens[32] = 20.0  # execution-day open matches the 20 signal close
    opens[42] = 28.5  # first reversal bar opens above the trailed stop
    frame = ohlcv(close, opens=opens)
    duped = pd.concat([frame, frame.iloc[[33], :]]).sort_index()  # DataFrame-row path
    sim = simulate_portfolio(cfg, {"AAPL": duped})
    sells = sim.trades[sim.trades["side"] == "sell"]
    assert len(sells) == 1 and sells.iloc[0]["reason"] == "stop"
    assert sells.iloc[0]["price"] == pytest.approx(28.2)  # 30 peak close - 6%
    control = simulate_portfolio(base, {"AAPL": frame})
    assert "stop" not in set(control.trades.get("reason") or [])


def test_simulator_breakeven_stop_protects_a_winner():
    base = run2_config()
    cfg = replace(base, strategy=replace(base.strategy, stop_breakeven_at_pct=10))
    close = [10.0] * 30 + [9.0, 20.0] + [22.5] * 5 + [20.1] * 5
    opens = list(close)
    opens[32] = 20.0
    sim = simulate_portfolio(cfg, {"AAPL": ohlcv(close, opens=opens)})
    sells = sim.trades[sim.trades["side"] == "sell"]
    assert len(sells) == 1 and sells.iloc[0]["reason"] == "stop"
    assert sells.iloc[0]["price"] == pytest.approx(20.0)  # stop parked at entry
    control = simulate_portfolio(base, {"AAPL": ohlcv(close, opens=opens)})
    assert "stop" not in set(control.trades.get("reason") or [])


# --- blocked_by attribution ------------------------------------------------------ #
def _paired_frames(asset="stock"):
    """Two symbols of one asset class that cross up on the same bar."""
    cfg = run2_config()
    frame = ohlcv([10.0] * 30 + [9.0, 20.0, 20.0])
    symbols = cfg.stock_symbols[:2] if asset == "stock" else cfg.crypto_symbols[:2]
    return {symbol: frame.copy() for symbol in symbols}


@pytest.mark.parametrize(
    "asset,overrides,expected",
    [
        ("stock", {"max_positions": 1}, "max_positions"),
        ("stock", {"max_gross_exposure": 625}, "max_gross_exposure"),
        ("stock", {"max_stock_positions": 1}, "max_stock_positions"),
        ("stock", {"max_stock_exposure": 625}, "max_stock_exposure"),
        ("crypto", {"max_crypto_positions": 1}, "max_crypto_positions"),
        ("crypto", {"max_crypto_exposure": 625}, "max_crypto_exposure"),
    ],
)
def test_replay_names_the_guard_that_suppressed_a_buy(asset, overrides, expected):
    """A suppressed buy must carry the guard that stopped it, not a bare bool.

    Without the name, a replay says 45% of fresh crosses never traded but not
    which cap did it — and only the counters can price relaxing one of them.
    """
    base = run2_config()
    cfg = replace(base, portfolio=replace(base.portfolio, **overrides))
    sim = simulate_portfolio(cfg, _paired_frames(asset))
    blocked = sim.decisions.loc[sim.decisions["blocked_by"].notna()]
    assert set(blocked["blocked_by"]) == {expected}
    assert set(blocked["signal"]) == {"BUY"}


def _stepped_frame(step_bar, step=2.0, amp=0.5, ramp=5, periods=80, base=100.0):
    """Flat bar series that steps up once, so its only fresh cross is at ``step_bar``."""
    index = np.arange(periods)
    advanced = np.clip((index - step_bar + 1) / ramp, 0.0, 1.0)
    close = base + step * advanced + amp * np.where(index % 2 == 0, 1.0, -1.0)
    return ohlcv(close)


def test_replay_names_the_correlation_cap_and_its_match_count():
    # sma_cross_signal needs slow + 2 bars before it can report a cross, so the
    # earliest detectable cross is at bar 32 — the held pair steps at 34, the
    # blocked candidate at 45.
    frames = {
        "AAPL": _stepped_frame(34),
        "MSFT": _stepped_frame(34),  # both held, ~0.99 correlated with NFLX
        "NFLX": _stepped_frame(45),
    }
    sim = simulate_portfolio(run2_config(), frames)
    blocked = sim.decisions.loc[sim.decisions["blocked_by"].notna()]
    assert list(blocked["symbol"]) == ["NFLX"]
    assert list(blocked["blocked_by"]) == ["correlation_cap:2"]  # 2 held matches, 1 allowed


def test_replay_names_the_entry_gate():
    sim = simulate_portfolio(
        run2_config(), {"AAPL": ohlcv([10.0] * 30 + [9.0, 20.0, 20.0])},
        entry_gate=lambda *args: False,
    )
    assert set(sim.decisions["blocked_by"].dropna()) == {"entry_gate"}
    assert sim.trades.empty


# --- statistics edge cases ------------------------------------------------------ #
def test_bootstrap_ci_needs_two_observations():
    low, high = block_bootstrap_mean_ci(pd.Series([1.0]))
    assert np.isnan(low) and np.isnan(high)


def test_deflated_sharpe_guards_and_single_trial():
    assert deflated_sharpe_probability(pd.Series([0.01, 0.02])) == 0.0
    assert deflated_sharpe_probability(pd.Series([0.0] * 10)) == 0.0
    values = pd.Series([0.01, -0.005, 0.02, 0.003, -0.01, 0.015])
    assert 0 <= deflated_sharpe_probability(values, trials=1) <= 1
