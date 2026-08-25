"""Thin wrapper around Alpaca's PAPER trading account.

Safety: ``paper=True`` is hardcoded on the client, so it can only ever reach
Alpaca's paper endpoint (https://paper-api.alpaca.markets) — fake money. There is
no code path to the live trading endpoint, and we never request withdrawal/funding
permissions. Credentials are read from the environment, never hardcoded.
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest


class BrokerError(RuntimeError):
    """Raised for missing credentials or rejected paper orders."""


def _position_symbol(symbol: str) -> str:
    """Alpaca reports/closes positions without the slash (BTC/USD -> BTCUSD)."""
    return symbol.replace("/", "")


def quantize_limit_price(price: float, asset: str) -> float:
    """Snap a limit price to Alpaca's minimum increment.

    US equities follow SEC Rule 612 (the sub-penny rule): $0.01 at or above
    $1, $0.0001 below $1. Crypto keeps 8 decimals, matching the previous
    paper-order rounding.
    """
    value = Decimal(str(price))
    if asset == "crypto":
        quantum = Decimal("0.00000001")
    elif abs(value) < Decimal("1"):
        quantum = Decimal("0.0001")
    else:
        quantum = Decimal("0.01")
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


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
        self.stock_data = StockHistoricalDataClient(api_key, secret_key)
        self.crypto_data = CryptoHistoricalDataClient(api_key, secret_key)

    # --- reads -------------------------------------------------------------
    def account(self) -> dict:
        a = self.client.get_account()
        return {
            "id": str(a.id),
            "equity": float(a.equity),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "portfolio_value": float(a.portfolio_value),
        }

    def clock(self) -> dict[str, Any]:
        value = self.client.get_clock()
        return {
            "is_open": bool(value.is_open),
            "timestamp": value.timestamp.isoformat(),
            "next_open": value.next_open.isoformat(),
            "next_close": value.next_close.isoformat(),
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

    def latest_price(self, symbol: str, asset: str) -> float:
        """Latest paper-market reference price for a guarded entry."""
        if asset == "crypto":
            trades = self.crypto_data.get_crypto_latest_trade(
                CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            )
        else:
            trades = self.stock_data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
        trade = trades[symbol]
        return float(trade.price)

    def all_orders(self, after: datetime | None = None) -> list[dict[str, Any]]:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, after=after)
        return [self._order_dict(o) for o in self.client.get_orders(req)]

    def activities(self, activity_type: str, after: str | None = None) -> list[dict[str, Any]]:
        """Read account activities from Alpaca's paper endpoint.

        alpaca-py does not currently expose this Trading API endpoint as a
        first-class method, but its authenticated paper client supports the
        documented REST resource.
        """
        data: dict[str, Any] = {"direction": "asc", "page_size": 100}
        if after:
            # This endpoint documents `after` as a calendar date. Run
            # initialization also requires an empty account, so including the
            # whole starting day cannot import a legitimate pre-run fill.
            data["after"] = after[:10]
        activities: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        while True:
            result = self.client.get(f"/account/activities/{activity_type}", data)
            page = result if isinstance(result, list) else []
            activities.extend(page)
            if len(page) < data["page_size"]:
                break
            token = str(page[-1].get("id", ""))
            if not token or token in seen_tokens:
                break
            seen_tokens.add(token)
            data["page_token"] = token
        return activities

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

    def buy_limit(
        self,
        symbol: str,
        notional: float,
        asset: str,
        limit_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        """Submit a price-capped paper buy; no live endpoint is available."""
        # Notional equity orders are fractional, and Alpaca only accepts DAY
        # for those. Crypto keeps IOC so an unfilled gap-cap order does not rest.
        tif = TimeInForce.IOC if asset == "crypto" else TimeInForce.DAY
        req = LimitOrderRequest(
            symbol=symbol,
            notional=round(float(notional), 2),
            limit_price=quantize_limit_price(limit_price, asset),
            side=OrderSide.BUY,
            time_in_force=tif,
            client_order_id=client_order_id,
        )
        return self._order_dict(self.client.submit_order(req))

    def close(self, symbol: str) -> str:
        order = self.client.close_position(_position_symbol(symbol))
        return str(order.id)

    @staticmethod
    def _order_dict(order: Any) -> dict[str, Any]:
        return {
            "id": str(order.id),
            "client_order_id": str(order.client_order_id),
            "symbol": str(order.symbol),
            "status": str(getattr(order.status, "value", order.status)),
            "side": str(getattr(order.side, "value", order.side)),
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
            "filled_at": order.filled_at.isoformat() if order.filled_at else None,
            "filled_qty": float(order.filled_qty or 0),
            "filled_avg_price": float(order.filled_avg_price or 0),
        }
