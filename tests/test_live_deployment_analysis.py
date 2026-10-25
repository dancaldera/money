"""Tests for the live-run deployment/attribution lens.

``scripts/analysis_live_deployment.py`` exists because ``money run-report`` shows
statistics, not capital: it never says how much of the frozen caps the live run
uses, where the money sits, or which guard turned a fresh cross away. Those are
the numbers a size step is decided on, so the primitives are pinned here on
synthetic positions/decisions; the script itself reads the gitignored
``results/run3/ledger.sqlite``.

Two units are easy to get wrong and are regression-guarded here: the ledger's
``drawdown_pct`` column is already in percent (0.097 = 0.097%, halting at 5), and
a BUY signal on a symbol the portfolio already holds is a no-op, not a blocked
entry (counting it as blocked would invent guard pressure that does not exist).
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from types import SimpleNamespace

import pytest

from .run2_helpers import ROOT

_SPEC = importlib.util.spec_from_file_location(
    "analysis_live_deployment", ROOT / "scripts" / "analysis_live_deployment.py"
)
assert _SPEC and _SPEC.loader
live = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live)

PORTFOLIO = SimpleNamespace(
    position_notional=625.0,
    max_positions=17,
    max_gross_exposure=10_625.0,
    max_crypto_positions=8,
    max_crypto_exposure=5_000.0,
    max_stock_positions=9,
    max_stock_exposure=5_625.0,
    drawdown_halt_pct=5.0,
)


def _position(qty: str, avg_entry: str, asset: str = "stock") -> SimpleNamespace:
    return SimpleNamespace(qty=Decimal(qty), avg_entry=Decimal(avg_entry), asset=asset)


def _decision(symbol, signal, action, reason, status="recorded", bar_end="2026-09-29T00:00:00+00:00"):
    return {
        "symbol": symbol,
        "signal": signal,
        "action": action,
        "reason": reason,
        "status": status,
        "signal_price": "100",
        "bar_end": bar_end,
        "decided_at": "2026-09-29T00:35:00+00:00",
    }


def test_latest_marks_keeps_the_newest_bar_per_symbol():
    rows = [
        _decision("AAPL", "BUY", "buy_intent", "allowed", bar_end="2026-09-28T04:00:00+00:00"),
        _decision("AAPL", "HOLD", "none", "holding", bar_end="2026-09-29T04:00:00+00:00"),
        {"symbol": "AMD", "signal_price": "not-a-price", "bar_end": "2026-09-29T04:00:00+00:00"},
    ]
    rows[0]["signal_price"], rows[1]["signal_price"] = "101.5", "103.25"
    marks = live.latest_marks(rows)
    assert marks == {"AAPL": 103.25}
    # A malformed price must not become a mark of 0.0.
    assert "AMD" not in marks


def test_position_rows_marks_to_last_close_and_flags_unmarked():
    positions = {
        "AAPL": _position("1.9", "300"),
        "BTC/USD": _position("0.005", "80000", asset="crypto"),
    }
    rows = live.position_rows(positions, {"AAPL": 330.0})
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["AAPL"]["market_value"] == pytest.approx(627.0)
    assert by_symbol["AAPL"]["unrealized_pl"] == pytest.approx(57.0)
    assert by_symbol["AAPL"]["unrealized_pct"] == pytest.approx(10.0)
    assert by_symbol["AAPL"]["mark_source"] == "mark"
    # No recorded bar for the symbol: priced at cost, flagged, never "flat".
    assert by_symbol["BTC/USD"]["mark_source"] == "cost"
    assert by_symbol["BTC/USD"]["unrealized_pl"] == 0.0
    # Sorted by market value, biggest first.
    assert [row["symbol"] for row in rows] == ["AAPL", "BTC/USD"]


def test_deployment_reports_cap_utilisation_and_room():
    rows = [
        live.position_rows({"AAPL": _position("1", "600")}, {"AAPL": 625.0})[0],
        live.position_rows({"BTC/USD": _position("1", "600", "crypto")}, {"BTC/USD": 625.0})[0],
    ]
    dep = live.deployment(rows, equity=100_000.0, portfolio=PORTFOLIO)
    assert dep["gross_exposure"] == pytest.approx(1_250.0)
    assert dep["gross_utilization_pct"] == pytest.approx(100 * 1_250 / 10_625)
    assert dep["deployment_pct_of_equity"] == pytest.approx(1.25)
    assert dep["slot_utilization_pct"] == pytest.approx(100 * 2 / 17)
    # $1,250 of the $10,625 gross cap is used and 15 of 17 slots are free, so
    # $9,375 (15 x $625) is what the caps still allow a fresh cross to deploy.
    assert dep["room_dollars"] == pytest.approx(9_375.0)
    assert dep["per_asset"]["crypto"]["positions"] == 1
    assert dep["per_asset"]["stock"]["exposure"] == pytest.approx(625.0)


def test_deployment_room_follows_the_tighter_of_gross_and_slots():
    row = live.position_rows({"AAPL": _position("1", "600")}, {"AAPL": 3_000.0})[0]
    dep = live.deployment([row] * 3, equity=100_000.0, portfolio=PORTFOLIO)
    # 3 x $3,000 = $9,000 of the $10,625 gross cap: gross binds ($1,625 left)
    # long before the 14 free slots do (14 x $625 = $8,750).
    assert dep["gross_room"] == pytest.approx(1_625.0)
    assert dep["slot_room_notional"] == pytest.approx(14 * 625.0)
    assert dep["room_dollars"] == pytest.approx(1_625.0)


def test_intent_census_separates_blocked_from_holding_and_stops():
    census = live.intent_census(
        [
            _decision("AAPL", "BUY", "buy_intent", "allowed", status="filled"),
            _decision("AMD", "BUY", "none", "gross_exposure"),
            _decision("GOOGL", "BUY", "none", "holding"),
            _decision("MSFT", "BUY", "buy_intent", "adverse_gap:103.5", status="expired"),
            _decision("BTC/USD", "HOLD", "none", "no_fresh_cross"),
            _decision("DOGE/USD", "SELL", "none", "no_fresh_cross"),
            _decision("AAPL", "STOP", "sell_intent", "stop:-8.4112%", status="filled"),
        ]
    )
    assert census["buy_signals"] == 4
    assert census["buy_signals_while_holding"] == 1
    assert census["new_entries_eligible"] == 3
    assert census["intents"] == 2
    assert census["blocked"] == {"gross_exposure": 1}
    assert census["entry_status"] == {"filled": 1, "expired": 1}
    assert census["expired_reasons"] == {"adverse_gap:103.5": 1}
    # A stop is its own signal name: it must show up as an exit, never as a buy.
    assert census["exit_signals"] == {"SELL": 1, "STOP": 1}
    assert census["exit_intents"] == {"STOP": 1}
    assert census["exit_status"] == {"filled": 1}


def test_scaling_arithmetic_multiplies_dollars_not_percent():
    scaled = live.scaling_arithmetic(250.0, 100_000.0, (1.0, 2.0))
    assert [item["dollars"] for item in scaled] == pytest.approx([250.0, 500.0])
    assert [item["pct_of_equity"] for item in scaled] == pytest.approx([0.25, 0.5])


def _trade(symbol, asset, gross, fees):
    return {"symbol": symbol, "asset": asset, "gross": gross, "fees": fees, "pnl": gross - fees}


def test_distribution_is_sample_stdev_and_zero_is_not_a_win():
    dist = live.distribution([10.0, -5.0, 0.0, 30.0])
    assert dist["n"] == 4
    assert dist["mean"] == pytest.approx(8.75)
    assert dist["stdev"] == pytest.approx(15.478, abs=1e-3)  # n-1, not the population sd
    assert dist["win_rate_pct"] == pytest.approx(50.0)  # a flat trip is not a win
    assert live.distribution([]) == {"n": 0, "mean": 0.0, "stdev": 0.0, "win_rate_pct": 0.0}
    assert live.distribution([7.0])["stdev"] == 0.0


def test_realized_census_separates_legs_symbols_and_costs():
    census = live.realized_census(
        [
            _trade("AMD", "stock", 82.0, 0.75),
            _trade("AAVE/USD", "crypto", 54.0, 2.3),
            _trade("DOGE/USD", "crypto", -47.0, 2.5),
        ]
    )
    assert census["trips"] == 3
    assert census["gross"] == pytest.approx(89.0)
    assert census["fees"] == pytest.approx(5.55)
    assert census["net"] == pytest.approx(83.45)
    assert census["legs"]["crypto"] == pytest.approx(
        {"trips": 2, "wins": 1, "gross": 7.0, "fees": 4.8, "net": 2.2}, abs=1e-9
    )
    # Worst net first: the losing trip is what the reader needs to see.
    assert list(census["symbols"]) == ["AMD", "AAVE/USD", "DOGE/USD"]
    assert live.realized_census([])["trips"] == 0


def test_replay_reference_reads_only_closing_rows(tmp_path):
    artifact = tmp_path / "exp-x" / "portfolio-backtest"
    artifact.mkdir(parents=True)
    path = artifact / "trades.csv"
    path.write_text(
        "date,symbol,asset,side,qty,price,fee,realized_pl,reason\n"
        "2026-01-02,BTC/USD,crypto,buy,0.01,40000,1.0,0.0,\n"
        "2026-01-03,BTC/USD,crypto,sell,0.01,44000,1.0,39.0,reverse_cross\n"
        "2026-01-04,AAPL,stock,buy,1,100,0.0,0.0,\n"
        "2026-01-05,AAPL,stock,sell,1,90,0.0,-10.0,stop:-10%\n"
        "2026-01-06,AMD,stock,sell,1,90,0.0,not-a-number,\n"
    )
    ref = live.replay_reference(path)
    # The two opens carry 0 and must not drag the reference mean; the malformed
    # realized_pl row is skipped instead of becoming a zero.
    assert ref["n"] == 2
    assert ref["mean"] == pytest.approx(14.5)
    assert ref["stdev"] == pytest.approx(34.648, abs=1e-3)
    assert ref["win_rate_pct"] == pytest.approx(50.0)
    assert ref["run"] == "exp-x"
    assert live.replay_reference(tmp_path / "missing.csv") is None
    path.write_text("date,symbol,asset,side,qty,price,fee,realized_pl,reason\n")
    assert live.replay_reference(path) is None  # too short to have a dispersion


def test_parity_sizes_the_gap_against_the_replay_not_the_live_sample():
    reference = {"run": "ctl", "n": 366, "mean": 25.75, "stdev": 251.41, "win_rate_pct": 30.1}
    live_pnls = [-7.64] * 7
    result = live.parity(live_pnls, reference, equity=100_000.0)
    assert result["gap_per_trade"] == pytest.approx(-33.39)
    # 33.39 / (251.41 / sqrt(7)) = 0.35 sigma: a seven-trade record is noise.
    assert result["z"] == pytest.approx(-0.351, abs=1e-3)
    assert result["inside_noise"] is True
    assert result["shortfall_dollars"] == pytest.approx(-233.73)
    assert result["shortfall_pct_of_equity"] == pytest.approx(-0.2337, abs=1e-3)
    # 2 x 251.41 / 33.39 squared: how long the same gap needs to be readable.
    assert result["n_for_2sigma"] == 227
    assert 0 < result["win_p_value"] < 1
    # A gap of zero (live == replay) needs no patience and is not a shortfall.
    level = live.parity([25.75] * 7, reference, equity=100_000.0)
    assert level["gap_per_trade"] == pytest.approx(0.0)
    assert level["n_for_2sigma"] is None
    assert level["inside_noise"] is True
    # Outside 1.645 sigma with a real, large gap: that is the "investigate" case.
    bad = live.parity([-100.0] * 36, reference, equity=100_000.0)
    assert bad["inside_noise"] is False
    # Degenerate reference (no dispersion) must not divide by zero.
    flat = live.parity([-1.0], {**reference, "stdev": 0.0}, equity=100_000.0)
    assert flat["z"] is None and flat["inside_noise"] is None
    # No reference / no trades: the comparison is skipped, never invented.
    assert live.parity([-7.64] * 7, None, 1.0)["z"] is None
    assert live.parity([], reference, 1.0)["z"] is None


def test_parity_lines_state_which_case_the_record_is_in():
    reference = {"run": "ctl", "n": 366, "mean": 25.75, "stdev": 251.41, "win_rate_pct": 30.1}
    noisy = live.parity_lines(live.parity([-7.64] * 7, reference, 100_000.0))
    assert "replay parity vs ctl (366 closed trips" in noisy[1]
    assert "inside the replay's noise" in "\n".join(noisy)
    assert "227 trades to reach" in "\n".join(noisy)
    broken = live.parity_lines(live.parity([-100.0] * 36, reference, 100_000.0))
    # Outside the envelope asks for an investigation, never a re-tune.
    assert "outside the replay envelope — investigate, do not re-tune" in "\n".join(broken)
    assert "no reference artifact" in "\n".join(live.parity_lines(live.parity([-1.0], None, 1.0)))
    assert "no live round trip yet" in "\n".join(
        live.parity_lines(live.parity([], reference, 1.0))
    )
    undersized = live.parity([-1.0], {**reference, "stdev": 0.0}, 1.0)
    assert "no dispersion" in "\n".join(live.parity_lines(undersized))
    assert live.parity_lines(None) == []


def test_round_trip_lines_skip_an_empty_census():
    assert live.round_trip_lines(None) == []
    assert live.round_trip_lines({"trips": 0}) == []
    census = live.realized_census([_trade("AMD", "stock", 82.0, 0.75)])
    census["portfolio"], census["fees_booked"] = "baseline", 12.15
    text = "\n".join(live.round_trip_lines(census))
    assert "closed round trips [baseline]: 1 trips" in text
    assert "fees $0.75 of $12.15 booked" in text
    assert "by symbol: AMD $+81.25 (1/1)" in text


def test_render_keeps_the_ledger_drawdown_percent_scale():
    report = {
        "run_id": "run3",
        "portfolio": "baseline",
        "status": "active",
        "halted": False,
        "snapshot_at": "2026-09-30T13:53:37+00:00",
        "snapshot_source": "alpaca-paper",
        "snapshot_drawdown_pct": 0.0975,
        "halt_pct": 5.0,
        "fees": 7.55,
        "sessions": 14,
        "positions": [],
        "totals": {
            "market_value": 0.0,
            "unrealized_pl": 0.0,
            "unrealized_pct": 0.0,
            "unrealized_pct_of_equity": 0.0,
            "unmarked": 0,
        },
        "deployment": live.deployment([], 100_000.0, PORTFOLIO),
        "census": live.intent_census([]),
        "scaling": live.scaling_arithmetic(0.0, 100_000.0),
        "realized": {
            "completed_trades": 0,
            "win_rate_pct": 0.0,
            "realized_expectancy": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
        },
    }
    text = live.render(report)
    # Regression: the column already carries percent units (0.0975 = 0.0975%,
    # halting at 5). Multiplying by 100 printed "drawdown=9.75% (halt at 5.00%)".
    assert "drawdown=0.10% (halt at 5.00%)" in text
    assert "9.75%" not in text
    assert "room before a cap binds: $10,625.00" in text
