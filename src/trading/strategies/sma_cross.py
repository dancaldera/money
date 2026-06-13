"""Assumption: buy when a fast SMA crosses above a slow SMA, exit on the reverse."""

from __future__ import annotations

from backtesting import Strategy
from backtesting.lib import crossover

from .base import sma


class SmaCross(Strategy):
    n1 = 10  # fast moving-average window
    n2 = 30  # slow moving-average window

    def init(self):
        self.sma1 = self.I(sma, self.data.Close, self.n1)
        self.sma2 = self.I(sma, self.data.Close, self.n2)

    def next(self):
        if crossover(self.sma1, self.sma2):
            self.position.close()
            self.buy()
        elif crossover(self.sma2, self.sma1):
            self.position.close()
