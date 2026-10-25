"""Deployment lens on the live run: how much money is the desk actually risking?

``money run-report`` answers the statistical question (expectancy, Sharpe, DSR).
This script answers the *capital* question, which is the one that prices a size
step and the one a daily read never shows:

1. **Deployment** — open positions against the frozen caps, gross exposure as a
   share of the cap and of equity, and how many dollars a fresh cross could
   still deploy. A desk at 7/17 slots and 4% of equity is not "up 0.3%": it is a
   book whose realized move is diluted by undeployed capital, and that ratio is
   what a size decision has to move.
2. **Attribution** — per position: qty, average entry, mark, market value,
   unrealised dollars and percent, plus the realized round-trip stats reused
   from ``collect_run_report`` (single source of truth for trade P&L).
3. **Scan census** — what the guards did with the fresh crosses the strategies
   produced: intents allowed, blocked by which guard, and how the intents ended
   (filled / pending / expired). This is the live evidence for whether a cap or
   a rule is what is holding the book back.
4. **Sizing arithmetic** — the live dollar move multiplied by a notional factor.
   Arithmetic on *realised* live P&L, never a forecast: the measured ladder in
   ``docs/experiments.md`` is what prices a real step up, and changing the size
   of the live book is a human decision (new run_id + reviewed registry entry).
5. **Round trips and replay parity** — the closed trades by leg and by symbol
   (gross, fees, net), then the live expectancy against the per-trade
   distribution of a replayed control: the z of the gap, the exact binomial tail
   of the win count, and how many trades the observed gap would need to reach
   2 sigma. This is the instrument that keeps a five-week record from being read
   as a broken strategy — every lever on this desk was measured closed, so the
   honest response to a short shortfall is patience, and this line says how much.

Read-only, and no broker or network call: marks come from the newest recorded
decision per symbol, i.e. the last closed bar's close the desk itself acted on.
A symbol with no recorded bar is priced at its average entry and flagged
``cost`` so a missing mark can never masquerade as a flat position.

Run:  .venv/bin/python scripts/analysis_live_deployment.py \
          --ledger results/run3/ledger.sqlite --run-config config/run3.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_LEDGER = Path("results/run3/ledger.sqlite")
DEFAULT_RUN_CONFIG = Path("config/run3.yaml")
# The 1x control replay: the reference distribution of what one of this desk's
# trades pays. Any replayed run works; this one is the frozen-basis control.
DEFAULT_REPLAY_TRADES = Path("results/exp-slots-all-recover/portfolio-backtest/trades.csv")
SCALE_FACTORS = (1.0, 2.0, 3.0)
# 90% one-sided: below this the live gap is inside the replay's own noise.
Z_ALERT = 1.645


def latest_marks(decisions: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Newest recorded ``signal_price`` per symbol (the last closed bar's close).

    Decisions are appended in scan order, so the last row wins; ties on
    ``bar_end`` (re-runs of the same bar) keep the newest ``decided_at``.
    """
    best: dict[str, tuple[str, str, float]] = {}
    for row in decisions:
        symbol = str(row["symbol"])
        try:
            price = float(row["signal_price"])
        except (TypeError, ValueError):
            continue
        stamp = (str(row.get("bar_end") or ""), str(row.get("decided_at") or ""))
        current = best.get(symbol)
        if current is None or stamp >= (current[0], current[1]):
            best[symbol] = (stamp[0], stamp[1], price)
    return {symbol: price for symbol, (_, _, price) in best.items()}


def position_rows(
    positions: Mapping[str, Any], marks: Mapping[str, float]
) -> list[dict[str, Any]]:
    """Per-position market value and unrealised P&L, marked to the last close.

    ``positions`` maps symbol -> the ledger's ``Position`` (qty/avg_entry/asset).
    Unrealised percent is measured against the average entry, so it is the move
    the desk actually captured, not a window return.
    """
    rows: list[dict[str, Any]] = []
    for symbol, pos in positions.items():
        qty, avg = float(pos.qty), float(pos.avg_entry)
        mark = marks.get(symbol)
        source = "mark" if mark else "cost"
        price = float(mark) if mark else avg
        rows.append(
            {
                "symbol": symbol,
                "asset": str(pos.asset),
                "qty": qty,
                "avg_entry": avg,
                "mark": price,
                "mark_source": source,
                "market_value": qty * price,
                "cost_basis": qty * avg,
                "unrealized_pl": qty * (price - avg),
                "unrealized_pct": ((price / avg - 1) * 100) if avg else 0.0,
            }
        )
    return sorted(rows, key=lambda row: (-row["market_value"], row["symbol"]))


