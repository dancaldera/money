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


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    fast_window: int
    slow_window: int
    timeframe: str
    stop_loss_pct: float
    require_fresh_cross_after_stop: bool


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


def load_run_config(path: str | Path) -> RunConfig:
    """Load and validate a manifest, returning its stable content hash."""
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
    _validate(cfg)
    return cfg


def _validate(cfg: RunConfig) -> None:
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

    frozen = {
        "starting_equity": (cfg.starting_equity, 100_000),
        "fast_window": (cfg.strategy.fast_window, 10),
        "slow_window": (cfg.strategy.slow_window, 30),
        "stop_loss_pct": (cfg.strategy.stop_loss_pct, 8),
        "position_notional": (cfg.portfolio.position_notional, 625),
        "max_positions": (cfg.portfolio.max_positions, 8),
        "max_gross_exposure": (cfg.portfolio.max_gross_exposure, 5_000),
        "max_crypto_positions": (cfg.portfolio.max_crypto_positions, 4),
        "max_crypto_exposure": (cfg.portfolio.max_crypto_exposure, 2_500),
        "max_stock_positions": (cfg.portfolio.max_stock_positions, 6),
        "max_stock_exposure": (cfg.portfolio.max_stock_exposure, 3_750),
        "correlation_window": (cfg.portfolio.correlation_window, 60),
        "correlation_threshold": (cfg.portfolio.correlation_threshold, 0.80),
        "correlation_matches_allowed": (cfg.portfolio.correlation_matches_allowed, 1),
        "drawdown_halt_pct": (cfg.portfolio.drawdown_halt_pct, 5),
        "stock_gap_limit_pct": (cfg.execution.stock_gap_limit_pct, 2),
        "crypto_gap_limit_pct": (cfg.execution.crypto_gap_limit_pct, 3),
    }
    changed = [name for name, (actual, expected) in frozen.items() if actual != expected]
    if changed:
        raise RunConfigError(f"Run 2 frozen values changed: {', '.join(changed)}")
    if not cfg.strategy.require_fresh_cross_after_stop:
        raise RunConfigError("Run 2 requires a fresh SMA cross after a stopped position")
    if cfg.crypto_symbols != _RUN2_CRYPTO or cfg.stock_symbols != _RUN2_STOCKS:
        raise RunConfigError("Run 2 watchlists do not match the archived 17-symbol baseline")
