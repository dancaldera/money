"""The trades a rule change actually added or removed, against the control's.

The sweep table (`analysis_experiment_table.py`) prices a whole path: return,
drawdown, expectancy, DSR. It answers *whether* a variant won, not *how*. When
the variant differs from the control by exactly one guard or filter — the
correlation cap, the whipsaw gate, a maker entry — the question that decides
adoption is sharper than the path: **are the trades the rule blocks better or
worse than the desk's average trade?** A rule that suppresses below-average
trades is a candidate even when it costs a little return — but the added average
is not the verdict: a rule can block below-average trades and still lose dollars
if it blocks more than it adds. The script prints both, and the **net effect**
(added P&L − dropped P&L − extra fees) is the number adoption turns on.

The two replays write the same ``trades.csv`` schema (one row per fill; the
``realized_pl`` of a round trip is booked on its ``sell`` row, ``buy`` rows carry
0). This script matches a control artifact against one or more variant artifacts
on ``(date, symbol, side)`` and reports:

* entries the variant adds / drops, split by asset;
* the added and dropped **round trips** with their realized P&L, mean and asset
  split, plus the added mean as a multiple of the control's expectancy per trade;
* the fee bill the extra round trips bring with them.

It is a first-order lens, deliberately: matching on the fill key attributes the
exit that the variant actually booked, so a displaced position (same symbol, a
different entry date) shows up as one added and one removed row instead of being
netted away. Read it next to the sweep table and the ``blocked_by`` census in
``decisions.csv``, which name the guard that freed the slot.

Run:
  .venv/bin/python scripts/analysis_marginal_trades.py \\
      --control exp-slots-all-recover exp-corr-off exp-corr-match2
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "results"

REQUIRED_COLUMNS = ("date", "symbol", "asset", "side", "fee", "realized_pl")


def artifact_dir(results_dir: Path, run_id: str) -> Path:
    return Path(results_dir) / run_id / "portfolio-backtest"


def load_trades(results_dir: Path, run_id: str) -> pd.DataFrame:
    """A replay's ``trades.csv``; a missing artifact is an error, not an empty set."""
    path = artifact_dir(results_dir, run_id) / "trades.csv"
    if not path.exists():
        raise FileNotFoundError(f"no replay artifact for {run_id}: {path}")
    trades = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in trades.columns]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}")
    trades["date"] = trades["date"].astype(str)
    return trades


def _fill_keys(trades: pd.DataFrame) -> set[tuple[str, str, str]]:
    return set(map(tuple, trades[["date", "symbol", "side"]].itertuples(index=False)))


