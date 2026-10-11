"""Risk-based position sizing, shared by the replay and the live desk.

The frozen rule deploys one flat ``position_notional`` into every slot whatever
the asset is, so a $625 crypto slot carries roughly two to three times the daily
volatility of a $625 equity slot: the book's advertised cap (17 slots x $625) is
not the risk it actually runs, and the measured fee bill follows the same split
(crypto supplies 56% of trades and 86% of the fees).

The experiment-only ``position_vol_target_pct`` knob replaces the flat notional
with a constant-risk one:

    notional = position_notional * clip(target_vol / realized_vol, 0.25, 4)

so a high-volatility crypto entry gets a smaller slot and a calm equity entry a
larger one, at unchanged frozen caps. Realized volatility is the annualized
standard deviation of the last ``position_vol_window`` daily returns *up to and
including the signal bar* (no lookahead), and both legs annualize on 365 days —
the same basis the desk's return math uses, so the two legs are comparable.

Degenerate inputs never size *up*: a missing, short or zero-variance history
returns the flat ``position_notional`` instead of the clipped maximum. The knob
is pinned to ``None`` on every live run, so this module is inert there: it is
replay-only until a human opens a run that carries it.
"""

from __future__ import annotations

from math import isfinite, sqrt

import pandas as pd

from .config import RunConfig

# Bounds on the scale factor: without them a low-volatility symbol would take an
# arbitrarily large slot, and the "notional" would stop being a risk measure.
VOL_SCALE_MIN = 0.25
VOL_SCALE_MAX = 4.0

# Both legs annualize on the same basis as the desk's Sharpe: the portfolio is
# scored on 365 daily observations, not on a per-asset trading calendar.
ANNUALIZATION_DAYS = 365


def realized_vol_pct(closes: pd.Series, window: int) -> float | None:
    """Annualized realized volatility (%) over the last ``window`` returns.

    ``None`` means "not measurable" (too short a history, or a non-finite
    standard deviation) — the caller falls back to the flat notional.
    """
    series = pd.Series(closes, dtype="float64").dropna()
    if window < 2 or len(series) < window + 1:
        return None
    returns = series.pct_change().dropna().tail(window)
    std = float(returns.std(ddof=1))
    if not isfinite(std):
        return None
    return std * sqrt(ANNUALIZATION_DAYS) * 100


def entry_notional(cfg: RunConfig, closes: pd.Series | None = None) -> float:
    """Notional for one fresh entry: flat, or scaled to ``position_vol_target_pct``."""
    base = float(cfg.portfolio.position_notional)
    target = cfg.portfolio.position_vol_target_pct
    if target is None or closes is None:
        return base
    vol = realized_vol_pct(closes, cfg.portfolio.position_vol_window)
    if vol is None or vol <= 0:
        return base
    scale = min(max(target / vol, VOL_SCALE_MIN), VOL_SCALE_MAX)
    return base * scale
