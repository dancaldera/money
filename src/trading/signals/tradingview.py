"""Wrap tradingview-ta to fetch TradingView's aggregated BUY/SELL/NEUTRAL signal.

This is the closest free stand-in for a "TradingView MCP": it returns the same
technical-analysis recommendation you see on a TradingView chart, computed from
TradingView's bundle of indicators. It is read-only market intelligence — no
account, no orders.
"""

from __future__ import annotations

from tradingview_ta import TA_Handler

# tradingview-ta interval strings.
_INTERVALS = {
    "1d": "1d",
    "1h": "1h",
    "4h": "4h",
    "1w": "1W",
}


def get_signal(
    symbol: str,
    asset: str,
    interval: str = "1d",
    exchange: str | None = None,
) -> dict:
    """Return TradingView's recommendation summary for ``symbol``.

    ``asset`` is "crypto" or "stock". For crypto, ``symbol`` should be the
    exchange ticker without the slash (e.g. "BTCUSD"); a "BTC/USD" form is
    accepted and normalized.
    """
    if asset == "crypto":
        screener = "crypto"
        ex = (exchange or "BINANCE").upper()
        tv_symbol = symbol.replace("/", "").upper()
    else:
        screener = "america"
        ex = (exchange or "NASDAQ").upper()
        tv_symbol = symbol.upper()

    handler = TA_Handler(
        symbol=tv_symbol,
        screener=screener,
        exchange=ex,
        interval=_INTERVALS.get(interval, "1d"),
    )
    summary = handler.get_analysis().summary
    return {
        "symbol": tv_symbol,
        "exchange": ex,
        "recommendation": summary["RECOMMENDATION"],
        "buy": summary["BUY"],
        "sell": summary["SELL"],
        "neutral": summary["NEUTRAL"],
    }
