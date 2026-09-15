"""Tests for the symbol/asset P&L attribution analysis.

``scripts/analysis_symbol_edge.py`` backs two decisions that would otherwise be
taken on hindsight ranking alone: (a) whether pruning symbols by trailing P&L
makes money, and (b) whether the per-symbol ranking persists at all. The
primitives are pinned here on synthetic closed-trade frames; the script itself
reads the gitignored ``results/`` artifacts.

Fixture shape: 120 consecutive days, one closing trade per symbol per day, so
``walk_forward_selection`` (warmup=40, tail=20) has 60 usable splits with plenty
of future trades and ``split_half_persistence`` has 60 trades per half.
"""

from __future__ import annotations

import importlib.util

import pandas as pd
import pytest

from .run2_helpers import ROOT

_SPEC = importlib.util.spec_from_file_location(
    "analysis_symbol_edge", ROOT / "scripts" / "analysis_symbol_edge.py"
)
assert _SPEC and _SPEC.loader
edge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(edge)

DAYS = 120


def _trades(per_symbol: dict[str, list[float]], notional: float = 1250.0) -> pd.DataFrame:
    """One closing trade per symbol per day from the given per-day P&L lists."""
    dates = pd.date_range("2024-01-01", periods=DAYS, freq="D")
    rows = []
    for symbol, pnls in per_symbol.items():
        for day, pnl in zip(dates, pnls, strict=True):
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "asset": "crypto" if symbol.endswith("/USD") else "stock",
                    "side": "sell",
                    "qty": 1.0,
                    "price": 100.0,
                    "fee": notional * 0.0005,
                    "realized_pl": pnl,
                    "reason": "stop" if pnl < 0 else float("nan"),
                }
            )
    return pd.DataFrame(rows)


PERSISTENT = {
    "WIN": [100.0] * DAYS,
    "LOSE1": [-50.0] * DAYS,
    "LOSE2": [-50.0] * DAYS,
    "LOSE3": [-50.0] * DAYS,
}


def test_closed_trades_keeps_only_sells_sorted_by_date():
    raw = _trades(PERSISTENT).head(8).copy()
    raw.loc[raw.index[0], "side"] = "buy"
    raw = raw.sample(frac=1.0, random_state=3)  # scrambled on purpose
    closed = edge.closed_trades(raw)
    assert (closed["side"] == "sell").all()
    assert len(closed) == 7
    assert closed["date"].is_monotonic_increasing
    assert pd.api.types.is_datetime64_any_dtype(closed["date"])


def test_closed_trades_tolerates_an_empty_frame():
    assert edge.closed_trades(pd.DataFrame(columns=["side", "date"])).empty
    assert edge.symbol_summary(pd.DataFrame(), 1250.0).empty
    assert edge.walk_forward_selection(pd.DataFrame(), 5) == {}
    assert edge.split_half_persistence(pd.DataFrame()) == {}


def test_symbol_summary_reports_expectancy_fees_and_stop_share():
    out = edge.symbol_summary(edge.closed_trades(_trades(PERSISTENT)), 1250.0)
    assert out.loc["WIN", "trades"] == DAYS
    assert out.loc["WIN", "pnl"] == pytest.approx(100.0 * DAYS)
    # expectancy as a share of the position notional, not of the equity
    assert out.loc["WIN", "mean_pct_notional"] == pytest.approx(100 / 1250 * 100)
    assert out.loc["WIN", "fees"] == pytest.approx(1250 * 0.0005 * DAYS)
    assert out.loc["WIN", "stop_share_pct"] == pytest.approx(0.0)
    assert out.loc["LOSE1", "stop_share_pct"] == pytest.approx(100.0)
    # worst performer first: the table is meant to be read as a ranking
    assert out.index[0] == "LOSE1"


def test_asset_summary_shares_add_up():
    out = edge.asset_summary(edge.closed_trades(_trades(PERSISTENT)), 1250.0)
    assert out["trade_share_pct"].sum() == pytest.approx(100.0)
    assert out["fee_share_pct"].sum() == pytest.approx(100.0)
    # WIN is the only stock in this fixture, so stocks hold all the P&L
    assert out.loc["stock", "pnl_share_pct"] == pytest.approx(100.0)


def test_walk_forward_selection_prefers_persistent_winners_over_random():
    out = edge.walk_forward_selection(edge.closed_trades(_trades(PERSISTENT)), k=1, seed=11)
    assert out["splits"] == 60
    # the trailing winners keep winning: better per trade AND more dollars than
    # either the whole universe or a random symbol of the same size
    assert out["per_trade_edge"] > 0
    assert out["per_trade_win_pct"] == 100.0
    assert out["subset_total"] > out["universe_total"]
    assert out["subset_total"] > out["random_total"] or out["selection_premium_pct"] > 0
    # a k=1 subset takes one trade a day against the universe's four
    assert out["universe_trades"] == pytest.approx(4 * out["subset_trades"], rel=1e-6)


def test_walk_forward_selection_fewer_trades_than_k_returns_nothing():
    assert edge.walk_forward_selection(edge.closed_trades(_trades(PERSISTENT)), k=9) == {}


def test_walk_forward_selection_total_can_lose_while_per_trade_wins():
    """The trap the script exists to expose: pruning raises quality, cuts dollars.

    Five symbols: BIG is the best per trade ($100/day) but the four weaker symbols
    each still make $30/day, so the universe's total dwarfs the kept subset even
    while the subset's expectancy per trade is far higher.
    """
    many = {
        "BIG": [100.0] * DAYS,
        "S1": [30.0] * DAYS,
        "S2": [30.0] * DAYS,
        "S3": [30.0] * DAYS,
        "S4": [30.0] * DAYS,
    }
    out = edge.walk_forward_selection(edge.closed_trades(_trades(many)), k=1, seed=11)
    # subset expectancy $100 vs the universe's $44 per trade
    assert out["per_trade_edge"] == pytest.approx(56.0)
    assert out["per_trade_win_pct"] == 100.0
    # ... yet the universe earns $220/day against the subset's $100/day
    assert out["subset_total"] < out["universe_total"]
    assert out["total_edge"] < 0


def test_split_half_persistence_detects_a_stable_ranking():
    stable = {"AAA": [100.0] * DAYS, "BBB": [50.0] * DAYS, "CCC": [-50.0] * DAYS}
    out = edge.split_half_persistence(edge.closed_trades(_trades(stable)), topk=1)
    assert out["pearson"] == pytest.approx(1.0)
    assert out["spearman"] == pytest.approx(1.0)
    assert out["topk_overlap"] == 1  # AAA leads in both halves
    # with k wider than the universe the overlap is capped at the symbol count
    assert edge.split_half_persistence(edge.closed_trades(_trades(stable)))["topk_overlap"] == 3


def test_split_half_persistence_flags_a_ranking_that_flips():
    flipped = {
        "AAA": [100.0] * (DAYS // 2) + [-100.0] * (DAYS // 2),
        "BBB": [-100.0] * (DAYS // 2) + [100.0] * (DAYS // 2),
        "CCC": [10.0] * DAYS,
    }
    out = edge.split_half_persistence(edge.closed_trades(_trades(flipped)), topk=1)
    assert out["pearson"] < 0
    # the best symbol in the first half is the worst in the second: no overlap
    assert out["topk_overlap"] == 0
