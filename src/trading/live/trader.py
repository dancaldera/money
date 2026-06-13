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
    dry_run: bool = False,
) -> dict:
    """Compute the live signal for ``symbol`` and place a paper order if warranted.

    Returns a dict describing what happened. Actions:
      - BUY  + not holding -> submit a notional market buy ("bought")
      - SELL + holding     -> close the position ("closed")
      - otherwise          -> "none"
    With ``dry_run=True`` no order is sent; the intended action is reported instead.
    """
    signal = get_signal_fn(strategy_name)(bars)
    holding = broker.is_holding(symbol)

    action, order_id = "none", None
    if signal == "BUY" and not holding:
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
        "action": action,
        "order_id": order_id,
        "last_price": round(float(bars["Close"].iloc[-1]), 2),
    }
