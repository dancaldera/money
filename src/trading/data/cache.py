"""Parquet caching so repeated backtests don't refetch the same candles."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

import pandas as pd

# Project root is three levels up from this file: src/trading/data/cache.py
DATA_DIR = Path(__file__).resolve().parents[3] / "data"

# Daily bars only change once a day, so a cache older than this is refetched.
DEFAULT_MAX_AGE_HOURS = 24.0


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def cache_path(key: str) -> Path:
    return DATA_DIR / f"{_safe(key)}.parquet"


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
