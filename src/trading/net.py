"""Bounded HTTP for the Alpaca clients — never call one without a timeout.

``requests`` has no default timeout, so a half-open TCP connection blocks the
caller forever, and alpaca-py never passes one (its ``RESTClient`` builds a bare
``requests.Session`` and calls ``session.request(method, url, **opts)``).

Measured cost of leaving it unbounded, 2026-10-07 04:47 CST: the 30-min stop
monitor hung inside a broker call past the job's 3600s watchdog, and the next
tick only ran six hours later — ~12 missed ticks with every open position
unenforced and no alert (the run was recorded as a plain timeout). The daily
scan has the same shape and a worse ending: it only ever evaluates the newest
closed bar, so a hung data fetch loses that bar's signal permanently.
"""

from __future__ import annotations

from typing import Any

import requests

# (connect, read) in seconds. Requests bounds the read as the gap between bytes
# received, not the whole response, so a large bar frame is unaffected; ~5s for
# connect sits just above the TCP retransmission window, so a retransmit can
# still save one transient blip before the call is declared dead.
HTTP_TIMEOUT: tuple[float, float] = (5.0, 20.0)

_BOUND_MARK = "_money_http_timeout"


def bound_http_timeout(client: Any, timeout: tuple[float, float] = HTTP_TIMEOUT) -> Any:
    """Install a default request timeout on an alpaca-py client, in place.

    Wrapping the session's bound ``request`` preserves whatever else the client
    configured on that session (adapters, headers) and still lets an explicit
    per-call timeout win. Returns the client so a construction site can chain.
    Idempotent: a client that is already bounded keeps its first timeout.
    """
    session = getattr(client, "_session", None)
    if not isinstance(session, requests.Session) or hasattr(session, _BOUND_MARK):
        return client
    original = session.request

    def request(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout)
        return original(method, url, **kwargs)

    session.request = request  # type: ignore[method-assign]
    setattr(session, _BOUND_MARK, timeout)
    return client
