"""Frozen, validated configuration for an auditable paper-trading run."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import yaml


class RunConfigError(ValueError):
    """Raised when a run manifest is unsafe or internally inconsistent."""


_RUN2_CRYPTO = (
    "BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "LINK/USD", "DOGE/USD", "AVAX/USD", "AAVE/USD"
)
_RUN2_STOCKS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX")

# The audited live paper run. Its manifest values are frozen and no other
# manifest may claim this run_id.
LIVE_RUN_ID = "run2"

# Live runs and their blessed parameter sets. Only these run_ids can load
# through a live command's strict loader, and each must match its entry exactly:
# opening a new live run is a human decision that lands here as a reviewed
# entry. After `run-init`, the ledger's stored config hash freezes the manifest
# for good (editing the file breaks every command).
_FROZEN_BASELINES: dict[str, dict[str, float | int | None]] = {
    "run2": {
        "starting_equity": 100_000,
        "halt_recovery_drawdown_pct": None,
        "halt_recovery_days": None,
        "fast_window": 10,
        "slow_window": 30,
        "stop_loss_pct": 8,
        "stop_breakeven_at_pct": None,
        "stop_trail_pct": None,
        "position_notional": 625,
        "max_positions": 8,
        "max_gross_exposure": 5_000,
        "max_crypto_positions": 4,
        "max_crypto_exposure": 2_500,
        "max_stock_positions": 6,
        "max_stock_exposure": 3_750,
        "correlation_window": 60,
        "correlation_threshold": 0.80,
        "correlation_matches_allowed": 1,
        "drawdown_halt_pct": 5,
        "stock_gap_limit_pct": 2,
        "crypto_gap_limit_pct": 3,
    },
    # Opened 2026-09-19 on the deployment-breadth measurement (docs/experiments.md):
    # same ~$10.6k gross, spent on 17 slots x $625 instead of 8 x $1,250, plus the
    # opt-in halt recovery as freeze insurance (verified bit-identical to the
    # latch on this path, exp-slots-all-recover).
    "run3": {
        "starting_equity": 100_000,
        "halt_recovery_drawdown_pct": 2.5,
        "halt_recovery_days": 20,
        "fast_window": 10,
        "slow_window": 30,
        "stop_loss_pct": 8,
        "stop_breakeven_at_pct": None,
        "stop_trail_pct": None,
        "position_notional": 625,
        "max_positions": 17,
        "max_gross_exposure": 10_625,
        "max_crypto_positions": 8,
        "max_crypto_exposure": 5_000,
        "max_stock_positions": 9,
        "max_stock_exposure": 5_625,
        "correlation_window": 60,
        "correlation_threshold": 0.80,
        "correlation_matches_allowed": 1,
        "drawdown_halt_pct": 5,
        "stock_gap_limit_pct": 2,
        "crypto_gap_limit_pct": 3,
    },
}
LIVE_RUN_IDS: tuple[str, ...] = tuple(_FROZEN_BASELINES)


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    fast_window: int
    slow_window: int
    timeframe: str
    stop_loss_pct: float
    require_fresh_cross_after_stop: bool
    # Experiment-only stop refinements (None = the frozen fixed fill-derived stop).
    # Pinned to None for every live run: a trailing/breakeven stop can only be
    # measured through portfolio-backtest until a human opens a run that carries it.
    stop_breakeven_at_pct: float | None = None
    stop_trail_pct: float | None = None


@dataclass(frozen=True)
class PortfolioConfig:
    position_notional: float
    max_positions: int
    max_gross_exposure: float
    max_crypto_positions: int
    max_crypto_exposure: float
    max_stock_positions: int
    max_stock_exposure: float
    correlation_window: int
    correlation_threshold: float
    correlation_matches_allowed: int
    drawdown_halt_pct: float
    # Optional drawdown recovery. Absent (None) keeps the one-way latch the
    # audited run uses: once halted by drawdown, entries never re-arm. When set,
    # a run halted *by drawdown* re-arms as soon as drawdown falls back to this
    # level, so a lost drawdown does not freeze the desk for good.
    halt_recovery_drawdown_pct: float | None = None
    # Calendar-day cooldown after a drawdown halt, before entries re-arm. Needed
    # because a halted desk goes flat: with no positions its equity cannot rise,
    # so a drawdown-only recovery rule can never trigger (measured, see
    # docs/experiments.md).
    halt_recovery_days: int | None = None


@dataclass(frozen=True)
class ExecutionConfig:
    stock_gap_limit_pct: float
    crypto_gap_limit_pct: float
    paper_only: bool
    use_margin: bool
    equity_slippage_bps: float
    crypto_taker_fee_bps: float


@dataclass(frozen=True)
class ResearchConfig:
    regime_min_score: int
    feature_max_age_hours: int
    news_lookback_days: int
    news_min_observations: int
    negative_news_z: float
    sec_lookback_hours: int
    sec_forms: tuple[str, ...]
    vix_percentile: float
    volatility_percentile: float
    model: str
    model_revision: str
    arms: tuple[str, ...]


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    starting_equity: float
    strategy: StrategyConfig
    portfolio: PortfolioConfig
    execution: ExecutionConfig
    research: ResearchConfig
    crypto_symbols: tuple[str, ...]
    stock_symbols: tuple[str, ...]
    raw: dict[str, Any]
    fingerprint: str

    @property
    def symbols(self) -> tuple[tuple[str, str], ...]:
        return tuple((s, "crypto") for s in self.crypto_symbols) + tuple(
            (s, "stock") for s in self.stock_symbols
        )

    def gap_limit_pct(self, asset: str) -> float:
        return (
            self.execution.crypto_gap_limit_pct
            if asset == "crypto"
            else self.execution.stock_gap_limit_pct
        )


def _canonical(raw: dict[str, Any]) -> str:
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _require(raw: dict[str, Any], key: str, parent: str = "config") -> Any:
    if key not in raw:
        raise RunConfigError(f"Missing {parent}.{key}")
    return raw[key]


def load_run_config(path: str | Path, *, strict: bool = True) -> RunConfig:
    """Load and validate a manifest, returning its stable content hash.

    ``strict`` (the default) pins every frozen Run 2 value and the archived
    17-symbol watchlist, so the audited live run can never be re-pointed at a
    different manifest. Research commands pass ``strict=False`` to replay an
    experiment manifest: safety and consistency checks still apply, the live
    ``run2`` run_id is still refused, and no experiment may relax the
    paper-only / no-margin contract.
    """
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise RunConfigError("Run config must be a YAML mapping")

    strategy_raw = _require(raw, "strategy")
    portfolio_raw = _require(raw, "portfolio")
    execution_raw = _require(raw, "execution")
    research_raw = _require(raw, "research")
    crypto_raw = _require(raw, "crypto")
    stocks_raw = _require(raw, "stocks")

    strategy = StrategyConfig(**strategy_raw)
    portfolio = PortfolioConfig(**portfolio_raw)
    execution = ExecutionConfig(**execution_raw)
    research = ResearchConfig(
        **{
            **research_raw,
            "arms": tuple(research_raw.get("arms", ())),
            "sec_forms": tuple(research_raw.get("sec_forms", ())),
        },
    )
    cfg = RunConfig(
        run_id=str(_require(raw, "run_id")),
        starting_equity=float(_require(raw, "starting_equity")),
        strategy=strategy,
        portfolio=portfolio,
        execution=execution,
        research=research,
        crypto_symbols=tuple(crypto_raw.get("symbols", ())),
        stock_symbols=tuple(stocks_raw.get("symbols", ())),
        raw=raw,
        fingerprint=sha256(_canonical(raw).encode()).hexdigest(),
    )
    _validate(cfg, strict=strict)
    return cfg


def _validate(cfg: RunConfig, *, strict: bool = True) -> None:
    if not cfg.run_id or any(c.isspace() for c in cfg.run_id):
        raise RunConfigError("run_id must be non-empty and contain no whitespace")
    if cfg.starting_equity <= 0:
        raise RunConfigError("starting_equity must be positive")
    if cfg.strategy.name != "sma_cross":
        raise RunConfigError("Run 2 baseline must remain sma_cross")
    if cfg.strategy.fast_window <= 0 or cfg.strategy.fast_window >= cfg.strategy.slow_window:
        raise RunConfigError("SMA windows must satisfy 0 < fast_window < slow_window")
    if cfg.strategy.timeframe != "1d":
        raise RunConfigError("Run 2 is frozen to closed daily bars")
    if not cfg.execution.paper_only or cfg.execution.use_margin:
        raise RunConfigError("Run 2 must be paper-only with margin disabled")
    if cfg.portfolio.position_notional * cfg.portfolio.max_positions > cfg.portfolio.max_gross_exposure:
        raise RunConfigError("position_notional * max_positions exceeds max_gross_exposure")
    if cfg.portfolio.max_gross_exposure > cfg.starting_equity:
        raise RunConfigError("max_gross_exposure cannot exceed starting equity")
    if not 0 < cfg.portfolio.drawdown_halt_pct < 100:
        raise RunConfigError("drawdown_halt_pct must be between 0 and 100")
    recovery = cfg.portfolio.halt_recovery_drawdown_pct
    if recovery is not None and not 0 < recovery < cfg.portfolio.drawdown_halt_pct:
        raise RunConfigError(
            "halt_recovery_drawdown_pct must be above 0 and below drawdown_halt_pct"
        )
    recovery_days = cfg.portfolio.halt_recovery_days
    if recovery_days is not None and recovery_days <= 0:
        raise RunConfigError("halt_recovery_days must be positive")
    for name in ("stop_breakeven_at_pct", "stop_trail_pct"):
        value = getattr(cfg.strategy, name)
        if value is not None and value <= 0:
            raise RunConfigError(f"{name} must be positive when set")
    if not 0 <= cfg.portfolio.correlation_threshold <= 1:
        raise RunConfigError("correlation_threshold must be in [0, 1]")
    if not cfg.crypto_symbols or not cfg.stock_symbols:
        raise RunConfigError("Both crypto and stock watchlists must be non-empty")
    symbols = [s.replace("/", "") for s, _ in cfg.symbols]
    if len(symbols) != len(set(symbols)):
        raise RunConfigError("Watchlist symbols must be unique")
    expected_arms = {"baseline", "shadow_regime", "shadow_regime_news"}
    if set(cfg.research.arms) != expected_arms:
        raise RunConfigError(f"research.arms must be exactly {sorted(expected_arms)}")
    if cfg.research.sec_lookback_hours <= 0 or not cfg.research.sec_forms:
        raise RunConfigError("SEC lookback and form allowlist must be non-empty")
    if cfg.research.feature_max_age_hours <= 0:
        raise RunConfigError("feature_max_age_hours must be positive")

    if not strict:
        # Experiment path: parameters may differ, but the audited live run and
        # the paper-only contract stay off limits.
        if cfg.run_id in LIVE_RUN_IDS:
            raise RunConfigError(
                f"experiment manifests must not reuse the live run_id {cfg.run_id!r}"
            )
        return

    # A live run's manifest may never drift from its blessed parameter set: a
    # new risk rule needs a new run_id (and a human decision — see
    # _FROZEN_BASELINES), and the ledger's stored hash would reject an edit
    # anyway. A manifest with any other run_id must still carry the audited
    # baseline values to pass this loader (that is how `scale-1x`, the faithful
    # copy used as the ladder reference, keeps loading); anything else must
    # replay read-only via portfolio-backtest.
    baseline = _FROZEN_BASELINES.get(cfg.run_id, _FROZEN_BASELINES[LIVE_RUN_ID])
    frozen = {
        "starting_equity": (cfg.starting_equity, baseline["starting_equity"]),
        "halt_recovery_drawdown_pct": (
            cfg.portfolio.halt_recovery_drawdown_pct,
            baseline["halt_recovery_drawdown_pct"],
        ),
        "halt_recovery_days": (
            cfg.portfolio.halt_recovery_days,
            baseline["halt_recovery_days"],
        ),
        "fast_window": (cfg.strategy.fast_window, baseline["fast_window"]),
        "slow_window": (cfg.strategy.slow_window, baseline["slow_window"]),
        "stop_loss_pct": (cfg.strategy.stop_loss_pct, baseline["stop_loss_pct"]),
        "stop_breakeven_at_pct": (
            cfg.strategy.stop_breakeven_at_pct,
            baseline["stop_breakeven_at_pct"],
        ),
        "stop_trail_pct": (cfg.strategy.stop_trail_pct, baseline["stop_trail_pct"]),
        "position_notional": (cfg.portfolio.position_notional, baseline["position_notional"]),
        "max_positions": (cfg.portfolio.max_positions, baseline["max_positions"]),
        "max_gross_exposure": (cfg.portfolio.max_gross_exposure, baseline["max_gross_exposure"]),
        "max_crypto_positions": (cfg.portfolio.max_crypto_positions, baseline["max_crypto_positions"]),
        "max_crypto_exposure": (cfg.portfolio.max_crypto_exposure, baseline["max_crypto_exposure"]),
        "max_stock_positions": (cfg.portfolio.max_stock_positions, baseline["max_stock_positions"]),
        "max_stock_exposure": (cfg.portfolio.max_stock_exposure, baseline["max_stock_exposure"]),
        "correlation_window": (cfg.portfolio.correlation_window, baseline["correlation_window"]),
        "correlation_threshold": (cfg.portfolio.correlation_threshold, baseline["correlation_threshold"]),
        "correlation_matches_allowed": (
            cfg.portfolio.correlation_matches_allowed,
            baseline["correlation_matches_allowed"],
        ),
        "drawdown_halt_pct": (cfg.portfolio.drawdown_halt_pct, baseline["drawdown_halt_pct"]),
        "stock_gap_limit_pct": (cfg.execution.stock_gap_limit_pct, baseline["stock_gap_limit_pct"]),
        "crypto_gap_limit_pct": (cfg.execution.crypto_gap_limit_pct, baseline["crypto_gap_limit_pct"]),
    }
    changed = [name for name, (actual, expected) in frozen.items() if actual != expected]
    if changed:
        raise RunConfigError(
            f"frozen values changed for run {cfg.run_id!r}: {', '.join(changed)}"
        )
    if not cfg.strategy.require_fresh_cross_after_stop:
        raise RunConfigError("Live runs require a fresh SMA cross after a stopped position")
    if cfg.crypto_symbols != _RUN2_CRYPTO or cfg.stock_symbols != _RUN2_STOCKS:
        raise RunConfigError("Live watchlists must match the archived 17-symbol baseline")
