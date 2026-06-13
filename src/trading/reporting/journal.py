"""Turn a backtest stats Series into a tidy summary and log it for later review."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Project root is three levels up: src/trading/reporting/journal.py
RESULTS_DIR = Path(__file__).resolve().parents[3] / "results"
JOURNAL_PATH = RESULTS_DIR / "journal.csv"

# Order of columns in the journal CSV.
_FIELDS = [
    "timestamp",
    "symbol",
    "asset",
    "strategy",
    "timeframe",
    "since",
    "trades",
    "win_rate_pct",
    "return_pct",
    "buy_hold_pct",
    "max_drawdown_pct",
    "sharpe",
    "final_equity",
]


def _g(stats: pd.Series, key: str, default=float("nan")):
    """Safely pull a value from the backtesting stats Series."""
    try:
        val = stats[key]
    except (KeyError, TypeError):
        return default
    return default if pd.isna(val) else val


def summarize(stats: pd.Series) -> dict:
    """Extract the win/loss metrics we care about from a stats Series."""
    return {
        "trades": int(_g(stats, "# Trades", 0)),
        "win_rate_pct": round(float(_g(stats, "Win Rate [%]", 0.0)), 2),
        "return_pct": round(float(_g(stats, "Return [%]", 0.0)), 2),
        "buy_hold_pct": round(float(_g(stats, "Buy & Hold Return [%]", 0.0)), 2),
        "max_drawdown_pct": round(float(_g(stats, "Max. Drawdown [%]", 0.0)), 2),
        "sharpe": round(float(_g(stats, "Sharpe Ratio", 0.0)), 3),
        "final_equity": round(float(_g(stats, "Equity Final [$]", 0.0)), 2),
    }


def record_run(meta: dict, stats: pd.Series) -> dict:
    """Append one backtest run to results/journal.csv and return the row written.

    ``meta`` should carry symbol/asset/strategy/timeframe/since.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": meta.get("symbol", ""),
        "asset": meta.get("asset", ""),
        "strategy": meta.get("strategy", ""),
        "timeframe": meta.get("timeframe", ""),
        "since": meta.get("since", ""),
        **summarize(stats),
    }
    write_header = not JOURNAL_PATH.exists()
    with JOURNAL_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return row
