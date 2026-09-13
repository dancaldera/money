"""Scan coverage: daily bars the data has and the ledger never evaluated.

``paper-scan`` only ever evaluates the newest *complete* bar. A bar that is never
the newest one is therefore never evaluated at all, and the loss is permanent:
the frozen strategy re-enters on a fresh SMA cross only, so a cross that lands on
an unevaluated bar is gone for good. Two real ways to lose a bar silently:

* a wrapper run that fetches fresh data and then dies (or records nothing new)
  before the ledger advances — the desk keeps trading the older bar;
* a schedule step that walks over a crypto close (bars close at 00:00 UTC, so a
  run at 17:00 CST advances the data clock by two days and skips one bar).

Coverage compares the cached bars with the ledger's decisions and reports, per
watchlist scope:

* ``newest_closed``   — newest complete bar in the cached data (what the next scan uses)
* ``newest_recorded`` — newest bar the ledger holds a decision for
* ``behind``          — the scan has not recorded the newest closed bar yet: the
  action is still possible, so this is what alerts
* ``gaps``            — complete bars inside the recorded window with no decision
  (already-permanent losses, kept for the evidence trail)
* ``recent_gap``      — a gap inside the newest ``RECENT_BARS`` complete bars
  (a fresh loss; the health watchdog stays quiet about older ones so a permanent
  gap does not alert forever)

Everything here is read-only and offline: it reads the local parquet cache, never
the broker, the ledger writes or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from ..data.cache import DATA_DIR, _safe
from ..data.clean import drop_forming_bar

RECENT_BARS = 3


def bar_key(value: Any) -> pd.Timestamp:
    """Normalise a bar label to a naive UTC timestamp.

    Ledger ``bar_end`` values are ISO strings with a ``+00:00`` offset while
    frame indexes come back naive from parquet, so both sides are compared as
    naive UTC timestamps.
    """
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


@dataclass(frozen=True)
class SymbolCoverage:
    """Coverage of one symbol: which complete bars the ledger never evaluated."""

    symbol: str
    asset: str
    newest_closed: pd.Timestamp | None
    newest_recorded: pd.Timestamp | None
    gaps: tuple[pd.Timestamp, ...] = ()
    recent: tuple[pd.Timestamp, ...] = ()

    @property
    def behind(self) -> bool:
        """The ledger has not evaluated the newest complete bar yet."""
        if self.newest_closed is None:
            return False
        return self.newest_recorded is None or self.newest_recorded < self.newest_closed

    @property
    def recent_gap(self) -> bool:
        """A gap inside the newest bars: the loss is fresh, not ancient history."""
        return any(gap in self.recent for gap in self.gaps)


@dataclass(frozen=True)
class ScopeCoverage:
    """Aggregate coverage for one asset scope (``equity``/``crypto``)."""

    scope: str
    symbols: int
    newest_closed: pd.Timestamp | None
    newest_recorded: pd.Timestamp | None
    gaps: tuple[pd.Timestamp, ...] = ()
    recent: tuple[pd.Timestamp, ...] = ()

    @property
    def behind(self) -> bool:
        if self.newest_closed is None:
            return False
        return self.newest_recorded is None or self.newest_recorded < self.newest_closed

    @property
    def recent_gap(self) -> bool:
        return any(gap in self.recent for gap in self.gaps)

    def health_line(self) -> str:
        """Flat ``key: value`` line for ``money health`` (parsed by health_check.sh)."""
        state = "behind" if self.behind else ("ok" if not self.gaps else "gap")
        closed = _stamp(self.newest_closed)
        recorded = _stamp(self.newest_recorded)
        missing = ",".join(_stamp(g) for g in self.gaps) or "-"
        return (
            f"  coverage_{self.scope}: {state} newest_closed={closed} "
            f"newest_recorded={recorded} behind={int(self.behind)} gaps={len(self.gaps)} "
            f"recent_gaps={int(self.recent_gap)} missing={missing}"
        )


def _stamp(value: pd.Timestamp | None) -> str:
    return value.isoformat() if value is not None else "none"


def symbol_coverage(
    symbol: str,
    asset: str,
    bars: pd.DataFrame | None,
    recorded: Iterable[Any],
    *,
    recent_bars: int = RECENT_BARS,
) -> SymbolCoverage:
    """Compare the cached bars of one symbol with the bars it has decisions for."""
    if bars is None or bars.empty:
        closed: list[pd.Timestamp] = []
    else:
        closed = [bar_key(index) for index in bars.index]
    seen = {bar_key(value) for value in recorded}
    newest_closed = max(closed) if closed else None
    newest_recorded = max(seen) if seen else None
    recent = tuple(closed[-recent_bars:]) if closed else ()
    gaps: tuple[pd.Timestamp, ...] = ()
    if closed and seen:
        # Only bars from the first decision on are the run's responsibility: the
        # cache holds years of history the frozen run never had to evaluate.
        start = min(seen)
        gaps = tuple(b for b in closed if b >= start and b != start and b not in seen)
    return SymbolCoverage(
        symbol=symbol,
        asset=asset,
        newest_closed=newest_closed,
        newest_recorded=newest_recorded,
        gaps=gaps,
        recent=recent,
    )


def scope_coverage(symbols: Sequence[tuple[str, str]], per_symbol: Iterable[SymbolCoverage]) -> dict[str, ScopeCoverage]:
    """Collapse per-symbol coverage into one entry per asset scope.

    ``newest_recorded`` is the *oldest* per-symbol newest bar: if any symbol of a
    scope lagged, the scope lagged.
    """
    by_scope: dict[str, list[SymbolCoverage]] = {}
    scope_of = {symbol: asset for symbol, asset in symbols}
    for item in per_symbol:
        scope = "crypto" if scope_of.get(item.symbol, item.asset) == "crypto" else "equity"
        by_scope.setdefault(scope, []).append(item)

    out: dict[str, ScopeCoverage] = {}
    for scope, items in by_scope.items():
        closed = [c.newest_closed for c in items if c.newest_closed is not None]
        recorded = [c.newest_recorded for c in items if c.newest_recorded is not None]
        gaps = tuple(sorted({gap for c in items for gap in c.gaps}))
        recent = tuple(sorted({bar for c in items for bar in c.recent}))
        out[scope] = ScopeCoverage(
            scope=scope,
            symbols=len(items),
            newest_closed=max(closed) if closed else None,
            newest_recorded=min(recorded) if recorded else None,
            gaps=gaps,
            recent=recent,
        )
    return out


def cached_bars(
    symbols: Iterable[tuple[str, str]], data_dir: Path | str | None = None
) -> dict[str, pd.DataFrame]:
    """Newest cached daily bars per symbol, forming bar dropped. No network access."""
    directory = Path(data_dir) if data_dir is not None else DATA_DIR
    frames: dict[str, pd.DataFrame] = {}
    for symbol, asset in symbols:
        pattern = f"alpaca_{_safe(asset)}_{_safe(symbol)}_1d_*.parquet"
        files = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime)
        if not files:
            continue
        frames[symbol] = drop_forming_bar(pd.read_parquet(files[-1]), "1d")
    return frames


def coverage_report(
    symbols: Sequence[tuple[str, str]],
    bars: Mapping[str, pd.DataFrame],
    recorded: Mapping[str, Iterable[Any]],
) -> dict[str, ScopeCoverage]:
    """Per-scope coverage from cached bars plus the ledger's recorded bars."""
    per_symbol = [
        symbol_coverage(symbol, asset, bars.get(symbol), recorded.get(symbol, ()))
        for symbol, asset in symbols
    ]
    return scope_coverage(symbols, per_symbol)


def health_lines(
    symbols: Sequence[tuple[str, str]],
    bars: Mapping[str, pd.DataFrame],
    recorded: Mapping[str, Iterable[Any]],
) -> list[str]:
    """``money health`` lines for every scope, in a stable order."""
    report = coverage_report(symbols, bars, recorded)
    return [report[scope].health_line() for scope in sorted(report)]