def deployment(
    rows: Sequence[Mapping[str, Any]], equity: float, portfolio: Any
) -> dict[str, Any]:
    """Cap utilisation: slots, gross exposure and per-asset limits.

    ``room_dollars`` is the smaller of the gross and slot headroom, i.e. what one
    more $ ``position_notional`` entry could actually deploy.
    """
    gross = sum(float(row["market_value"]) for row in rows)
    max_gross = float(portfolio.max_gross_exposure)
    max_positions = int(portfolio.max_positions)
    notional = float(portfolio.position_notional)
    per_asset: dict[str, Any] = {}
    for asset, max_count, max_exposure in (
        ("crypto", portfolio.max_crypto_positions, portfolio.max_crypto_exposure),
        ("stock", portfolio.max_stock_positions, portfolio.max_stock_exposure),
    ):
        selected = [row for row in rows if row["asset"] == asset]
        exposure = sum(float(row["market_value"]) for row in selected)
        per_asset[asset] = {
            "positions": len(selected),
            "max_positions": int(max_count),
            "exposure": exposure,
            "max_exposure": float(max_exposure),
        }
    slot_room = max(0.0, (max_positions - len(rows)) * notional)
    gross_room = max(0.0, max_gross - gross)
    return {
        "equity": float(equity),
        "positions": len(rows),
        "max_positions": max_positions,
        "slot_utilization_pct": (100 * len(rows) / max_positions) if max_positions else 0.0,
        "gross_exposure": gross,
        "max_gross_exposure": max_gross,
        "gross_utilization_pct": (100 * gross / max_gross) if max_gross else 0.0,
        "deployment_pct_of_equity": (100 * gross / equity) if equity else 0.0,
        "slot_room_notional": slot_room,
        "gross_room": gross_room,
        "room_dollars": min(slot_room, gross_room),
        "per_asset": per_asset,
    }


