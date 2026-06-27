"""Parquet caching so repeated backtests don't refetch the same candles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pandas as pd

# Project root is three levels up from this file: src/trading/data/cache.py
DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def cache_path(key: str) -> Path:
    return DATA_DIR / f"{_safe(key)}.parquet"


def load_or_fetch(
    key: str,
    fetch_fn: Callable[[], pd.DataFrame],
    refresh: bool = False,
) -> pd.DataFrame:
    """Return cached candles for ``key``, fetching and caching on a miss.

    ``fetch_fn`` is a zero-arg callable returning the DataFrame (e.g. a lambda
    wrapping ``fetch_crypto``/``fetch_equity``). Pass ``refresh=True`` to bypass
    the cache and overwrite it.
    """
    path = cache_path(key)
    if path.exists() and not refresh:
        return pd.read_parquet(path)

    df = fetch_fn()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df
