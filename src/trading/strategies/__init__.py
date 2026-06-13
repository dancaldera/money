"""Strategy registry.

Add a new strategy by importing its class and registering it under a short name
in ``STRATEGIES``; the CLI looks strategies up by that name.
"""

from .rsi_meanrev import RsiMeanReversion
from .sma_cross import SmaCross

STRATEGIES = {
    "sma_cross": SmaCross,
    "rsi_meanrev": RsiMeanReversion,
}


def get_strategy(name: str):
    try:
        return STRATEGIES[name]
    except KeyError:
        raise SystemExit(
            f"Unknown strategy '{name}'. Available: {', '.join(sorted(STRATEGIES))}"
        )


__all__ = ["STRATEGIES", "get_strategy", "SmaCross", "RsiMeanReversion"]
