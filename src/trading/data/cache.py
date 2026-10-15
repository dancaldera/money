"""Parquet caching so repeated backtests don't refetch the same candles."""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

# Project root is three levels up from this file: src/trading/data/cache.py
DATA_DIR = Path(__file__).resolve().parents[3] / "data"

# Daily bars only change once a day, so a cache older than this is refetched.
DEFAULT_MAX_AGE_HOURS = 24.0

# Full-history key shared by experiments and ``_run2_bars(recent=False)``.
REPLAY_SINCE = "2022-01-01"
# Live window: ``days`` before the first of the current month. See cli.
RECENT_WINDOW_DAYS = 400

# alpaca_{stock|crypto}_{symbol}_1d_{YYYY-MM-DD} — symbol may contain '_'.
_ALPACA_DAILY_START = re.compile(
    r"^alpaca_(?:stock|crypto)_.+_1d_(\d{4}-\d{2}-\d{2})$"
)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def cache_path(key: str) -> Path:
    return DATA_DIR / f"{_safe(key)}.parquet"


def recent_window_start(days: int = RECENT_WINDOW_DAYS, today: date | None = None) -> str:
    """Start of the live bar window: ``days`` before the first of ``today``'s month.

    Stable for a whole calendar month, so a failed refetch still has yesterday's
    file to fall back on. ``cli._recent_window_start`` delegates here.
    """
    anchor = (today or date.today()).replace(day=1) - timedelta(days=days)
    return anchor.isoformat()


def cache_file_role(name: str, *, today: date | None = None) -> str:
    """Classify a cache filename: ``live``, ``replay``, ``legacy``, or ``other``.

    ``live`` is the month-stable window the desk refetches every run. ``replay``
    is the full-history key experiments share. ``legacy`` is an old per-day
    window key nothing reuses — the daily-changing ``today-400d`` key wrote one
    of these per symbol per day, and counting them as stale hid a genuinely
    stale live file inside a few hundred dead ones. ``other`` is anything that
    is not an alpaca daily cache (fixtures, other fetchers).
    """
    match = _ALPACA_DAILY_START.match(name.removesuffix(".parquet"))
    if not match:
        return "other"
    start = match.group(1)
    if start == REPLAY_SINCE:
        return "replay"
    if start == recent_window_start(today=today):
        return "live"
    return "legacy"


def _age_hours(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 3600.0


def load_or_fetch(
    key: str,
    fetch_fn: Callable[[], pd.DataFrame],
    refresh: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> pd.DataFrame:
    """Return cached candles for ``key``, refetching on a miss or when stale.

    ``fetch_fn`` is a zero-arg callable returning the DataFrame (e.g. a lambda
    wrapping ``fetch_crypto``/``fetch_equity``). The cache is reused only when it
    exists and is younger than ``max_age_hours``; pass ``refresh=True`` to force a
    refetch. If a refetch fails (e.g. offline) but a cache file exists, the stale
    cache is returned rather than raising — backtests still run on slightly old
    data instead of crashing.
    """
    path = cache_path(key)
    if path.exists() and not refresh and _age_hours(path) <= max_age_hours:
        return pd.read_parquet(path)

    try:
        df = fetch_fn()
    except Exception:
        if path.exists():
            return pd.read_parquet(path)  # fall back to stale cache when offline
        raise

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df
