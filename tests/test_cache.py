"""Tests for the on-disk cache: TTL expiry and offline fallback (no network)."""

from __future__ import annotations

import os
import time

import pandas as pd
import pytest

from trading.data import cache as cache_mod
from trading.data.cache import load_or_fetch


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "DATA_DIR", tmp_path)
    return tmp_path


def _df(v: float) -> pd.DataFrame:
    return pd.DataFrame({"Close": [v]})


def _age_file(key: str, hours: float) -> None:
    p = cache_mod.cache_path(key)
    old = time.time() - hours * 3600
    os.utime(p, (old, old))


def test_fetches_on_miss_then_serves_from_cache(tmp_cache):
    calls = []

    def fetch():
        calls.append(1)
        return _df(1)

    assert load_or_fetch("k", fetch)["Close"].iloc[0] == 1
    load_or_fetch("k", fetch)  # second call hits cache
    assert len(calls) == 1


def test_refresh_forces_refetch(tmp_cache):
    calls = []

    def fetch():
        calls.append(1)
        return _df(len(calls))

    load_or_fetch("k", fetch)
    load_or_fetch("k", fetch, refresh=True)
    assert len(calls) == 2


def test_fresh_cache_within_ttl_is_reused(tmp_cache):
    calls = []

    def fetch():
        calls.append(1)
        return _df(1)

    load_or_fetch("k", fetch)
    load_or_fetch("k", fetch, max_age_hours=24)
    assert len(calls) == 1


def test_stale_cache_is_refetched(tmp_cache):
    calls = []

    def fetch():
        calls.append(1)
        return _df(len(calls))

    load_or_fetch("k", fetch)
    _age_file("k", 48)  # older than the 24h TTL
    load_or_fetch("k", fetch, max_age_hours=24)
    assert len(calls) == 2


def test_falls_back_to_stale_cache_when_fetch_fails(tmp_cache):
    load_or_fetch("k", lambda: _df(1))
    _age_file("k", 48)

    def boom():
        raise RuntimeError("offline")

    # Refetch fails but a cache exists -> serve stale data instead of crashing.
    assert load_or_fetch("k", boom, max_age_hours=24)["Close"].iloc[0] == 1


def test_fetch_error_with_no_cache_raises(tmp_cache):
    def boom():
        raise RuntimeError("offline")

    with pytest.raises(RuntimeError):
        load_or_fetch("missing", boom)


def test_cache_file_role_splits_the_live_window_from_dead_keys():
    from datetime import date

    from trading.data.cache import cache_file_role, recent_window_start

    today = date(2026, 10, 15)
    assert recent_window_start(today=today) == "2025-08-27"
    assert recent_window_start(today=date(2026, 9, 30)) == "2025-07-28"
    # Same formula the desk writes, including a symbol that contains '_'.
    assert cache_file_role("alpaca_crypto_BTC_USD_1d_2025-08-27.parquet", today=today) == "live"
    assert cache_file_role("alpaca_stock_SPY_1d_2022-01-01.parquet", today=today) == "replay"
    assert cache_file_role("alpaca_stock_AAPL_1d_2025-08-07.parquet", today=today) == "legacy"
    assert cache_file_role("stale.parquet", today=today) == "other"
    # The live name rolls with the month, not the day.
    assert cache_file_role(
        "alpaca_stock_AAPL_1d_2025-08-27.parquet", today=date(2026, 9, 30)
    ) == "legacy"
