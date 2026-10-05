"""Price a resting-limit (maker) entry against the desk's market-on-open entry.

The desk pays the **taker** crypto fee (25 bps/side, Alpaca fee schedule tier 1)
and takes whatever the next bar's open gives it. Alpaca charges 15 bps when the
order *adds* liquidity (maker), so a resting limit below the signal close would
pay 10 bps less per crypto entry — and, on the fills it gets, buy at a better
price. The cost of that is the fills it never gets: a buy intent that expires
unfilled is a trade the desk never takes, and the frozen strategy needs a
*fresh* cross to re-enter, so the whole move is missed.

This script prices all three effects on one control replay:

* **fill rate** — share of the control's own buy entries a limit at
  ``signal_close * (1 - offset)`` would still have filled (the entry bar's low
  reaching the limit);
* **price improvement** — the better entry the fills get, in % and in dollars,
  re-derived with the notional fixed (``qty = notional / fill_price``);
* **lost trades** — the realized P&L of the entries that would not have filled,
  taken from the control replay itself.

It is a first-order estimate, not a re-simulation: the exit path (and with it
the fill-derived 8% stop) is held fixed at the control's, and a second-order
slot effect (a freed slot picked up by a later cross) is not modelled. Read it
as a screen: if the fill rate is high and the missed trades are not the big
winners, a full re-simulation is worth an experiment manifest.

Run:
  .venv/bin/python scripts/analysis_limit_entry.py \
      --artifact results/exp-slots-all-recover/portfolio-backtest \
      --notional 625
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd

# Alpaca crypto fee schedule, tier 1 (30d volume < $100k). A resting limit that
# does not cross the spread is a maker order; the desk's market-on-open entry is
# a taker order. Stocks are commission-free, so their "cost" is the 5 bps
# slippage the replay charges on a marketable fill — a passive fill pays none.
CRYPTO_TAKER_BPS = 25.0
CRYPTO_MAKER_BPS = 15.0
EQUITY_SLIPPAGE_BPS = 5.0
DEFAULT_OFFSETS = (0.25, 0.5, 1.0)


def load_cached_bars(data_dir: Path) -> dict[str, pd.DataFrame]:
    """Full-history cached bars, keyed by symbol ('BTC/USD', 'AAPL', ...)."""
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(glob.glob(str(data_dir / "alpaca_*_1d_2022-01-01.parquet"))):
        name = Path(path).name
        if "_crypto_" in name:
            symbol = name.split("_crypto_", 1)[1].split("_1d_")[0].replace("_", "/")
        elif "_stock_" in name:
            symbol = name.split("_stock_", 1)[1].split("_1d_")[0]
        else:  # pragma: no cover - defensive, the glob above is specific
            continue
        frames[symbol] = pd.read_parquet(path).sort_index()
    # The replay normalizes its bar index to naive midnights; cached equity bars
    # are stamped at 04:00 UTC (midnight ET), so align both here or every stock
    # entry date would miss the index.
    for symbol, frame in frames.items():
        frame.index = pd.to_datetime(frame.index).normalize()
        frames[symbol] = frame
    return frames


def round_trips(trades: pd.DataFrame) -> list[dict]:
    """Pair each sell with the open buy of that symbol (one position at a time)."""
    trips: list[dict] = []
    open_buys: dict[str, dict] = {}
    for row in trades.sort_values("date").itertuples():
        if row.side == "buy":
            open_buys[row.symbol] = {
                "symbol": row.symbol,
                "asset": row.asset,
                "entry_date": pd.Timestamp(row.date),
                "entry_price": float(row.price),
                "entry_fee": float(row.fee),
            }
        elif row.side == "sell" and row.symbol in open_buys:
            trip = open_buys.pop(row.symbol)
            trip.update(
                exit_date=pd.Timestamp(row.date),
                exit_price=float(row.price),
                exit_fee=float(row.fee),
                realized_pl=float(row.realized_pl),
            )
            trips.append(trip)
    return trips


def price_trip(
    trip: dict,
    bars: pd.DataFrame,
    offset_pct: float,
    notional: float,
) -> dict | None:
    """Would a resting limit at signal_close*(1-offset) have filled, and at what?"""
    dates = bars.index
    entry_date = trip["entry_date"]
    if entry_date not in dates:
        return None
    pos = dates.get_loc(entry_date)
    if not isinstance(pos, int) or pos == 0:
        return None
    signal_close = float(bars["Close"].iloc[pos - 1])
    open_price = float(bars["Open"].iloc[pos])
    low = float(bars["Low"].iloc[pos])
    limit = signal_close * (1 - offset_pct / 100)
    filled = low <= limit
    # A resting limit fills at the limit; if the bar opens through it, the open
    # (already better than the limit) is the realistic fill.
    fill_price = min(open_price, limit) if filled else entry_date
    trip = dict(trip)
    trip["signal_close"] = signal_close
    trip["filled"] = bool(filled)
    trip["fill_price"] = float(fill_price) if filled else None
    return trip


def estimate(
    trips: list[dict],
    offset_pct: float,
    notional: float,
) -> dict:
    """Dollar effects of a maker entry at this offset, vs the control's fills."""
    filled, unfilled = [], []
    for trip in trips:
        if trip.get("fill_price") is None:
            unfilled.append(trip)
            continue
        asset = trip["asset"]
        entry_rate = (
            (CRYPTO_MAKER_BPS if asset == "crypto" else 0.0) / 10_000
        )
        exit_rate = (
            (CRYPTO_TAKER_BPS if asset == "crypto" else EQUITY_SLIPPAGE_BPS)
            / 10_000
        )
        e_new = trip["fill_price"]
        exit_price = trip["exit_price"]
        qty = notional / e_new
        pl_new = (exit_price - e_new) * qty - notional * entry_rate - qty * exit_price * exit_rate
        filled.append({**trip, "pl_new": pl_new})

    improvement_pct = [
        100 * (1 - t["fill_price"] / t["signal_close"]) for t in filled
    ]
    gross_improvement = sum(
        t["pl_new"] - t["realized_pl"] for t in filled
    )
    lost = sum(t["realized_pl"] for t in unfilled)
    n = len(filled) + len(unfilled)
    return {
        "offset_%": offset_pct,
        "buys": n,
        "filled": len(filled),
        "fill_%": 100 * len(filled) / n if n else 0.0,
        "mean_improvement_%": (
            sum(improvement_pct) / len(improvement_pct) if filled else 0.0
        ),
        "kept_pl": sum(t["realized_pl"] for t in filled),
        "pl_new": sum(t["pl_new"] for t in filled),
        "improvement_$": gross_improvement,
        "lost_pl_$": lost,
        "net_$": sum(t["pl_new"] for t in filled) - lost,
        "control_$": sum(t["realized_pl"] for t in filled) + lost,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default="results/exp-slots-all-recover/portfolio-backtest")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--notional", type=float, default=625.0)
    ap.add_argument("--offsets", default=",".join(str(o) for o in DEFAULT_OFFSETS))
    args = ap.parse_args()

    trades = pd.read_csv(Path(args.artifact) / "trades.csv")
    bars = load_cached_bars(Path(args.data_dir))
    offsets = [float(o) for o in args.offsets.split(",")]

    trips = round_trips(trades)
    priced = []
    for trip in trips:
        frame = bars.get(trip["symbol"])
        if frame is None:
            continue
        priced.append(trip)
    print(f"artifact   {args.artifact}")
    print(f"round trips matched to cached bars: {len(priced)} of {len(trips)}")
    if trips:
        years = (max(t["exit_date"] for t in trips) - min(t["entry_date"] for t in trips)).days / 365.25
        print(f"span       {years:.2f}y, notional ${args.notional:,.0f}, "
              f"crypto taker {CRYPTO_TAKER_BPS:.0f} bps -> maker {CRYPTO_MAKER_BPS:.0f} bps, "
              f"equity slippage {EQUITY_SLIPPAGE_BPS:.0f} bps -> 0 on a passive fill")

    rows = []
    skipped = 0
    for offset in offsets:
        prepared = []
        for trip in priced:
            frame = bars[trip["symbol"]]
            out = price_trip(trip, frame, offset, args.notional)
            if out is None:
                skipped += 1
            else:
                prepared.append(out)
        rows.append({**estimate(prepared, offset, args.notional), "leg": "all"})
        for leg in ("crypto", "stock"):
            leg_trips = [t for t in prepared if t["asset"] == leg]
            if leg_trips:
                rows.append({**estimate(leg_trips, offset, args.notional), "leg": leg})
    if skipped:
        print(f"WARNING    {skipped} price(s) skipped (entry bar missing from the cache) — "
              "the table is partial; re-fetch the bars before reading it")
    table = pd.DataFrame(rows)[
        ["leg", "offset_%", "buys", "filled", "fill_%", "mean_improvement_%",
         "kept_pl", "pl_new", "improvement_$", "lost_pl_$", "net_$", "control_$"]
    ]
    print("\n== resting-limit entry at signal_close * (1 - offset) ==")
    print(table.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print("\nfirst-order screen: net_$ = filled P&L under the limit entry - the P&L of the "
          "entries the limit would never have filled (exit path held fixed at the control's)")


if __name__ == "__main__":
    main()
