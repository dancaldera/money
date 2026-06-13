"""Run a strategy over a price series and return win/loss statistics."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from backtesting import Backtest


def run_backtest(
    df: pd.DataFrame,
    strategy,
    cash: float = 10_000,
    commission: float = 0.002,
    plot_path: str | Path | None = None,
) -> pd.Series:
    """Backtest ``strategy`` on ``df`` and return the stats Series.

    The starting cash is automatically scaled up if it is smaller than a few
    times the asset's max price — otherwise high-priced assets (e.g. BTC) could
    never fill a single unit and the backtest would show zero trades. This does
    not affect percentage-based metrics (return %, win rate, drawdown).
    """
    max_price = float(df["Close"].max())
    effective_cash = max(cash, max_price * 5)

    bt = Backtest(
        df,
        strategy,
        cash=effective_cash,
        commission=commission,
        finalize_trades=True,
    )
    stats = bt.run()

    if plot_path is not None:
        Path(plot_path).parent.mkdir(parents=True, exist_ok=True)
        bt.plot(filename=str(plot_path), open_browser=False)

    return stats
