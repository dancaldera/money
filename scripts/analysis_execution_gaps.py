"""Measure how much the desk's execution timing costs (or loses) per signal.

Two effects, both measured on the cached Alpaca daily bars in ``data/``:

1. Crypto latency — the daily run's slot decides which crypto bar is the newest
   *closed* one. At 17:00 CST (23:00 UTC) that bar closed ~23h earlier, so the
   entry lands a full daily bar after the signal; at 18:35 CST (00:35 UTC) the
   just-closed bar is used and the entry lands ~35 min after it. Prints the drift
   paid per cross and the share of crosses the frozen 3% crypto gap cap expires
   (an expired intent is a trade the desk never takes, and a fresh cross is
   required to re-enter, so the whole move is missed).

2. Stock gap cap — stock intents are submitted at the next open as a DAY limit
   at signal_close * (1 + 2%). Prints the share filled at the open, the share
   that only fills intraday, and the share that never fills at all (a lost
   entry: the intent is marked submitted and never retried).

Run:  .venv/bin/python scripts/analysis_execution_gaps.py [--data-dir data]
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd

FAST, SLOW = 10, 30
CRYPTO_GAP, STOCK_GAP = 0.03, 0.02
NOTIONAL = 625.0


def fresh_crosses(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Bars where SMA(FAST) crosses SMA(SLOW): (fresh up, fresh down).

    Uses ``shift(1, fill_value=False)``: shifting a bool Series yields object
    dtype, where ``~`` is integer bitwise negation (True -> -2, i.e. truthy) and
    every bar would look like a fresh cross.
    """
    above = close.rolling(FAST).mean() > close.rolling(SLOW).mean()
    was_above = above.shift(1, fill_value=False)
    return above & ~was_above, ~above & was_above


def late_drift_cost(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Adverse move between the signal bar's close and one bar later, by side."""
    rows = []
    for symbol, df in frames.items():
        close = df["Close"]
        drift = close.shift(-1) / close - 1.0
        up, down = fresh_crosses(close)
        buys = drift[up].dropna()
        sells = (-drift[down]).dropna()  # a sell also loses when the price falls
        rows.append(
            {
                "symbol": symbol,
                "buys": len(buys),
                "sells": len(sells),
                "buy_drift_%": 100 * buys.mean() if len(buys) else 0.0,
                "sell_cost_%": 100 * sells.mean() if len(sells) else 0.0,
                "buys_expired_%": 100 * (buys > CRYPTO_GAP).mean() if len(buys) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def stock_gap_fill(df: pd.DataFrame, gap: float = STOCK_GAP) -> dict[str, float]:
    """How a next-open DAY limit at signal_close * (1 + gap) resolves."""
    close, low, open_ = df["Close"], df["Low"], df["Open"]
    sig, _ = fresh_crosses(close)
    limit = close * (1 + gap)
    fills_open = open_.shift(-1) <= limit
    resting = (~fills_open) & (low.shift(-1) <= limit)
    n = int(sig.sum())
    if not n:
        return {"signals": 0, "at_open_%": 0.0, "resting_%": 0.0, "never_%": 0.0}
    return {
        "signals": n,
        "at_open_%": 100 * fills_open[sig].mean(),
        "resting_%": 100 * resting[sig].mean(),
        "never_%": 100 * (~(fills_open | resting))[sig].mean(),
    }


def _load(pattern: str, data_dir: Path) -> dict[str, pd.DataFrame]:
    frames = {}
    for path in sorted(glob.glob(str(data_dir / pattern))):
        symbol = Path(path).name.split("_", 2)[2].split("_1d_")[0].replace("_", "/")
        frames[symbol] = pd.read_parquet(path).sort_index()
    return frames


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)

    crypto = _load("alpaca_crypto_*_1d_2022-01-01.parquet", data_dir)
    per = late_drift_cost(crypto)
    print("== crypto: acting one daily bar late (17:00 slot) ==")
    print(per.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    buys = per["buys"].sum()
    expired = (per["buys"] * per["buys_expired_%"] / 100).sum()
    years = len(next(iter(crypto.values()))) / 365.25
    print(f"crosses {int((per['buys'] + per['sells']).sum())} over {years:.1f}y "
          f"({(per['buys'] + per['sells']).sum() / years / len(crypto):.1f}/symbol/year)")
    print(f"buy entries expired by the {CRYPTO_GAP:.0%} cap: {expired:.0f} "
          f"({100 * expired / buys:.1f}% of {int(buys)} buys, ~{expired / years:.1f}/year)")

    stocks = _load("alpaca_stock_*_1d_2022-01-01.parquet", data_dir)
    rows = []
    for symbol, df in stocks.items():
        rows.append({"symbol": symbol, **stock_gap_fill(df)})
    table = pd.DataFrame(rows)
    print("\n== stocks: next-open DAY limit at signal close +2% ==")
    print(table.to_string(index=False, float_format=lambda x: f"{x:,.1f}"))
    sig = table["signals"].sum()
    lost = (table["signals"] * table["never_%"] / 100).sum()
    print(f"signals {sig} over {years:.1f}y; never filled {lost:.1f} "
          f"({100 * lost / sig:.1f}%, ~{lost / years:.1f}/year)")
    print(f"\nnotional per entry ${NOTIONAL:.0f}; expectancy per completed trade is "
          "printed by `money portfolio-backtest`")


if __name__ == "__main__":
    main()
