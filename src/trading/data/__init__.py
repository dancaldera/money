"""Market-data fetchers with on-disk caching."""

from .crypto import fetch_crypto
from .equities import fetch_equity
from .cache import load_or_fetch

__all__ = ["fetch_crypto", "fetch_equity", "load_or_fetch"]
