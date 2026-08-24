"""Deterministic portfolio checks for the frozen Run 2 manifest."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

import pandas as pd

from .config import RunConfig
from .ledger import Position, RunLedger


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reason: str


def _pending_buys(ledger: RunLedger, run_id: str, portfolio: str) -> list:
    return [
        d for d in ledger.decisions(run_id, portfolio)
        if d["status"] in {"pending", "submitted"} and d["action"] == "buy_intent"
    ]


def correlation_matches(
    candidate: str,
    held: Mapping[str, Position],
    bars: Mapping[str, pd.DataFrame],
    window: int,
    threshold: float,
) -> int:
    if candidate not in bars or candidate in held:
        return 0
    candidate_returns = bars[candidate]["Close"].pct_change().dropna().tail(window)
    matches = 0
    for symbol in held:
        if symbol not in bars:
            continue
        other = bars[symbol]["Close"].pct_change().dropna().tail(window)
        aligned = pd.concat([candidate_returns, other], axis=1, join="inner").dropna()
        if len(aligned) < max(20, window // 2):
            continue
        corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
        if pd.notna(corr) and corr >= threshold:
            matches += 1
    return matches


def check_entry(
    cfg: RunConfig,
    ledger: RunLedger,
    portfolio: str,
    symbol: str,
    asset: str,
    bars: Mapping[str, pd.DataFrame] | None = None,
) -> RiskResult:
    if ledger.is_halted(cfg.run_id):
        return RiskResult(False, "run_halted")

    positions = ledger.positions(cfg.run_id, portfolio)
    if symbol in positions:
        return RiskResult(False, "already_holding")
    pending = _pending_buys(ledger, cfg.run_id, portfolio)
    if any(p["symbol"] == symbol for p in pending):
        return RiskResult(False, "buy_already_pending")

    notional = Decimal(str(cfg.portfolio.position_notional))
    position_count = len(positions) + len(pending)
    gross = sum((p.cost_basis for p in positions.values()), Decimal(0))
    gross += sum((Decimal(str(p["notional"])) for p in pending), Decimal(0))
    if position_count >= cfg.portfolio.max_positions:
        return RiskResult(False, "max_positions")
    if gross + notional > Decimal(str(cfg.portfolio.max_gross_exposure)):
        return RiskResult(False, "max_gross_exposure")

    same_positions = [p for p in positions.values() if p.asset == asset]
    same_pending = [p for p in pending if p["asset"] == asset]
    same_count = len(same_positions) + len(same_pending)
    same_exposure = sum((p.cost_basis for p in same_positions), Decimal(0))
    same_exposure += sum((Decimal(str(p["notional"])) for p in same_pending), Decimal(0))
    if asset == "crypto":
        if same_count >= cfg.portfolio.max_crypto_positions:
            return RiskResult(False, "max_crypto_positions")
        if same_exposure + notional > Decimal(str(cfg.portfolio.max_crypto_exposure)):
            return RiskResult(False, "max_crypto_exposure")
    else:
        if same_count >= cfg.portfolio.max_stock_positions:
            return RiskResult(False, "max_stock_positions")
        if same_exposure + notional > Decimal(str(cfg.portfolio.max_stock_exposure)):
            return RiskResult(False, "max_stock_exposure")

    if bars:
        matches = correlation_matches(
            symbol,
            positions,
            bars,
            cfg.portfolio.correlation_window,
            cfg.portfolio.correlation_threshold,
        )
        if matches > cfg.portfolio.correlation_matches_allowed:
            return RiskResult(False, f"correlation_cap:{matches}")
    return RiskResult(True, "allowed")


def drawdown_pct(equity: float, high_water: float) -> float:
    if high_water <= 0:
        return 0.0
    return max(0.0, (high_water - equity) / high_water * 100)