def intent_census(decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """What happened to the fresh crosses of one portfolio.

    ``new_entries_eligible`` counts BUY signals on a symbol the portfolio did not
    hold: the only crosses the guards had to rule on. A BUY signal while already
    holding is a no-op (``holding``), never a blocked entry. Exits are counted
    separately because a stop is booked under its own ``STOP`` signal name, not
    as a reverse cross.
    """
    buy_signals = holding = 0
    entry_intents: list[Mapping[str, Any]] = []
    blocked: Counter[str] = Counter()
    exit_signals: Counter[str] = Counter()
    exit_intents: Counter[str] = Counter()
    exit_status: Counter[str] = Counter()
    for row in decisions:
        signal, action = str(row["signal"]), str(row["action"])
        reason = str(row.get("reason") or "")
        if action == "buy_intent":
            entry_intents.append(row)
        if signal == "BUY":
            buy_signals += 1
            if action != "buy_intent":
                if reason == "holding":
                    holding += 1
                else:
                    blocked[reason or "unexplained"] += 1
        if signal in {"SELL", "STOP"}:
            exit_signals[signal] += 1
        if action == "sell_intent":
            exit_intents[signal] += 1
            exit_status[str(row.get("status") or "")] += 1
    statuses = Counter(str(row.get("status") or "") for row in entry_intents)
    expired = Counter(
        str(row.get("reason") or "unexplained")
        for row in entry_intents
        if str(row.get("status") or "") == "expired"
    )
    return {
        "buy_signals": buy_signals,
        "buy_signals_while_holding": holding,
        "new_entries_eligible": len(entry_intents) + sum(blocked.values()),
        "intents": len(entry_intents),
        "blocked": dict(blocked),
        "entry_status": dict(statuses),
        "expired_reasons": dict(expired),
        "pending": statuses.get("pending", 0),
        "exit_signals": dict(exit_signals),
        "exit_intents": dict(exit_intents),
        "exit_status": dict(exit_status),
    }


def scaling_arithmetic(
    pnl_dollars: float, equity: float, factors: Sequence[float] = SCALE_FACTORS
) -> list[dict[str, float]]:
    """Multiply a realised live dollar move by a notional factor.

    Pure arithmetic, deliberately: it answers "what share of equity did this move
    buy at 1x", so a size proposal can be quoted in the same units as the
    measured ladder. It does not re-simulate entries, so it is not a forecast.
    """
    return [
        {
            "factor": float(factor),
            "dollars": pnl_dollars * float(factor),
            "pct_of_equity": (100 * pnl_dollars * float(factor) / equity) if equity else 0.0,
        }
        for factor in factors
    ]


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def distribution(pnls: Sequence[float]) -> dict[str, float]:
    """Mean, sample stdev and win rate of a per-trade P&L series.

    Sample (n-1) stdev on purpose: this is used as the *reference* dispersion a
    short live record is judged against, never to describe the whole population.
    """
    values = [float(value) for value in pnls]
    n = len(values)
    if not n:
        return {"n": 0, "mean": 0.0, "stdev": 0.0, "win_rate_pct": 0.0}
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / (n - 1) if n > 1 else 0.0
    return {
        "n": n,
        "mean": mean,
        "stdev": math.sqrt(variance),
        "win_rate_pct": 100 * sum(1 for value in values if value > 0) / n,
    }


def realized_census(trades: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Where the closed trades' money went, by leg and by symbol.

    ``trades`` are the rows of ``trading.run2.reporting.closed_trades`` for ONE
    portfolio. That filter is load-bearing: shadow portfolios write to the same
    ``fills`` table under their own ``portfolio`` value, so summing the table
    unfiltered doubles the trade count and manufactures round trips that never
    happened (DOGE/USD read as a -6,077 unit position before the filter).
    """
    legs: dict[str, dict[str, float]] = {}
    symbols: dict[str, dict[str, float]] = {}
    for row in trades:
        net = float(row["pnl"])
        for bucket, key in ((legs, str(row["asset"])), (symbols, str(row["symbol"]))):
            item = bucket.setdefault(
                key, {"trips": 0, "wins": 0, "gross": 0.0, "fees": 0.0, "net": 0.0}
            )
            item["trips"] += 1
            item["wins"] += 1 if net > 0 else 0
            item["gross"] += float(row["gross"])
            item["fees"] += float(row["fees"])
            item["net"] += net
    return {
        "trips": sum(item["trips"] for item in symbols.values()),
        "gross": sum(item["gross"] for item in symbols.values()),
        "fees": sum(item["fees"] for item in symbols.values()),
        "net": sum(item["net"] for item in symbols.values()),
        "legs": dict(sorted(legs.items())),
        "symbols": dict(sorted(symbols.items(), key=lambda kv: (-kv[1]["net"], kv[0]))),
    }


def replay_reference(path: str | Path) -> dict[str, Any] | None:
    """Per-trade P&L distribution of a ``portfolio-backtest`` artifact.

    Reads the ``trades.csv`` a replay writes (one row per fill, ``realized_pl``
    is the fee-net P&L booked on the closing side) and keeps the closing rows:
    the opens carry a 0 that would otherwise drag the reference mean toward
    zero. Returns None when the artifact is absent or too short to have a
    dispersion, so the report can say "no reference" instead of inventing one.
    """
    artifact = Path(path)
    if not artifact.exists():
        return None
    pnls: list[float] = []
    with artifact.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("side") or "") != "sell":
                continue
            try:
                pnls.append(float(row["realized_pl"]))
            except (KeyError, TypeError, ValueError):
                continue
    if len(pnls) < 2:
        return None
    return {"path": str(artifact), "run": artifact.parent.parent.name, **distribution(pnls)}


def parity(
    live_pnls: Sequence[float], reference: Mapping[str, Any] | None, equity: float
) -> dict[str, Any]:
    """Is the live record distinguishable from the replay's, or is it noise?

    The only question a five-week paper run can answer. ``z`` is one-sample
    against the *replay's* per-trade stdev (a live sample of a few trades cannot
    estimate its own), ``n_for_2sigma`` the trade count this observed gap would
    need to reach 2 sigma — i.e. how much patience the desk's own edge needs
    before a shortfall can be read as a defect — and ``win_p_value`` the exact
    binomial tail ``P(wins <= observed | replay win rate)``. None of it is a
    forecast, and it never justifies a parameter change on its own: every lever
    this desk has was measured closed, so the honest use of a short record is to
    rule out "broken" and wait.
    """
    live = distribution(live_pnls)
    out: dict[str, Any] = {
        "live": live,
        "reference": dict(reference) if reference else None,
        "gap_per_trade": None,
        "z": None,
        "n_for_2sigma": None,
        "win_p_value": None,
        "shortfall_dollars": None,
        "inside_noise": None,
    }
    if reference is None or not live["n"]:
        return out
    gap = live["mean"] - float(reference["mean"])
    wins = sum(1 for value in live_pnls if float(value) > 0)
    rate = float(reference["win_rate_pct"]) / 100
    out.update(
        {
            "gap_per_trade": gap,
            "shortfall_dollars": gap * live["n"],
            "shortfall_pct_of_equity": (100 * gap * live["n"] / equity) if equity else 0.0,
            "win_p_value": sum(
                math.comb(int(live["n"]), k) * rate**k * (1 - rate) ** (int(live["n"]) - k)
                for k in range(wins + 1)
            ),
        }
    )
    sigma = float(reference["stdev"]) / math.sqrt(live["n"])
    if not sigma:
        # A reference with no dispersion cannot express a sigma: leave z None
        # rather than call a gap of any size "0 sigma, inside the noise".
        return out
    out.update(
        {
            "z": gap / sigma,
            "n_for_2sigma": (
                math.ceil((2 * float(reference["stdev"]) / -gap) ** 2)
                if gap < 0 and float(reference["stdev"]) > 0
                else None
            ),
            "inside_noise": abs(gap) < Z_ALERT * sigma,
        }
    )
    return out


def render(report: Mapping[str, Any]) -> str:
    dep, census = report["deployment"], report["census"]
    out = [
        f"{report['run_id']} live deployment — snapshot {report['snapshot_at']} "
        f"({report['snapshot_source']})",
        f"  status={report['status']} halted={'yes' if report['halted'] else 'no'}  "
        f"equity=${_fmt(dep['equity'])}  drawdown={report['snapshot_drawdown_pct']:.2f}% "
        f"(halt at {report['halt_pct']:.2f}%)",
        f"  gross exposure ${_fmt(dep['gross_exposure'])} = "
        f"{dep['deployment_pct_of_equity']:.2f}% of equity "
        f"(cap ${_fmt(dep['max_gross_exposure'])} -> {dep['gross_utilization_pct']:.1f}% used)",
        f"  slots {dep['positions']}/{dep['max_positions']} "
        f"({dep['slot_utilization_pct']:.1f}%)"
        + "".join(
            f"   {asset} {info['positions']}/{info['max_positions']}"
            for asset, info in sorted(dep["per_asset"].items())
        ),
        f"  room before a cap binds: ${_fmt(dep['room_dollars'])} "
        f"(gross room ${_fmt(dep['gross_room'])}, slot room ${_fmt(dep['slot_room_notional'])})",
        "",
        "positions (mark = newest recorded close; 'cost' = no recorded bar for that symbol)",
        "  symbol        asset   qty              avg_entry    mark         value        unreal$     unreal%",
    ]
    for row in report["positions"]:
        out.append(
            f"  {row['symbol']:<13} {row['asset']:<7} {row['qty']:<16.9g} "
            f"{_fmt(row['avg_entry'], 4):<12} {_fmt(row['mark'], 4) if row['mark_source'] == 'mark' else _fmt(row['mark'], 4) + '*':<12} "
            f"{_fmt(row['market_value']):>11} {row['unrealized_pl']:>+11.2f} {row['unrealized_pct']:>+9.2f}%"
        )
    totals = report["totals"]
    out += [
        f"  totals: value ${_fmt(totals['market_value'])} | unrealized "
        f"${totals['unrealized_pl']:+,.2f} ({totals['unrealized_pct']:+.2f}% on deployed, "
        f"{totals['unrealized_pct_of_equity']:+.2f}% of equity) | unmarked {totals['unmarked']}",
        "",
        f"realized [{report['portfolio']}]: completed {report['realized']['completed_trades']} "
        f"win {report['realized']['win_rate_pct']:.1f}% "
        f"expectancy ${report['realized']['realized_expectancy']:,.2f} | "
        f"return {report['realized']['return_pct']:+.2f}% maxDD {report['realized']['max_drawdown_pct']:.2f}% | "
        f"fees ${report['fees']:,.2f}",
        "",
        f"scan census [{report['portfolio']}]: {census['buy_signals']} buy signals "
        f"({census['buy_signals_while_holding']} while holding), "
        f"{census['new_entries_eligible']} new-entry eligible -> {census['intents']} intents, "
        f"{sum(census['blocked'].values())} blocked",
    ]
    out.append(
        "  blocked by guard: "
        + (", ".join(f"{k} x{v}" for k, v in sorted(census["blocked"].items())) or "none")
    )
    exit_signals = ", ".join(f"{k} {v}" for k, v in sorted(census["exit_signals"].items())) or "none"
    exit_intents = ", ".join(f"{k} {v}" for k, v in sorted(census["exit_intents"].items())) or "none"
    out.append(
        f"  exits: signals {exit_signals} -> intents {exit_intents} ("
        + (", ".join(f"{k} {v}" for k, v in sorted(census["exit_status"].items())) or "none")
        + ")"
    )
    out.append(
        "  entry intents: "
        + (", ".join(f"{k} {v}" for k, v in sorted(census["entry_status"].items())) or "none")
        + (f" | expired reasons: {census['expired_reasons']}" if census["expired_reasons"] else "")
        + f" | sessions recorded {report['sessions']}"
    )
    out += [
        "",
        "sizing arithmetic (live move x notional factor; arithmetic, not a forecast):",
    ]
    for item in report["scaling"]:
        out.append(
            f"  {item['factor']:.1f}x  ${item['dollars']:+,.2f}  "
            f"{item['pct_of_equity']:+.2f}% of equity"
        )
    out.append(
        "  What prices a real step up is the measured ladder (docs/experiments.md); "
        "the size of the live book is a human decision."
    )
    out += round_trip_lines(report.get("round_trips"))
    out += parity_lines(report.get("parity"))
    return "\n".join(out)


def round_trip_lines(census: Mapping[str, Any] | None) -> list[str]:
    """Closed-trade attribution: which leg and which symbol paid, net of fees."""
    if not census or not census.get("trips"):
        return []
    legs = " | ".join(
        f"{leg} {int(item['trips'])} trips {int(item['wins'])} win net ${item['net']:+,.2f}"
        for leg, item in census["legs"].items()
    )
    by_symbol = "  ".join(
        f"{symbol} ${item['net']:+,.2f} ({int(item['wins'])}/{int(item['trips'])})"
        for symbol, item in census["symbols"].items()
    )
    return [
        "",
        f"closed round trips [{census.get('portfolio', 'baseline')}]: "
        f"{int(census['trips'])} trips, gross ${census['gross']:+,.2f}, "
        f"fees ${census['fees']:,.2f}"
        + (
            f" of ${census['fees_booked']:,.2f} booked"
            if census.get("fees_booked") is not None
            else ""
        )
        + f", net ${census['net']:+,.2f}",
        f"  by leg: {legs}",
        f"  by symbol: {by_symbol}",
    ]


def parity_lines(result: Mapping[str, Any] | None) -> list[str]:
    """The line a short live record earns: noise, or a defect worth chasing."""
    if not result:
        return []
    reference = result.get("reference")
    if not reference:
        return [
            "",
            "replay parity: no reference artifact (pass --replay-trades "
            "<run>/portfolio-backtest/trades.csv) — skipped",
        ]
    live = result["live"]
    head = (
        f"replay parity vs {reference['run']} ({int(reference['n'])} closed trips, "
        f"${reference['mean']:,.2f}/trade, sd ${reference['stdev']:,.2f}, "
        f"win {reference['win_rate_pct']:.1f}%):"
    )
    if not live["n"]:
        return ["", head, "  no live round trip yet — nothing to compare"]
    if result["z"] is None:
        return ["", head, "  the reference has no dispersion — comparison skipped"]
    verdict = (
        "inside the replay's noise — do not read it as a broken desk"
        if result["inside_noise"]
        else "outside the replay envelope — investigate, do not re-tune"
    )
    out = [
        "",
        head,
        f"  live {int(live['n'])} trips at ${live['mean']:,.2f}/trade -> gap "
        f"${result['gap_per_trade']:+,.2f}/trade = {result['z']:+.2f} sigma; "
        f"P(wins<={int(round(live['win_rate_pct'] * live['n'] / 100))} | replay rate) = "
        f"{result['win_p_value']:.2f}",
        f"  shortfall vs the replay's expectation ${result['shortfall_dollars']:+,.2f} "
        f"({result.get('shortfall_pct_of_equity', 0.0):+.2f}% of equity) — {verdict}",
    ]
    if result["n_for_2sigma"]:
        out.append(
            f"  at this rate the gap needs {int(result['n_for_2sigma'])} trades to reach "
            "2 sigma: the sample, not the strategy, is what is short"
        )
    return out


def build_report(
    ledger: Any, cfg: Any, portfolio: str, replay_trades: str | Path = DEFAULT_REPLAY_TRADES
) -> dict[str, Any]:
    from trading.run2.reporting import closed_trades, collect_run_report

    run = ledger.assert_manifest(cfg)
    positions = ledger.positions(cfg.run_id, portfolio)
    decisions = [dict(row) for row in ledger.decisions(cfg.run_id, portfolio)]
    marks = latest_marks(decisions)
    rows = position_rows(positions, marks)
    history = ledger.equity_history(cfg.run_id, portfolio)
    snapshot = history[-1] if history else None
    equity = float(snapshot["equity"]) if snapshot else float(run["starting_equity"])
    stats = collect_run_report(cfg, ledger, marks)["portfolios"][portfolio]
    trades = closed_trades(ledger, cfg, portfolio)
    census = realized_census(trades)
    census["portfolio"] = portfolio
    census["fees_booked"] = float(ledger.total_fees(cfg.run_id))
    market_value = sum(row["market_value"] for row in rows)
    unrealized = sum(row["unrealized_pl"] for row in rows)
    cost = sum(row["cost_basis"] for row in rows)
    return {
        "run_id": cfg.run_id,
        "portfolio": portfolio,
        "status": run["status"],
        "halted": bool(ledger.is_halted(cfg.run_id)),
        "snapshot_at": snapshot["captured_at"] if snapshot else "none",
        "snapshot_source": snapshot["source"] if snapshot else "no-snapshot",
        "snapshot_drawdown_pct": float(snapshot["drawdown_pct"]) if snapshot else 0.0,
        "halt_pct": float(cfg.portfolio.drawdown_halt_pct),
        "fees": float(ledger.total_fees(cfg.run_id)),
        "sessions": len({str(row.get("decided_at")) for row in decisions}),
        "positions": rows,
        "totals": {
            "market_value": market_value,
            "unrealized_pl": unrealized,
            "unrealized_pct": (100 * unrealized / cost) if cost else 0.0,
            "unrealized_pct_of_equity": (100 * unrealized / equity) if equity else 0.0,
            "unmarked": sum(1 for row in rows if row["mark_source"] != "mark"),
        },
        "deployment": deployment(rows, equity, cfg.portfolio),
        "census": intent_census(decisions),
        "round_trips": census,
        "parity": parity([row["pnl"] for row in trades], replay_reference(replay_trades), equity),
        "scaling": scaling_arithmetic(unrealized, equity),
        "realized": {
            key: stats[key]
            for key in (
                "completed_trades",
                "win_rate_pct",
                "realized_expectancy",
                "expectancy_ci90",
                "return_pct",
                "max_drawdown_pct",
                "sharpe",
            )
        },
        "marks": marks,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live-run deployment, attribution and scan census (read-only)."
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--run-config", default=str(DEFAULT_RUN_CONFIG))
    parser.add_argument("--portfolio", default="baseline")
    parser.add_argument(
        "--replay-trades",
        default=str(DEFAULT_REPLAY_TRADES),
        help="trades.csv of a replayed control run, the per-trade reference distribution",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from trading.run2.config import load_run_config
    from trading.run2.ledger import RunLedger

    cfg = load_run_config(args.run_config)
    ledger = RunLedger(args.ledger)
    try:
        report = build_report(ledger, cfg, args.portfolio, args.replay_trades)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
