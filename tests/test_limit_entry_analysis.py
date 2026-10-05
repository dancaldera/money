"""Tests for the resting-limit (maker) entry screen.

``scripts/analysis_limit_entry.py`` prices a passive entry against the desk's
market-on-open entry on one control replay. Its primitives are pinned here on
synthetic frames and a temporary parquet cache (the script itself reads the
gitignored ``data/`` only when run by hand).
"""

from __future__ import annotations

import importlib.util

import pandas as pd
import pytest

from .run2_helpers import ROOT

_SPEC = importlib.util.spec_from_file_location(
    "analysis_limit_entry", ROOT / "scripts" / "analysis_limit_entry.py"
)
assert _SPEC and _SPEC.loader
limit_entry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(limit_entry)


def _bars(rows: list[tuple[str, float, float, float, float]], stamp_hour: int = 0) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows]) + pd.Timedelta(hours=stamp_hour)
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [max(r[1], r[4]) * 1.01 for r in rows],
            "Low": [r[2] for r in rows],
            "Close": [r[4] for r in rows],
        },
        index=idx,
    )


def test_load_cached_bars_parses_symbols_and_normalizes_the_index(tmp_path):
    frame = _bars([("2024-01-02", 10, 9, 11, 10)], stamp_hour=4)
    frame.to_parquet(tmp_path / "alpaca_stock_AAPL_1d_2022-01-01.parquet")
    frame.to_parquet(tmp_path / "alpaca_crypto_BTC_USD_1d_2022-01-01.parquet")
    # A superseded live-window key must not be picked up by the glob.
    frame.to_parquet(tmp_path / "alpaca_stock_AAPL_1d_2025-08-27.parquet")

    frames = limit_entry.load_cached_bars(tmp_path)

    assert set(frames) == {"AAPL", "BTC/USD"}
    # Equity bars are stamped 04:00 UTC; the replay normalizes to midnights.
    assert frames["AAPL"].index[0] == pd.Timestamp("2024-01-02")


def test_round_trips_pairs_each_sell_with_its_open_buy():
    trades = pd.DataFrame(
        [
            {"date": "2024-01-01", "symbol": "AAPL", "asset": "stock", "side": "buy", "price": 10.0, "qty": 1.0, "fee": 0.0, "realized_pl": 0.0},
            {"date": "2024-01-05", "symbol": "AAPL", "asset": "stock", "side": "sell", "price": 11.0, "qty": 1.0, "fee": 0.0, "realized_pl": 1.0},
            {"date": "2024-01-03", "symbol": "MSFT", "asset": "stock", "side": "buy", "price": 5.0, "qty": 1.0, "fee": 0.0, "realized_pl": 0.0},
        ]
    )
    trips = limit_entry.round_trips(trades)

    assert len(trips) == 1  # the still-open MSFT buy is not a round trip
    assert trips[0]["symbol"] == "AAPL"
    assert trips[0]["exit_price"] == 11.0 and trips[0]["realized_pl"] == 1.0


def test_price_trip_fills_at_the_limit_or_the_better_open():
    frame = _bars(
        [
            ("2024-01-01", 100, 99, 101, 100),   # signal bar
            ("2024-01-02", 100.5, 98.0, 101, 99),  # dips to the limit
        ]
    )
    trip = {
        "symbol": "AAPL", "asset": "stock", "entry_date": pd.Timestamp("2024-01-02"),
        "entry_price": 100.5, "exit_price": 105.0, "realized_pl": 3.0,
        "entry_fee": 0.1, "exit_fee": 0.1,
    }
    filled = limit_entry.price_trip(trip, frame, 0.25, 625.0)
    assert filled["filled"] is True
    assert filled["signal_close"] == 100.0
    assert filled["fill_price"] == 99.75  # the resting limit, not the open

    # A bar that opens through the limit fills at the open (already better).
    gap = _bars(
        [
            ("2024-01-01", 100, 99, 101, 100),
            ("2024-01-02", 98.0, 97.0, 99, 98.5),
        ]
    )
    filled_gap = limit_entry.price_trip(trip, gap, 0.25, 625.0)
    assert filled_gap["fill_price"] == 98.0

    # A bar that never trades down to the limit leaves the intent unfilled.
    rally = _bars(
        [
            ("2024-01-01", 100, 99, 101, 100),
            ("2024-01-02", 103.0, 101.0, 105, 104),
        ]
    )
    assert limit_entry.price_trip(trip, rally, 0.25, 625.0)["filled"] is False


