"""Attribute a portfolio-backtest's realized P&L to symbols and asset classes.

The frozen desk is one weak portfolio-level edge spread over 17 symbols, so the
tempting next move is always "drop the losers". This script measures whether that
would actually have made money, instead of trusting the ranking's hindsight:

1. **Attribution** — per symbol and per asset class: closing trades, total and mean
   realized P&L, mean P&L in % of notional, fees paid, and how many exits were
   stops. Fees are charged per side (see ``_cost_rate`` in
   ``src/trading/run2/portfolio.py``), so the crypto leg pays 25bps against the
   equity leg's 5bps and its gross edge has to clear a bigger hurdle.

2. **Walk-forward selection** — at each split date, rank symbols by trailing mean
   realized P&L, keep the top k, and compare against the *same* future window with
   all symbols. Two comparisons, because they disagree:
   - per-trade expectancy of the kept subset vs the universe, and
   - **total** P&L of the kept subset vs the universe and vs random same-size
     subsets. Fewer symbols buy better trades *and* fewer of them; only the total
     decides dollars. The random-k control separates selection skill from simply
     holding less.

3. **Split-half persistence** — correlation of per-symbol P&L between the first and
   second half of the sample, plus top-k overlap. A ranking that does not persist
   cannot be pruned on.

Caveat: this is a *conditional attribution*. It re-uses trades the full universe
generated, so dropping a symbol does not free cash or slots the way a re-simulated
subset would. Read it as "does symbol-level quality persist?", never as a
re-simulated portfolio; a universe change still needs its own experiment manifest
and ``money portfolio-backtest``.

Run:  .venv/bin/python scripts/analysis_symbol_edge.py \
          --artifact results/exp-scale-2x/portfolio-backtest --notional 1250
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_TOPK = (5, 8, 11)


def closed_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Closing (sell) rows with a parsed, sorted date column."""
    frame = pd.DataFrame(trades).copy()
    if frame.empty:
        return frame
    closed = frame[frame["side"] == "sell"].copy()
    closed["date"] = pd.to_datetime(closed["date"])
    return closed.sort_values("date").reset_index(drop=True)


def symbol_summary(closed: pd.DataFrame, notional: float) -> pd.DataFrame:
    """Per-symbol realized P&L, expectancy, fees and stop share."""
    if closed.empty:
        return pd.DataFrame()
    grouped = closed.groupby("symbol")
    out = pd.DataFrame(
        {
            "asset": grouped["asset"].first(),
            "trades": grouped.size(),
            "pnl": grouped["realized_pl"].sum(),
            "mean_pnl": grouped["realized_pl"].mean(),
            "fees": grouped["fee"].sum(),
        }
    )
    out["mean_pct_notional"] = out["mean_pnl"] / notional * 100
    out["stop_share_pct"] = grouped.apply(
        lambda g: 100 * (g["reason"] == "stop").mean(), include_groups=False
    )
    return out.sort_values("pnl")


def asset_summary(closed: pd.DataFrame, notional: float) -> pd.DataFrame:
    """Same decomposition collapsed to asset class (crypto vs stock)."""
    if closed.empty:
        return pd.DataFrame()
    grouped = closed.groupby("asset")
    out = pd.DataFrame(
        {
            "trades": grouped.size(),
            "pnl": grouped["realized_pl"].sum(),
            "mean_pnl": grouped["realized_pl"].mean(),
            "fees": grouped["fee"].sum(),
        }
    )
    out["mean_pct_notional"] = out["mean_pnl"] / notional * 100
    out["trade_share_pct"] = out["trades"] / len(closed) * 100
    out["pnl_share_pct"] = out["pnl"] / closed["realized_pl"].sum() * 100
    out["fee_share_pct"] = out["fees"] / closed["fee"].sum() * 100
    return out.sort_values("pnl")


def walk_forward_selection(
    closed: pd.DataFrame, k: int, seed: int = 11, warmup: int = 40, tail: int = 20
) -> dict[str, float]:
    """Rank-by-trailing-P&L selection vs the universe, with a random-k control.

    Splits are every trade date except the first ``warmup`` and last ``tail``.
    Adjacent splits share most of their windows, so the split counts are not
    independent observations — treat the percentages as descriptive.
    """
    if closed.empty or closed["symbol"].nunique() < k:
        return {}
    dates = closed["date"].unique()
    rng = np.random.default_rng(seed)
    per_trade_edge: list[float] = []
    total_edge: list[float] = []
    subset_total: list[float] = []
    universe_total: list[float] = []
    random_total: list[float] = []
    subset_trades: list[int] = []
    universe_trades: list[int] = []
    for split in dates[warmup:-tail]:
        past = closed[closed["date"] <= split]
        future = closed[closed["date"] > split]
        if len(future) < 20 or past["symbol"].nunique() < k:
            continue
        ranked = past.groupby("symbol")["realized_pl"].mean().sort_values(ascending=False)
        kept = ranked.head(k).index
        subset = future[future["symbol"].isin(kept)]
        if subset.empty:
            continue
        per_trade_edge.append(float(subset["realized_pl"].mean() - future["realized_pl"].mean()))
        total_edge.append(float(subset["realized_pl"].sum() - future["realized_pl"].sum()))
        subset_total.append(float(subset["realized_pl"].sum()))
        universe_total.append(float(future["realized_pl"].sum()))
        subset_trades.append(len(subset))
        universe_trades.append(len(future))
        picks = rng.choice(future["symbol"].unique(), k, replace=False)
        random_total.append(float(future[future["symbol"].isin(picks)]["realized_pl"].sum()))
    if not per_trade_edge:
        return {}
    return {
        "k": float(k),
        "splits": float(len(per_trade_edge)),
        "per_trade_edge": float(np.mean(per_trade_edge)),
        "per_trade_win_pct": float(100 * np.mean([e > 0 for e in per_trade_edge])),
        "total_edge": float(np.mean(total_edge)),
        "total_win_pct": float(100 * np.mean([e > 0 for e in total_edge])),
        "subset_total": float(np.mean(subset_total)),
        "universe_total": float(np.mean(universe_total)),
        "random_total": float(np.mean(random_total)),
        "selection_premium_pct": float(
            100 * (np.mean(subset_total) / np.mean(random_total) - 1)
        )
        if np.mean(random_total)
        else 0.0,
        "subset_trades": float(np.mean(subset_trades)),
        "universe_trades": float(np.mean(universe_trades)),
    }


