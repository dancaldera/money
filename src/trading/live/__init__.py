"""Live paper-trading layer (Alpaca paper account — fake money, never live)."""

from .broker import BrokerError, PaperBroker
from .signals import get_signal_fn
from .trader import evaluate, stop_breached

__all__ = ["PaperBroker", "BrokerError", "get_signal_fn", "evaluate", "stop_breached"]
