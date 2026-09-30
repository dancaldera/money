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
