"""Assumption: buy when a fast SMA crosses above a slow SMA, exit on the reverse."""

from __future__ import annotations

from backtesting import Strategy
from .base import sma_cross_signal


class SmaCross(Strategy):
    n1 = 10  # fast moving-average window
    n2 = 30  # slow moving-average window

    def init(self):
        # Indicators are evaluated by the shared pure signal function in next().
        pass

    def next(self):
        signal = sma_cross_signal(self.data.Close, self.n1, self.n2)
        if signal == "BUY":
            self.position.close()
            self.buy()
        elif signal == "SELL":
            self.position.close()
