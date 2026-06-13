"""Reconcile a strategy signal with the current paper position and act on it."""

from __future__ import annotations

import pandas as pd

from .broker import PaperBroker
from .signals import get_signal_fn


def evaluate(
    broker: PaperBroker,
    symbol: str,
    asset: str,
    strategy_name: str,
    bars: pd.DataFrame,
    notional: float,
    stop_loss_pct: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Compute the live signal for ``symbol`` and place a paper order if warranted.

    Returns a dict describing what happened. Actions, in priority order:
      - holding + loss exceeds ``stop_loss_pct`` -> close the position ("stopped")
      - BUY  + not holding & no open order        -> notional market buy ("bought")
      - SELL + holding                            -> close the position ("closed")
      - otherwise                                 -> "none"
    The stop-loss overrides the strategy signal, so a falling position is cut even
    while RSI says "still oversold". With ``dry_run=True`` no order is sent.
    """
    signal = get_signal_fn(strategy_name)(bars)
    holding = broker.is_holding(symbol)
    # A pending (unfilled) buy counts as "already in" so we don't stack orders
    # across runs while an order waits to fill (e.g. a stock order over a weekend).
    pending = broker.has_open_order(symbol)

    plpc = broker.position_plpc(symbol) if holding else None
    stop_hit = bool(
        holding and stop_loss_pct and plpc is not None and plpc <= -abs(stop_loss_pct)
    )

    action, order_id = "none", None
    if stop_hit:
        action = "would_stop" if dry_run else "stopped"
        if not dry_run:
            order_id = broker.close(symbol)
    elif signal == "BUY" and not holding and not pending:
        action = "would_buy" if dry_run else "bought"
        if not dry_run:
            order_id = broker.buy_notional(symbol, notional, asset)
    elif signal == "SELL" and holding:
        action = "would_close" if dry_run else "closed"
        if not dry_run:
            order_id = broker.close(symbol)

    return {
        "symbol": symbol,
        "asset": asset,
        "strategy": strategy_name,
        "signal": signal,
        "holding": holding,
        "pending": pending,
        "plpc": round(plpc, 2) if plpc is not None else None,
        "action": action,
        "order_id": order_id,
        "last_price": round(float(bars["Close"].iloc[-1]), 2),
    }
