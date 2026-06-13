"""Thin wrapper around Alpaca's PAPER trading account.

Safety: ``paper=True`` is hardcoded on the client, so it can only ever reach
Alpaca's paper endpoint (https://paper-api.alpaca.markets) — fake money. There is
no code path to the live trading endpoint, and we never request withdrawal/funding
permissions. Credentials are read from the environment, never hardcoded.
"""

from __future__ import annotations

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest


class BrokerError(RuntimeError):
    """Raised for missing credentials or rejected paper orders."""


def _position_symbol(symbol: str) -> str:
    """Alpaca reports/closes positions without the slash (BTC/USD -> BTCUSD)."""
    return symbol.replace("/", "")


class PaperBroker:
    def __init__(self, api_key: str | None = None, secret_key: str | None = None):
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise BrokerError(
                "Alpaca paper keys not found. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY (see README / .env.example)."
            )
        # paper=True is intentional and not configurable — live trading is off.
        self.client = TradingClient(api_key, secret_key, paper=True)

    # --- reads -------------------------------------------------------------
    def account(self) -> dict:
        a = self.client.get_account()
        return {
            "equity": float(a.equity),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "portfolio_value": float(a.portfolio_value),
        }

    def positions(self) -> list[dict]:
        out = []
        for p in self.client.get_all_positions():
            out.append(
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry": float(p.avg_entry_price),
                    "current_price": float(p.current_price) if p.current_price else None,
                    "market_value": float(p.market_value) if p.market_value else None,
                    "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                    "unrealized_plpc": float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0.0,
                }
            )
        return out

    def is_holding(self, symbol: str) -> bool:
        target = _position_symbol(symbol)
        return any(p.symbol == target for p in self.client.get_all_positions())

    def has_open_order(self, symbol: str) -> bool:
        """True if there's an unfilled order for this symbol (avoids stacking buys)."""
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
        orders = self.client.get_orders(req)
        return any(o.symbol in (symbol, _position_symbol(symbol)) for o in orders)

    def position_plpc(self, symbol: str) -> float | None:
        """Unrealized profit/loss for the position, in percent (e.g. -8.0), or None."""
        target = _position_symbol(symbol)
        for p in self.client.get_all_positions():
            if p.symbol == target:
                return float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0.0
        return None

    # --- writes (paper only) ----------------------------------------------
    def buy_notional(self, symbol: str, notional: float, asset: str) -> str:
        # Crypto must use GTC; equities use DAY.
        tif = TimeInForce.GTC if asset == "crypto" else TimeInForce.DAY
        req = MarketOrderRequest(
            symbol=symbol,
            notional=round(float(notional), 2),
            side=OrderSide.BUY,
            time_in_force=tif,
        )
        order = self.client.submit_order(req)
        return str(order.id)

    def close(self, symbol: str) -> str:
        order = self.client.close_position(_position_symbol(symbol))
        return str(order.id)