def test_price_trip_returns_none_when_the_entry_bar_is_unusable():
    frame = _bars([("2024-01-01", 100, 99, 101, 100)])
    first = dict(
        symbol="AAPL", asset="stock", entry_date=pd.Timestamp("2024-01-01"),
        entry_price=100.0, exit_price=100.0, realized_pl=0.0, entry_fee=0.0, exit_fee=0.0,
    )
    # Bar 0 has no signal bar before it.
    assert limit_entry.price_trip(first, frame, 0.25, 625.0) is None
    missing = {**first, "entry_date": pd.Timestamp("2030-01-01")}
    assert limit_entry.price_trip(missing, frame, 0.25, 625.0) is None


def test_estimate_charges_the_maker_fee_and_the_missed_trades():
    common = {"signal_close": 100.0, "exit_price": 110.0}
    filled_crypto = {**common, "asset": "crypto", "fill_price": 99.75, "realized_pl": 5.0}
    unfilled_crypto = {**common, "asset": "crypto", "fill_price": None, "realized_pl": 40.0}
    out = limit_entry.estimate([filled_crypto, unfilled_crypto], 0.25, 625.0)

    assert out["buys"] == 2 and out["filled"] == 1 and out["fill_%"] == 50.0
    assert out["lost_pl_$"] == 40.0
    assert out["net_$"] == out["pl_new"] - 40.0
    # Maker (15 bps) on the entry, taker (25 bps) on the exit, notional fixed.
    qty = 625.0 / 99.75
    expected = (110.0 - 99.75) * qty - 625.0 * 0.0015 - qty * 110.0 * 0.0025
    assert out["pl_new"] == pytest.approx(expected)
    assert out["improvement_$"] == pytest.approx(expected - 5.0)

    # A passive equity fill pays no entry slippage (the replay charges 5 bps),
    # while the (market) exit still does.
    equity_fill = dict(filled_crypto, asset="stock")
    equity = limit_entry.estimate([equity_fill], 0.25, 625.0)
    assert equity["pl_new"] == pytest.approx(
        (110.0 - 99.75) * qty - qty * 110.0 * 0.0005
    )


def test_main_prints_the_screen_per_leg(tmp_path, capsys):
    bars = _bars(
        [
            ("2024-01-01", 100, 99, 101, 100),
            ("2024-01-02", 100.5, 98.0, 101, 99),
            ("2024-01-03", 99, 98, 100, 105),
        ]
    )
    data = tmp_path / "data"
    data.mkdir()
    bars.to_parquet(data / "alpaca_stock_AAPL_1d_2022-01-01.parquet")
    artifact = tmp_path / "portfolio-backtest"
    artifact.mkdir()
    pd.DataFrame(
        [
            {"date": "2024-01-02", "symbol": "AAPL", "asset": "stock", "side": "buy", "qty": 6.0, "price": 100.5, "fee": 0.3, "realized_pl": 0.0, "reason": ""},
            {"date": "2024-01-03", "symbol": "AAPL", "asset": "stock", "side": "sell", "qty": 6.0, "price": 105.0, "fee": 0.3, "realized_pl": 26.4, "reason": ""},
        ]
    ).to_csv(artifact / "trades.csv", index=False)

    import sys

    argv = sys.argv
    sys.argv = [
        "analysis_limit_entry.py",
        "--artifact", str(artifact),
        "--data-dir", str(data),
        "--notional", "625",
        "--offsets", "0.25",
    ]
    try:
        limit_entry.main()
    finally:
        sys.argv = argv

    out = capsys.readouterr().out
    assert "resting-limit entry at signal_close * (1 - offset)" in out
    assert "round trips matched to cached bars: 1 of 1" in out
    assert " stock " in out or "\nstock " in out
