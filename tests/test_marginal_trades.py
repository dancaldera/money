"""Tests for ``scripts/analysis_marginal_trades.py``.

The script backs the "what did this rule change?" lens in ``docs/experiments.md``:
it reads the gitignored ``results/<run_id>/portfolio-backtest/trades.csv``
artifacts, so its primitives are pinned here on synthetic fixtures. The two
regressions that matter: an identical pair must report *nothing* added or
removed (otherwise every sweep reads as if the rule did something), and the
added-round-trip average must be expressed against the control's expectancy —
the number that decides whether a guard is earning its keep.
"""

from __future__ import annotations

import importlib.util

import pandas as pd
import pytest

from .run2_helpers import ROOT

_SPEC = importlib.util.spec_from_file_location(
    "analysis_marginal_trades", ROOT / "scripts" / "analysis_marginal_trades.py"
)
assert _SPEC and _SPEC.loader
marginal_trades = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(marginal_trades)


def _trade(date, symbol, asset, side, fee=0.0, realized_pl=0.0, reason=""):
    return {
        "date": date,
        "symbol": symbol,
        "asset": asset,
        "side": side,
        "qty": 1.0,
        "price": 100.0,
        "fee": fee,
        "realized_pl": realized_pl,
        "reason": reason,
    }


def _write(tmp_path, run_id, rows):
    art = tmp_path / "results" / run_id / "portfolio-backtest"
    art.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(art / "trades.csv", index=False)
    return art


CONTROL_ROWS = [
    _trade("2022-02-09", "BTC/USD", "crypto", "buy", fee=1.5625),
    _trade("2022-02-11", "BTC/USD", "crypto", "sell", fee=1.4375, realized_pl=-20.0, reason="stop"),
    _trade("2022-02-10", "AAPL", "stock", "buy"),
    _trade("2022-03-01", "AAPL", "stock", "sell", realized_pl=40.0),
]

# Same control round trips plus one crypto round trip the rule would unblock:
# a small winner (+$10) and a bigger loser (-$30), both far below the $10/trade
# control expectancy in the other direction — the fixture's point is attribution.
VARIANT_ROWS = CONTROL_ROWS + [
    _trade("2022-04-01", "ETH/USD", "crypto", "buy", fee=1.5625),
    _trade("2022-04-05", "ETH/USD", "crypto", "sell", fee=1.4375, realized_pl=10.0),
    _trade("2022-04-06", "SOL/USD", "crypto", "buy", fee=1.5625),
    _trade("2022-04-09", "SOL/USD", "crypto", "sell", fee=1.4375, realized_pl=-30.0),
]


def test_load_trades_raises_instead_of_reporting_empty(tmp_path):
    with pytest.raises(FileNotFoundError, match="no replay artifact"):
        marginal_trades.load_trades(tmp_path / "results", "exp-missing")


def test_load_trades_rejects_an_artifact_without_the_fill_columns(tmp_path):
    art = tmp_path / "results" / "exp-x" / "portfolio-backtest"
    art.mkdir(parents=True)
    (art / "trades.csv").write_text("date,symbol\n2022-01-01,BTC/USD\n")
    with pytest.raises(ValueError, match="missing column"):
        marginal_trades.load_trades(tmp_path / "results", "exp-x")


def test_identical_replays_report_nothing_added(tmp_path):
    _write(tmp_path, "exp-ctrl", CONTROL_ROWS)
    _write(tmp_path, "exp-same", CONTROL_ROWS)
    control = marginal_trades.load_trades(tmp_path / "results", "exp-ctrl")
    same = marginal_trades.load_trades(tmp_path / "results", "exp-same")
    stats = marginal_trades.marginal(control, same)
    assert stats["added_entries"] == 0
    assert stats["removed_entries"] == 0
    assert stats["added_round_trips"] == 0
    assert stats["removed_round_trips"] == 0
    assert stats["extra_fees"] == pytest.approx(0.0)
    assert stats["completed_delta"] == 0


def test_marginal_attributes_the_added_entries_and_round_trips(tmp_path):
    _write(tmp_path, "exp-ctrl", CONTROL_ROWS)
    _write(tmp_path, "exp-loose", VARIANT_ROWS)
    control = marginal_trades.load_trades(tmp_path / "results", "exp-ctrl")
    variant = marginal_trades.load_trades(tmp_path / "results", "exp-loose")
    stats = marginal_trades.marginal(control, variant)
    assert stats["added_entries"] == 2
    assert stats["added_entries_by_asset"] == {"crypto": 2}
    assert stats["removed_entries"] == 0
    assert stats["added_round_trips"] == 2
    assert stats["added_pnl"] == pytest.approx(-20.0)
    assert stats["added_mean"] == pytest.approx(-10.0)
    assert stats["added_by_asset"]["crypto"] == {"n": 2, "pnl": pytest.approx(-20.0)}
    assert stats["completed_delta"] == 2


def test_added_mean_is_reported_against_the_control_expectancy(tmp_path):
    _write(tmp_path, "exp-ctrl", CONTROL_ROWS)
    _write(tmp_path, "exp-loose", VARIANT_ROWS)
    control = marginal_trades.load_trades(tmp_path / "results", "exp-ctrl")
    variant = marginal_trades.load_trades(tmp_path / "results", "exp-loose")
    stats = marginal_trades.marginal(control, variant)
    # control trips: -20 and +40 -> expectancy $10/trade
    assert stats["control_expectancy"] == pytest.approx(10.0)
    assert stats["expectancy_multiple"] == pytest.approx(-1.0)
    report = marginal_trades.format_report("exp-ctrl", control, "exp-loose", stats)
    assert "expectancy $+10.00/trade" in report
    assert "-1.00x the control's expectancy" in report
    assert "below-average trades" in report


def test_extra_fees_and_dropped_trips_are_read_from_the_fills(tmp_path):
    # Variant drops the control's losing BTC trip and takes a smaller winner.
    rows = [
        _trade("2022-02-10", "AAPL", "stock", "buy"),
        _trade("2022-03-01", "AAPL", "stock", "sell", realized_pl=40.0),
        _trade("2022-05-01", "LINK/USD", "crypto", "buy", fee=3.0),
        _trade("2022-05-09", "LINK/USD", "crypto", "sell", fee=3.0, realized_pl=5.0),
    ]
    _write(tmp_path, "exp-ctrl", CONTROL_ROWS)
    _write(tmp_path, "exp-tight", rows)
    control = marginal_trades.load_trades(tmp_path / "results", "exp-ctrl")
    variant = marginal_trades.load_trades(tmp_path / "results", "exp-tight")
    stats = marginal_trades.marginal(control, variant)
    assert stats["removed_round_trips"] == 1
    assert stats["removed_pnl"] == pytest.approx(-20.0)
    assert stats["added_pnl"] == pytest.approx(5.0)
    assert stats["extra_fees"] == pytest.approx(6.0 - 3.0)  # variant fees - control fees
    assert stats["completed_delta"] == 0


def test_main_prints_one_block_per_variant(tmp_path, capsys):
    _write(tmp_path, "exp-ctrl", CONTROL_ROWS)
    _write(tmp_path, "exp-loose", VARIANT_ROWS)
    code = marginal_trades.main(
        [
            "exp-loose",
            "--control",
            "exp-ctrl",
            "--results-dir",
            str(tmp_path / "results"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("variant `") == 1
    assert "exp-ctrl" in out and "exp-loose" in out
