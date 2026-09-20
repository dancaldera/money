"""Price a replay's drawdown halt: how many times it fired and how long the desk sat flat.

The deployment ladders in ``docs/experiments.md`` show what a bigger
``position_notional`` earns, but the return and the max drawdown on their own do
not say what the halt *cost*: an opt-in ``halt_recovery_*`` rule turns a frozen
desk back on, and the bill for that is paid in **days out of the market** (and in
the drawdown the desk is allowed to keep while waiting). This script reads an
existing ``portfolio-backtest`` artifact and re-runs the halt state machine over
its equity curve, using the same ``trading.run2.risk.halt_action`` the live
service and the replay call — so the episode it reports is the episode the desk
would have lived, not a lookalike.

For each episode it prints the halt date, the drawdown that tripped it, the
resume date and which branch of the recovery rule fired (``dd`` = drawdown fell
back to ``halt_recovery_drawdown_pct``, ``calendar`` = ``halt_recovery_days``
passed), plus how many sessions were flat and how deep the drawdown got while
halted. A latch manifest (no recovery keys, e.g. ``scale-4x``) never resumes, so
its episode runs to the end of the window — that is the failure mode the recovery
rule exists to remove.

Run:  .venv/bin/python scripts/analysis_halt_episodes.py \
          --artifact results/exp-slots-all-2x-recover/portfolio-backtest \
          --run-config config/experiments/exp-slots-all-2x-recover.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from trading.run2.config import load_run_config
from trading.run2.risk import DRAWDOWN_HALT_PREFIX, drawdown_pct, halt_action


def halt_episodes(equity: pd.Series, cfg) -> pd.DataFrame:
    """Replay the halt/recovery policy over an equity curve, one row per halt.

    Mirrors ``simulate_portfolio`` exactly: the drawdown is measured against a
    running high-water mark, and a resume re-baselines that mark at the resume
    equity (the peak the desk already lost is not counted twice).
    """
    series = pd.Series(equity, dtype=float).dropna()
    rows: list[dict] = []
    high_water = 0.0
    halted = False
    halt_day = None
    halt_dd = 0.0
    deepest = 0.0
    flat_days = 0

    for day, value in series.items():
        high_water = max(high_water, float(value))
        dd = drawdown_pct(float(value), high_water)
        action = halt_action(
            cfg,
            dd,
            halted=halted,
            halt_reason=DRAWDOWN_HALT_PREFIX if halted else None,
            days_since_halt=(day - halt_day).days if halt_day is not None else None,
        )
        if action == "halt":
            halted = True
            halt_day = day
            halt_dd = dd
            deepest = dd
            flat_days = 0
            continue
        if not halted:
            continue
        flat_days += 1
        deepest = max(deepest, dd)
        if action == "resume":
            recovery_dd = cfg.portfolio.halt_recovery_drawdown_pct
            recovery_days = cfg.portfolio.halt_recovery_days
            if recovery_dd is not None and dd <= recovery_dd:
                resumed_by = "dd"
            elif recovery_days is not None and (day - halt_day).days >= recovery_days:
                resumed_by = "calendar"
            else:
                resumed_by = "unknown"
            rows.append(
                {
                    "halt_date": halt_day,
                    "resume_date": day,
                    "resumed_by": resumed_by,
                    "halt_dd_pct": -halt_dd,
                    "deepest_dd_pct": -deepest,
                    "flat_days": flat_days,
                }
            )
            halted = False
            halt_day = None
            high_water = float(value)

    if halted:  # a latch (or a window that ends while still halted)
        rows.append(
            {
                "halt_date": halt_day,
                "resume_date": None,
                "resumed_by": "never",
                "halt_dd_pct": -halt_dd,
                "deepest_dd_pct": -deepest,
                "flat_days": flat_days,
            }
        )
    columns = ["halt_date", "resume_date", "resumed_by", "halt_dd_pct", "deepest_dd_pct", "flat_days"]
    return pd.DataFrame(rows, columns=columns)


def load_equity(artifact: Path) -> pd.Series:
    frame = pd.read_csv(artifact / "equity.csv")
    index = pd.to_datetime(frame["date"])
    return pd.Series(frame["equity"].to_numpy(dtype=float), index=index, name="equity")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifact", required=True, help="results/<run>/portfolio-backtest directory")
    ap.add_argument("--run-config", required=True, help="manifest whose halt_* settings to apply")
    args = ap.parse_args()

    cfg = load_run_config(Path(args.run_config), strict=False)
    equity = load_equity(Path(args.artifact))
    episodes = halt_episodes(equity, cfg)

    print(f"halt episodes — {cfg.run_id} ({len(equity)} sessions, "
          f"halt {cfg.portfolio.drawdown_halt_pct:g}%, "
          f"recovery dd {cfg.portfolio.halt_recovery_drawdown_pct} / days {cfg.portfolio.halt_recovery_days})")
    if episodes.empty:
        print("  none: the drawdown never reached the halt on this path")
        return
    print(episodes.to_string(index=False))
    flat = int(episodes["flat_days"].sum())
    share = 100 * flat / len(equity) if len(equity) else 0.0
    never = int((episodes["resume_date"].isna()).sum())
    print(f"\n  {len(episodes)} halt(s), {flat} sessions flat ({share:.1f}% of the window), "
          f"deepest {episodes['deepest_dd_pct'].min():.2f}%, never resumed {never}")


if __name__ == "__main__":
    main()
