"""Assumption: buy when RSI is oversold, sell when it becomes overbought."""

from __future__ import annotations

from backtesting import Strategy

from .base import rsi


class RsiMeanReversion(Strategy):
    period = 14
    lower = 30  # oversold threshold -> enter long
    upper = 70  # overbought threshold -> exit
    stop_loss_pct = 8  # protective stop below entry (0 = off); mirrors live trading

    def init(self):
        self.rsi = self.I(rsi, self.data.Close, self.period)

    def next(self):
        if self.rsi[-1] < self.lower and not self.position:
            price = self.data.Close[-1]
            sl = price * (1 - self.stop_loss_pct / 100) if self.stop_loss_pct else None
            self.buy(sl=sl)
        elif self.rsi[-1] > self.upper and self.position:
            self.position.close()
