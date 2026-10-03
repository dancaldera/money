"""One comparison table for a sweep of experiment replays, on a single clock.

Every row of a sweep like the SMA-window family (``exp-sma-*``) is a full
``money portfolio-backtest`` run whose artifacts land in
``results/<run_id>/portfolio-backtest/``. Reading those tables in the docs means
copying numbers out of five ``summary.json`` files by hand, which is exactly how
rows measured on *different* cache dates end up side by side (the docs carry
both a through-2026-09-18 and a through-2026-09-29 ladder for that reason).

This script prints them as one table and, more importantly, **refuses to imply a
comparison it cannot support**: it labels the clock (first and last date of each
replay's ``equity.csv``) and flags when the rows disagree, so a sweep that was
replayed across a cache refresh is visible instead of silent.

Columns are read-only and come from files the CLI already wrote:

- return / maxDD / Sharpe / trades / win% / expectancy / PF / DSR: ``summary.json``
- total: ``completed_trades * expectancy`` (the realized P&L the table implies)
- ret/DD: return per unit of drawdown — the lens the sizing ladder uses
- fast/slow: the manifest's ``strategy`` windows, so a row is self-describing

No broker call, no network, no writes: safe to run in the daily measure step.

Run:  .venv/bin/python scripts/analysis_experiment_table.py \\
          exp-slots-all-recover exp-sma-3-15 exp-sma-5-20 exp-sma-8-24
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "results"
DEFAULT_CONFIGS = ROOT / "config" / "experiments"


def artifact_dir(results_dir: Path, run_id: str) -> Path:
    return Path(results_dir) / run_id / "portfolio-backtest"


def load_summary(results_dir: Path, run_id: str) -> dict:
    """The replay's ``summary.json``; missing artifacts are an error, not a zero."""
    path = artifact_dir(results_dir, run_id) / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"no replay artifact for {run_id}: {path}")
    return json.loads(path.read_text())


def replay_clock(results_dir: Path, run_id: str) -> tuple[str, str]:
    """First and last date of the replayed equity curve (the cache it saw)."""
    path = artifact_dir(results_dir, run_id) / "equity.csv"
    if not path.exists():
        raise FileNotFoundError(f"no equity curve for {run_id}: {path}")
    dates = pd.read_csv(path)["date"]
    return str(dates.iloc[0]), str(dates.iloc[-1])


def strategy_windows(configs_dir: Path, run_id: str) -> tuple[int, int] | None:
    """The manifest's ``strategy`` windows, when the manifest still exists."""
    path = Path(configs_dir) / f"{run_id}.yaml"
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text()) or {}
    strategy = raw.get("strategy") or {}
    fast, slow = strategy.get("fast_window"), strategy.get("slow_window")
    return (int(fast), int(slow)) if fast and slow else None


def row(results_dir: Path, configs_dir: Path, run_id: str) -> dict:
    summary = load_summary(results_dir, run_id)
    first, last = replay_clock(results_dir, run_id)
    windows = strategy_windows(configs_dir, run_id)
    returned = float(summary["return_pct"])
    drawdown = float(summary["max_drawdown_pct"])
    trades = float(summary.get("completed_trades") or 0.0)
    expectancy = float(summary.get("expectancy") or 0.0)
    return {
        "run_id": run_id,
        "windows": f"{windows[0]}/{windows[1]}" if windows else "-",
        "return_pct": returned,
        "max_drawdown_pct": drawdown,
        "return_per_dd": returned / abs(drawdown) if drawdown else float("nan"),
        "trades": int(trades),
        "win_rate_pct": float(summary.get("win_rate_pct") or 0.0),
        "expectancy": expectancy,
        "total": trades * expectancy,
        "sharpe": float(summary.get("sharpe") or 0.0),
        "profit_factor": float(summary.get("profit_factor") or 0.0),
        "dsr": float(summary.get("deflated_sharpe_probability") or 0.0),
        "first": first,
        "last": last,
    }


def format_table(rows: list[dict]) -> str:
    header = (
        "| manifest | windows | return | maxDD | ret/DD | trades | win% | expectancy | "
        "total | Sharpe | PF | DSR | clock |"
    )
    sep = "|" + "---|" * 13
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| `{r['run_id']}` | {r['windows']} | {r['return_pct']:+.2f}% | "
            f"{r['max_drawdown_pct']:.2f}% | {r['return_per_dd']:.2f} | {r['trades']} | "
            f"{r['win_rate_pct']:.1f}% | ${r['expectancy']:.2f} | ${r['total']:,.0f} | "
            f"{r['sharpe']:.3f} | {r['profit_factor']:.2f} | {r['dsr']:.3f} | "
            f"{r['first']}..{r['last']} |"
        )
    return "\n".join(lines)


def clock_warning(rows: list[dict]) -> str | None:
    """A sweep only compares if every row saw the same bars."""
    clocks = {(r["first"], r["last"]) for r in rows}
    if len(clocks) <= 1:
        return None
    return (
        "WARNING: rows were replayed on different clocks — re-run the odd ones "
        "before reading the table as a comparison."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="compare portfolio-backtest replays")
    parser.add_argument("run_ids", nargs="+", help="experiment run_ids with replay artifacts")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--configs-dir", default=str(DEFAULT_CONFIGS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results_dir, configs_dir = Path(args.results_dir), Path(args.configs_dir)
    rows = [row(results_dir, configs_dir, run_id) for run_id in args.run_ids]
    rows.sort(key=lambda r: r["return_pct"], reverse=True)
    print(format_table(rows))
    warning = clock_warning(rows)
    if warning:
        print(f"\n{warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