def _added_or_removed(base: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    """Rows in ``other`` whose fill key is absent from ``base``."""
    return other[~other[["date", "symbol", "side"]].apply(tuple, axis=1).isin(_fill_keys(base))]


def _asset_counts(frame: pd.DataFrame) -> dict[str, int]:
    return {str(k): int(v) for k, v in frame.groupby("asset").size().items()}


def _by_asset_pnl(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for asset, group in frame.groupby("asset"):
        out[str(asset)] = {"n": int(len(group)), "pnl": float(group["realized_pl"].sum())}
    return out


def marginal(control: pd.DataFrame, variant: pd.DataFrame) -> dict:
    """The fills and round trips the variant has that the control does not (and back)."""
    added = _added_or_removed(control, variant)
    removed = _added_or_removed(variant, control)
    added_entries, removed_entries = added[added.side == "buy"], removed[removed.side == "buy"]
    added_trips, removed_trips = added[added.side == "sell"], removed[removed.side == "sell"]
    control_trips = control[control.side == "sell"]

    added_pnl = float(added_trips["realized_pl"].sum()) if len(added_trips) else 0.0
    removed_pnl = float(removed_trips["realized_pl"].sum()) if len(removed_trips) else 0.0
    extra_fees = float(variant["fee"].sum() - control["fee"].sum())
    control_expectancy = float(control_trips["realized_pl"].mean()) if len(control_trips) else 0.0
    return {
        "added_entries": int(len(added_entries)),
        "removed_entries": int(len(removed_entries)),
        "added_entries_by_asset": _asset_counts(added_entries),
        "removed_entries_by_asset": _asset_counts(removed_entries),
        "added_round_trips": int(len(added_trips)),
        "added_pnl": added_pnl,
        "added_mean": added_pnl / len(added_trips) if len(added_trips) else 0.0,
        "added_by_asset": _by_asset_pnl(added_trips),
        "removed_round_trips": int(len(removed_trips)),
        "removed_pnl": removed_pnl,
        "extra_fees": extra_fees,
        # The dollar verdict of the change: what the new trips earn, minus what
        # the dropped trips earned, minus the fees it takes to do it. A rule can
        # block below-average trades and still lose money (it blocks more than it
        # adds), so this — not the added mean — is what adoption turns on.
        "net_pnl": added_pnl - removed_pnl - extra_fees,
        "completed_delta": int(len(variant[variant.side == "sell"]) - len(control_trips)),
        "control_expectancy": control_expectancy,
        "expectancy_multiple": (added_pnl / len(added_trips) / control_expectancy)
        if len(added_trips) and control_expectancy
        else float("nan"),
    }


def _per_asset_line(by_asset: dict[str, dict[str, float]]) -> str:
    if not by_asset:
        return "-"
    return ", ".join(
        f"{asset} ${v['pnl']:+,.2f}/{v['n']}" for asset, v in sorted(by_asset.items())
    )


def format_report(control_id: str, control: pd.DataFrame, variant_id: str, stats: dict) -> str:
    control_trips = int(len(control[control.side == "sell"]))
    lines = [
        f"control `{control_id}`: {control_trips} completed trades, "
        f"expectancy ${stats['control_expectancy']:+,.2f}/trade",
        f"variant `{variant_id}`: completed trades {control_trips} -> "
        f"{control_trips + stats['completed_delta']} ({stats['completed_delta']:+d})",
        f"  entries          +{stats['added_entries']} "
        f"({stats['added_entries_by_asset']}) "
        f"-{stats['removed_entries']} ({stats['removed_entries_by_asset']})",
        f"  round trips      +{stats['added_round_trips']} realized ${stats['added_pnl']:+,.2f} "
        f"(mean ${stats['added_mean']:+,.2f} = "
        f"{stats['expectancy_multiple']:.2f}x the control's expectancy)",
        f"                   {_per_asset_line(stats['added_by_asset'])}",
        f"  dropped trips    -{stats['removed_round_trips']} "
        f"realized ${stats['removed_pnl']:+,.2f}",
        f"  extra fees       ${stats['extra_fees']:+,.2f}",
        f"  net effect       ${stats['net_pnl']:+,.2f} (added - dropped - fees) — "
        f"the change {'pays for itself' if stats['net_pnl'] > 0 else 'costs dollars'}.",
    ]
    if stats["expectancy_multiple"] == stats["expectancy_multiple"]:
        rel = "below" if stats["expectancy_multiple"] < 1 else "above"
        lines.append(
            f"  read: the added trips earn {rel}-average dollars "
            f"({stats['expectancy_multiple']:.2f}x the control's expectancy). "
            "Weigh the net effect above against the drawdown the change costs — "
            "neither number alone decides adoption."
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="the fills/round trips a rule change added, against a control replay"
    )
    parser.add_argument("run_ids", nargs="+", help="variant experiment run_ids")
    parser.add_argument("--control", required=True, help="experiment run_id of the control")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results_dir = Path(args.results_dir)
    control = load_trades(results_dir, args.control)
    for run_id in args.run_ids:
        variant = load_trades(results_dir, run_id)
        print(format_report(args.control, control, run_id, marginal(control, variant)))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