def split_half_persistence(closed: pd.DataFrame, topk: int = 5) -> dict[str, float]:
    """Does the per-symbol P&L ranking survive to the second half?"""
    if closed.empty:
        return {}
    midpoint = closed["date"].quantile(0.5)
    first = closed[closed["date"] <= midpoint]
    second = closed[closed["date"] > midpoint]
    if first.empty or second.empty:
        return {}
    f = first.groupby("symbol")["realized_pl"].sum()
    s = second.groupby("symbol")["realized_pl"].sum()
    joined = pd.concat([f.rename("first"), s.rename("second")], axis=1).dropna()
    if len(joined) < 3:
        return {}
    overlap = len(
        set(f.sort_values(ascending=False).head(topk).index)
        & set(s.sort_values(ascending=False).head(topk).index)
    )
    return {
        "symbols": float(len(joined)),
        "pearson": float(joined["first"].corr(joined["second"])),
        "spearman": float(joined["first"].rank().corr(joined["second"].rank())),
        "topk_overlap": float(overlap),
        "topk": float(topk),
    }


def load_trades(artifact: Path) -> pd.DataFrame:
    return pd.read_csv(Path(artifact) / "trades.csv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Attribute portfolio-backtest P&L to symbols")
    parser.add_argument(
        "--artifact",
        default="results/exp-scale-2x/portfolio-backtest",
        help="directory holding trades.csv from a portfolio-backtest run",
    )
    parser.add_argument("--notional", type=float, default=1250.0, help="per-position notional")
    parser.add_argument("--topk", type=int, nargs="+", default=list(DEFAULT_TOPK))
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args(argv)

    closed = closed_trades(load_trades(Path(args.artifact)))
    if closed.empty:
        print(f"no closing trades in {args.artifact}")
        return 1
    print(f"-- {args.artifact} --")
    print(
        f"closing trades: {len(closed)}  window: {closed['date'].min().date()}"
        f" -> {closed['date'].max().date()}  notional ${args.notional:,.0f}"
    )

    print("\n-- per symbol (worst first) --")
    print(symbol_summary(closed, args.notional).to_string(float_format=lambda v: f"{v:,.2f}"))

    print("\n-- per asset class --")
    print(asset_summary(closed, args.notional).to_string(float_format=lambda v: f"{v:,.2f}"))

    print("\n-- walk-forward top-k selection (all splits, same future window) --")
    print(
        "  k  splits  per-trade edge%  beats  total edge$  beats   subset$  universe$"
        "  random$  vs random   trades k/all"
    )
    for k in args.topk:
        row = walk_forward_selection(closed, k, seed=args.seed)
        if not row:
            print(f"{k:>3}  (not enough symbols/history)")
            continue
        print(
            f"{k:>3}  {row['splits']:>6.0f}  {row['per_trade_edge']:>15,.2f}"
            f"  {row['per_trade_win_pct']:>5.1f}%  {row['total_edge']:>12,.0f}"
            f"  {row['total_win_pct']:>5.1f}%  {row['subset_total']:>9,.0f}"
            f"  {row['universe_total']:>9,.0f}  {row['random_total']:>7,.0f}"
            f"  {row['selection_premium_pct']:>+8.1f}%  {row['subset_trades']:.0f}/{row['universe_trades']:.0f}"
        )
    print(
        "  reading: the top-k subset buys better trades and fewer of them."
        " Pruning pays only if the per-trade gain beats the lost trade count,"
        " i.e. only if subset$ > universe$."
    )

    print("\n-- split-half persistence --")
    persistence = split_half_persistence(closed)
    if persistence:
        print(
            f"  symbols={persistence['symbols']:.0f}  pearson={persistence['pearson']:+.2f}"
            f"  spearman={persistence['spearman']:+.2f}"
            f"  top{persistence['topk']:.0f} overlap={persistence['topk_overlap']:.0f}"
            f"/{persistence['topk']:.0f}"
        )
    else:
        print("  (not enough symbols)")
    print("\nConditional attribution on trades the full universe generated — not a re-simulated subset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
