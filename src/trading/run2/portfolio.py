"""Cash-constrained, synchronized portfolio simulation and statistics."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import NormalDist
from typing import Callable, Mapping

import numpy as np
import pandas as pd

from ..strategies.base import sma_cross_signal, sma_trend_up
from .config import RunConfig
from .risk import DRAWDOWN_HALT_PREFIX, drawdown_pct, halt_action
from .sizing import entry_notional


def profit_target(entry_price: float, take_profit_pct: float | None) -> float | None:
    """Fill-derived profit target, or ``None`` when the experiment knob is off.

    ``None``, 0, and below all mean off — the same falsey contract as
    ``protective_stop``. A live run pins the knob to None, so this level is
    never installed outside ``portfolio-backtest``.
    """
    if not take_profit_pct or take_profit_pct <= 0:
        return None
    return entry_price * (1 + take_profit_pct / 100)


def protective_stop(entry_price: float, stop_loss_pct: float) -> float | None:
    """Fill-derived protective stop level, or ``None`` when the stop is disabled.

    A ``stop_loss_pct`` of 0 (or below) disables the stop — the same contract as
    ``trading.live.trader.stop_breached`` and the CLI stop monitor, both of which
    treat a falsey/negative threshold as "off". The replay must agree: taking 0
    literally (``entry * (1 - 0/100)``) parks the stop *at* the entry price and
    stops every position on its entry bar at a fee-paying round trip, which is a
    replay-only artifact no manifest could intend.
    """
    if not stop_loss_pct or stop_loss_pct <= 0:
        return None
    return entry_price * (1 - stop_loss_pct / 100)


@dataclass
class SimPosition:
    symbol: str
    asset: str
    qty: float
    entry: float
    stop: float | None  # None = no protective stop (manifest stop_loss_pct 0)
    entry_fee: float
    peak: float = 0.0  # highest close since entry (drives experiment trail/breakeven stops)
    bars_held: int = 0  # own sessions completed; only the experiment time stop reads it


@dataclass
class PendingIntent:
    symbol: str
    asset: str
    side: str
    signal_price: float
    created: pd.Timestamp
    # Notional fixed at signal time (risk-scaled when the experiment knob is set,
    # otherwise the flat position_notional). Sells carry 0 — they close a position.
    notional: float = 0.0
    # Set only by an experiment exit (a time stop). A reverse-cross sell leaves
    # this empty so the trade row stays reason-less, matching the frozen path.
    reason: str | None = None


@dataclass(frozen=True)
class PortfolioSimulation:
    equity: pd.Series
    trades: pd.DataFrame
    decisions: pd.DataFrame
    metrics: dict[str, float]


def primary_benchmark(
    cfg: RunConfig,
    frames: Mapping[str, pd.DataFrame],
    start: str | pd.Timestamp | None = None,
    cash_yield: pd.Series | None = None,
) -> pd.Series:
    """Monthly-rebalanced 25% stocks / 25% crypto / 50% T-bill benchmark."""
    closes = {
        symbol: pd.Series(frame["Close"], index=pd.to_datetime(frame.index), dtype=float)
        for symbol, frame in frames.items()
        if symbol in {s for s, _ in cfg.symbols} and not frame.empty
    }
    panel = pd.DataFrame(closes).sort_index().ffill()
    if start is not None:
        start_ts = pd.Timestamp(start)
        if panel.index.tz is None and start_ts.tzinfo is not None:
            start_ts = start_ts.tz_convert("UTC").tz_localize(None)
        elif panel.index.tz is not None and start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize(panel.index.tz)
        panel = panel.loc[start_ts:]
    if panel.empty:
        return pd.Series(dtype=float, name="primary_benchmark")
    returns = panel.pct_change().fillna(0.0)
    stock_cols = [s for s in cfg.stock_symbols if s in returns]
    crypto_cols = [s for s in cfg.crypto_symbols if s in returns]
    if not stock_cols or not crypto_cols:
        return pd.Series(dtype=float, name="primary_benchmark")

    if cash_yield is None:
        cash_daily = pd.Series(0.0, index=returns.index)
    else:
        annual = pd.Series(cash_yield, dtype=float)
        annual.index = pd.to_datetime(annual.index)
        cash_daily = annual.reindex(returns.index).ffill().fillna(0.0) / 100 / 365

    wealth = cfg.starting_equity
    output: dict[pd.Timestamp, float] = {}
    for _month, group in returns.groupby(returns.index.to_period("M")):
        stock_leg = wealth * 0.25
        crypto_leg = wealth * 0.25
        cash_leg = wealth * 0.50
        stock_units = {c: stock_leg / len(stock_cols) for c in stock_cols}
        crypto_units = {c: crypto_leg / len(crypto_cols) for c in crypto_cols}
        for day, row in group.iterrows():
            for c in stock_cols:
                stock_units[c] *= 1 + float(row[c])
            for c in crypto_cols:
                crypto_units[c] *= 1 + float(row[c])
            cash_leg *= 1 + float(cash_daily.loc[day])
            wealth = sum(stock_units.values()) + sum(crypto_units.values()) + cash_leg
            output[day] = wealth
    return pd.Series(output, name="primary_benchmark", dtype=float)


def _cost_rate(cfg: RunConfig, asset: str) -> float:
    bps = (
        cfg.execution.crypto_taker_fee_bps
        if asset == "crypto"
        else cfg.execution.equity_slippage_bps
    )
    return bps / 10_000


def _correlation_matches_on_day(
    candidate: str,
    held: Mapping[str, SimPosition],
    indexed: Mapping[str, pd.DataFrame],
    day: pd.Timestamp,
    window: int,
    threshold: float,
) -> int:
    candidate_frame = indexed.get(candidate)
    if candidate_frame is None:
        return 0
    candidate_returns = candidate_frame.loc[:day, "Close"].pct_change().dropna().tail(window)
    matches = 0
    for symbol in held:
        other_frame = indexed.get(symbol)
        if other_frame is None:
            continue
        other_returns = other_frame.loc[:day, "Close"].pct_change().dropna().tail(window)
        aligned = pd.concat([candidate_returns, other_returns], axis=1, join="inner").dropna()
        if len(aligned) < max(20, window // 2):
            continue
        corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
        if pd.notna(corr) and corr >= threshold:
            matches += 1
    return matches


def _metrics(equity: pd.Series, trades: pd.DataFrame, starting: float) -> dict[str, float]:
    returns = equity.pct_change().dropna()
    total_return = (float(equity.iloc[-1]) / starting - 1) * 100 if len(equity) else 0.0
    dd = equity / equity.cummax() - 1 if len(equity) else pd.Series(dtype=float)
    sharpe = float(returns.mean() / returns.std(ddof=1) * sqrt(365)) if len(returns) > 1 and returns.std() else 0.0
    downside = returns[returns < 0].std(ddof=1)
    sortino = float(returns.mean() / downside * sqrt(365)) if downside and not np.isnan(downside) else 0.0
    closed = trades[trades["side"] == "sell"] if not trades.empty else trades
    pnls = closed["realized_pl"] if not closed.empty else pd.Series(dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    return {
        "return_pct": total_return,
        "max_drawdown_pct": float(dd.min() * 100) if len(dd) else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "completed_trades": float(len(pnls)),
        "win_rate_pct": float((pnls > 0).mean() * 100) if len(pnls) else 0.0,
        "expectancy": float(pnls.mean()) if len(pnls) else 0.0,
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else 0.0,
    }


def simulate_portfolio(
    cfg: RunConfig,
    frames: Mapping[str, pd.DataFrame],
    entry_gate: Callable[[str, str, pd.Timestamp], bool] | None = None,
) -> PortfolioSimulation:
    """Replay the frozen strategy with shared cash, gaps, stops, and fees."""
    clean = {s: f.sort_index().copy() for s, f in frames.items() if not f.empty}
    dates = sorted(set().union(*(set(pd.to_datetime(f.index).normalize()) for f in clean.values())))
    cash = cfg.starting_equity
    positions: dict[str, SimPosition] = {}
    pending: dict[str, PendingIntent] = {}
    last_prices: dict[str, float] = {}
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    trade_rows: list[dict] = []
    decision_rows: list[dict] = []
    high_water = 0.0
    # Non-None while the run is halted by drawdown; exits still execute, entries do not.
    halted_reason: str | None = None
    halted_day: pd.Timestamp | None = None
    # day each symbol was last stopped out of (experiment-only re-entry window).
    stopped_on: dict[str, pd.Timestamp] = {}

    indexed = {s: f.assign(_day=pd.to_datetime(f.index).normalize()).set_index("_day", drop=True) for s, f in clean.items()}
    for day in dates:
        # Execute prior closed-bar intents at the next available bar open.
        for symbol in sorted(list(pending)):
            intent = pending[symbol]
            frame = indexed.get(symbol)
            if frame is None or day not in frame.index or day <= intent.created:
                continue
            row = frame.loc[day]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            open_price = float(row["Open"])
            if intent.side == "buy":
                gap_limit = intent.signal_price * (1 + cfg.gap_limit_pct(intent.asset) / 100)
                if open_price > gap_limit:
                    decision_rows.append({"date": day, "symbol": symbol, "action": "expired_gap"})
                    del pending[symbol]
                    continue
                notional = min(intent.notional, cash)
                rate = _cost_rate(cfg, intent.asset)
                fee = notional * rate
                if notional + fee > cash:
                    del pending[symbol]
                    continue
                qty = notional / open_price
                cash -= notional + fee
                positions[symbol] = SimPosition(
                    symbol, intent.asset, qty, open_price,
                    protective_stop(open_price, cfg.strategy.stop_loss_pct),
                    fee,
                )
                trade_rows.append({"date": day, "symbol": symbol, "asset": intent.asset, "side": "buy", "qty": qty, "price": open_price, "fee": fee, "realized_pl": 0.0})
            else:
                pos = positions.pop(symbol, None)
                if pos:
                    gross = pos.qty * open_price
                    fee = gross * _cost_rate(cfg, pos.asset)
                    cash += gross - fee
                    realized = (open_price - pos.entry) * pos.qty - pos.entry_fee - fee
                    row = {"date": day, "symbol": symbol, "asset": pos.asset, "side": "sell", "qty": pos.qty, "price": open_price, "fee": fee, "realized_pl": realized}
                    if intent.reason:
                        row["reason"] = intent.reason
                    trade_rows.append(row)
            del pending[symbol]

        # Intraday stop approximation: gap at open, otherwise the fill-derived
        # stop level (None = the manifest disabled the stop, stop_loss_pct 0).
        for symbol in sorted(list(positions)):
            frame = indexed.get(symbol)
            if frame is None or day not in frame.index:
                continue
            row = frame.loc[day]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            pos = positions[symbol]
            if pos.stop is None or float(row["Low"]) > pos.stop:
                continue
            exit_price = min(float(row["Open"]), pos.stop)
            gross = pos.qty * exit_price
            fee = gross * _cost_rate(cfg, pos.asset)
            cash += gross - fee
            realized = (exit_price - pos.entry) * pos.qty - pos.entry_fee - fee
            trade_rows.append({"date": day, "symbol": symbol, "asset": pos.asset, "side": "sell", "qty": pos.qty, "price": exit_price, "fee": fee, "realized_pl": realized, "reason": "stop"})
            del positions[symbol]
            # Arms the experiment-only re-entry window (inert while the knob is
            # None, which is every live run) — and never a same-bar re-entry.
            stopped_on[symbol] = day

        # Experiment-only profit target (None on every live run). Intraday, like
        # the stop: a gap through the target fills at the open (the favorable
        # gap); a bar that only trades through it fills at the resting level.
        # The stop loop already ran, so a bar that prints both is a stop — the
        # tape order is unknowable, and booking the loss is the conservative
        # read. A take-profit does not arm ``stopped_on``: it is not a stop, and
        # the frozen fresh-cross rule still gates the next entry.
        tp = cfg.strategy.take_profit_pct
        if tp:
            for symbol in sorted(list(positions)):
                frame = indexed[symbol]
                if day not in frame.index:
                    continue
                row = frame.loc[day]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
                pos = positions[symbol]
                target = profit_target(pos.entry, tp)
                assert target is not None  # the loop only runs when the knob is set
                if float(row["High"]) < target:
                    continue
                open_price = float(row["Open"])
                exit_price = open_price if open_price >= target else target
                gross = pos.qty * exit_price
                fee = gross * _cost_rate(cfg, pos.asset)
                cash += gross - fee
                realized = (exit_price - pos.entry) * pos.qty - pos.entry_fee - fee
                trade_rows.append({
                    "date": day, "symbol": symbol, "asset": pos.asset, "side": "sell",
                    "qty": pos.qty, "price": exit_price, "fee": fee,
                    "realized_pl": realized, "reason": "take_profit",
                })
                del positions[symbol]

        # Experiment-only stop refinements (both knobs None on every live run):
        # track each position's highest close and optionally raise the stop to
        # breakeven and/or trail it under the peak. Applies from the next
        # session — today's stop checks already ran.
        if cfg.strategy.stop_breakeven_at_pct is not None or cfg.strategy.stop_trail_pct is not None:
            for symbol in sorted(positions):
                frame = indexed.get(symbol)
                if frame is not None and day in frame.index:
                    row = frame.loc[day]
                    if isinstance(row, pd.DataFrame):
                        row = row.iloc[0]
                    pos = positions[symbol]
                    close = float(row["Close"])
                    pos.peak = max(pos.peak or pos.entry, close)
                    # A disabled stop (None) is "below everything": a refinement
                    # knob that sets a level simply installs one.
                    floor = pos.stop if pos.stop is not None else float("-inf")
                    be = cfg.strategy.stop_breakeven_at_pct
                    if be is not None and close >= pos.entry * (1 + be / 100):
                        pos.stop = max(floor, pos.entry)
                        floor = pos.stop
                    trail = cfg.strategy.stop_trail_pct
                    if trail is not None:
                        pos.stop = max(floor, pos.peak * (1 - trail / 100))

        # Generate fresh signals only after today's close.
        for symbol, asset in sorted(cfg.symbols, key=lambda x: x[0]):
            frame = indexed.get(symbol)
            if frame is None or day not in frame.index:
                continue
            history = frame.loc[:day]
            signal = sma_cross_signal(
                history["Close"],
                cfg.strategy.fast_window,
                cfg.strategy.slow_window,
                cfg.strategy.entry_min_separation_pct,
            )
            price = float(history["Close"].iloc[-1])
            last_prices[symbol] = price
            action = "none"
            blocked_by: str | None = None
            # Experiment-only re-entry window (None on every live run): after a
            # protective stop, the frozen gate waits for a *fresh* up-cross, so an
            # intact up-trend is sat out. With the knob set, a symbol stopped out
            # within the last N of its own sessions is an entry candidate again
            # while the fast SMA is still above the slow SMA — every guard below
            # still applies, and it is priced exactly like a cross entry.
            reentry = False
            if cfg.strategy.stop_reentry_bars is not None and signal != "BUY":
                stop_day = stopped_on.get(symbol)
                if stop_day is not None:
                    since = int(((frame.index > stop_day) & (frame.index <= day)).sum())
                    if 0 < since <= cfg.strategy.stop_reentry_bars and sma_trend_up(
                        history["Close"], cfg.strategy.fast_window, cfg.strategy.slow_window
                    ):
                        reentry = True
            if (signal == "BUY" or reentry) and symbol not in positions and symbol not in pending:
                same = [p for p in positions.values() if p.asset == asset]
                pending_buys = [p for p in pending.values() if p.side == "buy"]
                same_pending = [p for p in pending_buys if p.asset == asset]
                gross = sum(p.qty * last_prices.get(p.symbol, p.entry) for p in positions.values())
                same_gross = sum(p.qty * last_prices.get(p.symbol, p.entry) for p in same)
                asset_kind = "crypto" if asset == "crypto" else "stock"
                max_count = cfg.portfolio.max_crypto_positions if asset == "crypto" else cfg.portfolio.max_stock_positions
                max_exposure = cfg.portfolio.max_crypto_exposure if asset == "crypto" else cfg.portfolio.max_stock_exposure
                correlation_count = _correlation_matches_on_day(
                    symbol,
                    positions,
                    indexed,
                    day,
                    cfg.portfolio.correlation_window,
                    cfg.portfolio.correlation_threshold,
                )
                # Same checks, order and names as the live gate in
                # trading.run2.risk.entry_allowed, evaluated one by one so a
                # suppressed buy carries the guard that stopped it into
                # decisions.csv. Deployment is capped by whichever of these
                # fires first, and only the counters can say which one it is.
                # The candidate's own notional is used (flat on every live run;
                # per-asset tilted and/or risk-scaled when an experiment knob is
                # set), and pending buys contribute their own committed
                # notionals rather than a count times a flat size.
                notional = entry_notional(cfg, history["Close"], asset)
                pending_notional = sum(p.notional for p in pending_buys)
                same_pending_notional = sum(p.notional for p in same_pending)
                if len(positions) + len(pending_buys) >= cfg.portfolio.max_positions:
                    blocked_by = "max_positions"
                elif gross + pending_notional + notional > cfg.portfolio.max_gross_exposure:
                    blocked_by = "max_gross_exposure"
                elif len(same) + len(same_pending) >= max_count:
                    blocked_by = f"max_{asset_kind}_positions"
                elif same_gross + same_pending_notional + notional > max_exposure:
                    blocked_by = f"max_{asset_kind}_exposure"
                elif correlation_count > cfg.portfolio.correlation_matches_allowed:
                    blocked_by = f"correlation_cap:{correlation_count}"
                elif halted_reason is not None:
                    blocked_by = "run_halted"
                elif entry_gate is not None and not entry_gate(symbol, asset, day):
                    blocked_by = "entry_gate"
                if blocked_by is None:
                    pending[symbol] = PendingIntent(symbol, asset, "buy", price, day, notional)
                    action = "buy_intent"
            elif signal == "SELL" and symbol in positions and symbol not in pending:
                pending[symbol] = PendingIntent(symbol, asset, "sell", price, day)
                action = "sell_intent"
            # Experiment-only time stop (None on every live run). Count this
            # symbol's own sessions — a missing bar is not a session — and queue
            # the sell for the next open, the same path as a reverse cross. The
            # stop and take-profit loops already ran, so a bar that also hits
            # those is not relabeled. A reverse cross that already queued a sell
            # keeps that reason. This exit does not arm ``stopped_on``.
            hold_bars = cfg.strategy.max_hold_bars
            if hold_bars and symbol in positions:
                pos = positions[symbol]
                pos.bars_held += 1
                if pos.bars_held >= hold_bars and symbol not in pending:
                    pending[symbol] = PendingIntent(symbol, asset, "sell", price, day, reason="time_stop")
                    action = "time_stop"
            decision_rows.append({"date": day, "symbol": symbol, "asset": asset, "signal": signal, "action": action, "price": price, "blocked_by": blocked_by})

        equity = cash + sum(p.qty * last_prices.get(s, p.entry) for s, p in positions.items())
        equity_rows.append((day, equity))
        # Same halt/recovery policy as the live service (trading.run2.risk): the
        # replay must not be able to promise a resume the desk could not execute.
        high_water = max(high_water, equity)
        dd = drawdown_pct(equity, high_water)
        action = halt_action(
            cfg,
            dd,
            halted=halted_reason is not None,
            halt_reason=halted_reason,
            days_since_halt=(
                (day - halted_day).days if halted_day is not None else None
            ),
        )
        if action == "halt":
            halted_reason = f"{DRAWDOWN_HALT_PREFIX}{dd:.4f}%"
            halted_day = day
        elif action == "resume":
            halted_reason = None
            halted_day = None
            # Re-baseline: the resumed run measures drawdown from here, exactly
            # like the live path (the peak it already lost is not counted twice).
            high_water = equity

    equity = pd.Series(dict(equity_rows), name="equity", dtype=float)
    trades = pd.DataFrame(trade_rows)
    decisions = pd.DataFrame(decision_rows)
    return PortfolioSimulation(equity, trades, decisions, _metrics(equity, trades, cfg.starting_equity))


def block_bootstrap_mean_ci(
    values: pd.Series, confidence: float = 0.90, block: int = 5, samples: int = 2000, seed: int = 7
) -> tuple[float, float]:
    clean = pd.Series(values, dtype=float).dropna().to_numpy()
    if len(clean) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(samples):
        picked = []
        while len(picked) < len(clean):
            start = int(rng.integers(0, len(clean)))
            picked.extend(clean[(start + i) % len(clean)] for i in range(block))
        means.append(float(np.mean(picked[: len(clean)])))
    alpha = (1 - confidence) / 2
    return float(np.quantile(means, alpha)), float(np.quantile(means, 1 - alpha))


def deflated_sharpe_probability(returns: pd.Series, trials: int = 3) -> float:
    r = pd.Series(returns, dtype=float).dropna()
    if len(r) < 3 or r.std(ddof=1) == 0:
        return 0.0
    sr = float(r.mean() / r.std(ddof=1))
    skew = float(r.skew())
    kurt = float(r.kurt() + 3)
    variance_sr = max(1e-12, (1 - skew * sr + ((kurt - 1) / 4) * sr * sr) / (len(r) - 1))
    normal = NormalDist()
    if trials <= 1:
        expected_max = 0.0
    else:
        gamma = 0.5772156649
        expected_max = sqrt(variance_sr) * (
            (1 - gamma) * normal.inv_cdf(1 - 1 / trials)
            + gamma * normal.inv_cdf(1 - 1 / (trials * np.e))
        )
    denominator = sqrt(max(1e-12, 1 - skew * sr + ((kurt - 1) / 4) * sr * sr))
    z = (sr - expected_max) * sqrt(len(r) - 1) / denominator
    return float(normal.cdf(z))
