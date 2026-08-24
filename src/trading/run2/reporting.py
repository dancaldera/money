"""Ledger-backed Run 2 metrics and human-readable report rendering."""

from __future__ import annotations

from decimal import Decimal
import json
from math import sqrt
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .config import RunConfig
from .ledger import RunLedger
from .portfolio import block_bootstrap_mean_ci, deflated_sharpe_probability


def _closed_trade_pnls(ledger: RunLedger, cfg: RunConfig, portfolio: str) -> list[float]:
    """Return fee-net P&L for completed symbol round trips.

    Multiple partial fills are one completed trade. Baseline fees are linked by
    broker order id when Alpaca supplies it; shadow costs use the pre-registered
    slippage/taker-fee assumptions.
    """
    fees_by_order: dict[str, Decimal] = {}
    if portfolio == "baseline":
        for fee in ledger.fees(cfg.run_id):
            order_id = str(fee["broker_order_id"] or "")
            if order_id:
                fees_by_order[order_id] = fees_by_order.get(order_id, Decimal(0)) + Decimal(
                    fee["amount"]
                )
    state: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    pnls: list[float] = []
    for fill in ledger.fills(cfg.run_id, portfolio):
        symbol = fill["symbol"]
        qty, price = Decimal(fill["qty"]), Decimal(fill["price"])
        held, cost, cycle_pl, cycle_fees = state.get(
            symbol, (Decimal(0), Decimal(0), Decimal(0), Decimal(0))
        )
        if portfolio == "baseline":
            cycle_fees += fees_by_order.pop(str(fill["broker_order_id"] or ""), Decimal(0))
        else:
            rate_bps = (
                cfg.execution.crypto_taker_fee_bps
                if fill["asset"] == "crypto"
                else cfg.execution.equity_slippage_bps
            )
            cycle_fees += qty * price * Decimal(str(rate_bps)) / Decimal(10_000)
        if fill["side"] == "buy":
            state[symbol] = (held + qty, cost + qty * price, cycle_pl, cycle_fees)
        elif held > 0:
            sold = min(held, qty)
            avg = cost / held
            cycle_pl += sold * (price - avg)
            remaining = held - sold
            if remaining <= Decimal("0.000000000001"):
                pnls.append(float(cycle_pl - cycle_fees))
                state[symbol] = (Decimal(0), Decimal(0), Decimal(0), Decimal(0))
            else:
                state[symbol] = (remaining, remaining * avg, cycle_pl, cycle_fees)
    return pnls


def collect_run_report(
    cfg: RunConfig,
    ledger: RunLedger,
    marks: Mapping[str, float] | None = None,
    benchmark: pd.Series | None = None,
) -> dict[str, Any]:
    run = ledger.assert_manifest(cfg)
    marks = marks or {}
    portfolios: dict[str, Any] = {}
    for portfolio in cfg.research.arms:
        positions = ledger.positions(cfg.run_id, portfolio)
        market_value = sum(float(p.qty) * marks.get(symbol, float(p.avg_entry)) for symbol, p in positions.items())
        pnls = _closed_trade_pnls(ledger, cfg, portfolio)
        gaps = ledger.fill_gaps(cfg.run_id, portfolio)
        history = ledger.equity_history(cfg.run_id, portfolio)
        equity = pd.Series(
            [float(r["equity"]) for r in history],
            index=pd.to_datetime([r["captured_at"] for r in history], utc=True),
            dtype=float,
        )
        returns = equity.pct_change().dropna()
        dd = equity / equity.cummax() - 1 if len(equity) else pd.Series(dtype=float)
        low, high = block_bootstrap_mean_ci(pd.Series(pnls), confidence=0.90)
        portfolios[portfolio] = {
            "open_positions": len(positions),
            "market_value": market_value,
            "completed_trades": len(pnls),
            "realized_expectancy": float(np.mean(pnls)) if pnls else 0.0,
            "expectancy_ci90": [low, high],
            "win_rate_pct": float(np.mean(np.array(pnls) > 0) * 100) if pnls else 0.0,
            "mean_fill_gap_pct": float(np.mean(gaps)) if gaps else 0.0,
            "max_adverse_fill_gap_pct": max([0.0, *gaps]),
            "equity": float(equity.iloc[-1]) if len(equity) else cfg.starting_equity,
            "return_pct": (float(equity.iloc[-1]) / cfg.starting_equity - 1) * 100 if len(equity) else 0.0,
            "max_drawdown_pct": float(dd.min() * 100) if len(dd) else 0.0,
            "sharpe": float(returns.mean() / returns.std(ddof=1) * sqrt(365)) if len(returns) > 1 and returns.std() else 0.0,
            "deflated_sharpe_probability": deflated_sharpe_probability(returns, len(cfg.research.arms)),
        }
    benchmark_stats = None
    if benchmark is not None and len(benchmark):
        benchmark = pd.Series(benchmark, dtype=float).dropna()
        dd = benchmark / benchmark.cummax() - 1
        benchmark_stats = {
            "equity": float(benchmark.iloc[-1]),
            "return_pct": (float(benchmark.iloc[-1]) / cfg.starting_equity - 1) * 100,
            "max_drawdown_pct": float(dd.min() * 100),
        }
    return {
        "run_id": cfg.run_id,
        "status": run["status"],
        "started_at": run["started_at"],
        "halt_reason": run["halt_reason"],
        "config_hash": cfg.fingerprint,
        "fees": float(ledger.total_fees(cfg.run_id)),
        "primary_benchmark": benchmark_stats,
        "portfolios": portfolios,
    }


def render_run_report(report: dict[str, Any]) -> str:
    out = [
        f"Run {report['run_id']} — {report['status']}",
        f"Started: {report['started_at']}",
        f"Manifest: {report['config_hash']}",
        f"Recorded fees: ${report['fees']:,.2f}",
    ]
    if report.get("halt_reason"):
        out.append(f"HALT: {report['halt_reason']}")
    if report.get("primary_benchmark"):
        b = report["primary_benchmark"]
        out.append(
            f"Primary benchmark: ${b['equity']:,.2f} ({b['return_pct']:+.2f}%), "
            f"maxDD={b['max_drawdown_pct']:.2f}%"
        )
    for name, p in report["portfolios"].items():
        out.extend(
            [
                "",
                f"[{name}] equity=${p['equity']:,.2f} return={p['return_pct']:+.2f}% "
                f"maxDD={p['max_drawdown_pct']:.2f}%",
                f"  positions={p['open_positions']} completed={p['completed_trades']} "
                f"win={p['win_rate_pct']:.1f}% expectancy=${p['realized_expectancy']:.2f}",
                f"  fill gap mean={p['mean_fill_gap_pct']:+.3f}% "
                f"max adverse={p['max_adverse_fill_gap_pct']:.3f}%",
                f"  Sharpe={p['sharpe']:.3f} DSR probability={p['deflated_sharpe_probability']:.3f}",
            ]
        )
    return "\n".join(out)
