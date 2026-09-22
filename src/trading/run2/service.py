"""Run 2 decision, guarded execution, and broker reconciliation service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
import hashlib
import json
from typing import Any, Mapping

import pandas as pd

from ..live.broker import PaperBroker, quantize_limit_price
from ..strategies.base import sma_cross_signal
from .config import RunConfig
from .ledger import INKIND_FEE_PREFIX, RunLedger, utc_now
from .risk import DRAWDOWN_HALT_PREFIX, check_entry, drawdown_pct, halt_action


class RunSafetyError(RuntimeError):
    """Raised when an operation would violate the frozen paper-run contract."""


def _bar_end(df: pd.DataFrame) -> str:
    ts = pd.Timestamp(df.index[-1])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def _asset_from_broker_symbol(cfg: RunConfig, symbol: str) -> tuple[str, str]:
    target = symbol.replace("/", "")
    for configured, asset in cfg.symbols:
        if configured.replace("/", "") == target:
            return configured, asset
    return symbol, ""


def crypto_buy_fee_in_kind(
    cfg: RunConfig, qty: Decimal, price: Decimal
) -> tuple[Decimal, Decimal]:
    """Split a crypto buy fill into what was received and the in-kind fee.

    Alpaca deducts the crypto taker fee from the asset received, so the position
    always moves by ``qty * (1 - fee_bps/10_000)``: "the base crypto fee is .25%
    which is the difference between .9975 and 1.0" (Alpaca forum, Alpaca staff).
    Confirmed on the live paper account: an AAVE/USD buy activity of 4.409390845
    units left a 4.398367367-unit position. A sell receives USD, so its fee comes
    out of the proceeds instead and needs no adjustment.
    """
    rate = Decimal(str(cfg.execution.crypto_taker_fee_bps)) / Decimal(10_000)
    received = qty * (Decimal(1) - rate)
    # Alpaca holds crypto to 9 decimals and truncates the fee it took in kind
    # (4.409390845 gross -> 4.398367367 held). Truncating the same way keeps the
    # ledger qty equal to the broker's to the last digit, so a later full close
    # leaves no dust position behind to block that symbol forever.
    received = received.quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
    return received, qty * price * rate


def _feature_payload(
    ledger: RunLedger,
    cfg: RunConfig,
    scope: str,
    name: str,
    symbol: str | None = None,
) -> Any | None:
    row = ledger.latest_feature(cfg.run_id, scope, name, symbol)
    if not row:
        return None
    try:
        captured = pd.Timestamp(row["captured_at"])
        if captured.tzinfo is None:
            captured = captured.tz_localize("UTC")
        else:
            captured = captured.tz_convert("UTC")
        age_hours = (pd.Timestamp.now(tz="UTC") - captured).total_seconds() / 3600
        if age_hours > cfg.research.feature_max_age_hours:
            return None
        return json.loads(row["value_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _feature_number(ledger: RunLedger, cfg: RunConfig, scope: str, name: str, symbol: str | None = None) -> float | None:
    value = _feature_payload(ledger, cfg, scope, name, symbol)
    try:
        return float(value["value"] if isinstance(value, dict) and "value" in value else value)
    except (TypeError, ValueError, KeyError):
        return None


class Run2Service:
    def __init__(self, cfg: RunConfig, ledger: RunLedger, broker: PaperBroker | None = None):
        self.cfg = cfg
        self.ledger = ledger
        self.broker = broker

    def initialize(self) -> dict[str, Any]:
        if self.broker is None:
            raise RunSafetyError("A paper broker is required to initialize a run")
        account = self.broker.account()
        positions = self.broker.positions()
        orders = self.broker.all_orders()
        if abs(account["equity"] - self.cfg.starting_equity) > 1:
            raise RunSafetyError(
                f"Paper equity must be ${self.cfg.starting_equity:,.2f}; got ${account['equity']:,.2f}"
            )
        if abs(account["cash"] - self.cfg.starting_equity) > 1:
            raise RunSafetyError(
                f"Paper cash must be ${self.cfg.starting_equity:,.2f}; got ${account['cash']:,.2f}"
            )
        if positions or orders:
            raise RunSafetyError("Paper account must be clean: no positions and no order history")
        self.ledger.initialize_run(self.cfg, account["id"])
        self.record_account_snapshot(account)
        return account

    def resume(self) -> dict[str, Any]:
        """Bind the frozen run to an already-traded paper account (mid-history restart).

        ``initialize()`` demands a clean account at exactly the manifest equity with no
        order history, which can never pass again once the desk has traded. ``resume()``
        binds the frozen manifest to the account as it is right now and imports every
        open position as a simulated baseline fill, so broker qty == ledger qty and
        ``reconcile`` has nothing unknown to halt on. From this point the loop is
        identical to a fresh init: every new broker order must map to a ledger order.

        The frozen manifest is untouched — only the run's starting point moves.
        """
        if self.broker is None:
            raise RunSafetyError("A paper broker is required to resume a run")
        account = self.broker.account()
        positions = self.broker.positions()
        if self.ledger.run(self.cfg.run_id) is not None:
            raise RunSafetyError(f"Run {self.cfg.run_id!r} is already initialized")
        self.ledger.initialize_run(self.cfg, account["id"])
        imported: list[dict[str, Any]] = []
        warnings: list[str] = []
        for p in positions:
            symbol, asset = _asset_from_broker_symbol(self.cfg, str(p["symbol"]))
            if not asset:
                asset = "crypto" if "/" in str(p["symbol"]) else "stock"
                warnings.append(
                    f"position_outside_universe:{symbol} — seeded so broker qty matches, "
                    "but the frozen scan will not rotate it (stops still manage it)"
                )
            qty = Decimal(str(p["qty"]))
            avg = Decimal(str(p["avg_entry"]))
            if qty <= 0 or avg <= 0:
                continue
            now = utc_now()
            notional = qty * avg
            decision_id = self.ledger.record_decision(
                {
                    "run_id": self.cfg.run_id,
                    "portfolio": "baseline",
                    "symbol": symbol,
                    "asset": asset,
                    "strategy": self.cfg.strategy.name,
                    "bar_end": now,
                    "signal": "BUY",
                    "signal_price": str(avg),
                    "notional": str(notional),
                    "action": "buy_intent",
                    "reason": "resume_baseline",
                    "status": "filled",
                    "config_hash": self.cfg.fingerprint,
                }
            )
            order_pk = self.ledger.record_order(
                {
                    "run_id": self.cfg.run_id,
                    "decision_id": decision_id,
                    "portfolio": "baseline",
                    "client_order_id": f"{self.cfg.run_id}-resume-{decision_id[:12]}",
                    "symbol": symbol,
                    "asset": asset,
                    "side": "buy",
                    "requested_notional": str(notional),
                    "limit_price": str(avg),
                    "status": "filled",
                    "submitted_at": now,
                }
            )
            self.ledger.record_fill(
                {
                    "fill_id": f"resume-{decision_id}",
                    "run_id": self.cfg.run_id,
                    "order_pk": order_pk,
                    "decision_id": decision_id,
                    "portfolio": "baseline",
                    "broker_order_id": None,
                    "symbol": symbol,
                    "asset": asset,
                    "side": "buy",
                    "qty": str(qty),
                    "price": str(avg),
                    "transaction_time": now,
                    "simulated": 1,
                }
            )
            imported.append(
                {"symbol": symbol, "asset": asset, "qty": str(qty), "avg_entry": str(avg)}
            )
        snap = self.record_account_snapshot(account)
        return {
            "equity": account["equity"],
            "cash": account["cash"],
            "imported": imported,
            "warnings": warnings,
            "drawdown_pct": float(snap["drawdown_pct"]),
        }

    def health(self) -> dict[str, Any]:
        """Read-only ledger health: no broker, no writes. Used by the watchdog."""
        run = self.ledger.assert_manifest(self.cfg)
        history = self.ledger.equity_history(self.cfg.run_id)
        snap = history[-1] if history else None
        return {
            "run_id": self.cfg.run_id,
            "halted": self.ledger.is_halted(self.cfg.run_id),
            "status": run["status"],
            "halt_reason": run["halt_reason"],
            "equity": float(snap["equity"]) if snap else None,
            "drawdown_pct": float(snap["drawdown_pct"]) if snap else None,
            "captured_at": snap["captured_at"] if snap else None,
            "positions": len(self.ledger.positions(self.cfg.run_id)),
            "fees": str(self.ledger.total_fees(self.cfg.run_id)),
        }

    def scan(self, bars: Mapping[str, pd.DataFrame], record: bool = True) -> list[dict[str, Any]]:
        self.ledger.assert_manifest(self.cfg)
        results: list[dict[str, Any]] = []
        for symbol, asset in sorted(self.cfg.symbols, key=lambda item: item[0]):
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                results.append({"symbol": symbol, "portfolio": "all", "action": "none", "reason": "missing_bars"})
                continue
            signal = sma_cross_signal(
                frame["Close"], self.cfg.strategy.fast_window, self.cfg.strategy.slow_window
            )
            price = float(frame["Close"].iloc[-1])
            bar_end = _bar_end(frame)
            scope = "crypto" if asset == "crypto" else "equity"
            regime = _feature_number(self.ledger, self.cfg, scope, "regime_score")
            news_payload = _feature_payload(
                self.ledger, self.cfg, "news", "negative_news_z", symbol
            )
            try:
                news_z = float(news_payload["value"])
                news_observations = int(news_payload.get("observations", 0))
            except (TypeError, ValueError, KeyError, AttributeError):
                news_z, news_observations = None, 0

            for portfolio in self.cfg.research.arms:
                positions = self.ledger.positions(self.cfg.run_id, portfolio)
                holding = symbol in positions
                action, reason, status = "none", "no_fresh_cross", "recorded"
                if signal == "BUY" and not holding:
                    risk = check_entry(self.cfg, self.ledger, portfolio, symbol, asset, bars)
                    allowed = risk.allowed
                    reason = risk.reason
                    if portfolio in {"shadow_regime", "shadow_regime_news"}:
                        if regime is None:
                            allowed, reason = False, "missing_regime"
                        elif regime < self.cfg.research.regime_min_score:
                            allowed, reason = False, f"regime_gate:{regime:g}"
                    if portfolio == "shadow_regime_news" and allowed:
                        if news_z is None:
                            allowed, reason = False, "missing_news"
                        elif news_observations < self.cfg.research.news_min_observations:
                            allowed, reason = False, f"news_warmup:{news_observations}"
                        elif news_z >= self.cfg.research.negative_news_z:
                            allowed, reason = False, f"negative_news:{news_z:.2f}"
                    if allowed:
                        action, reason, status = "buy_intent", "allowed", "pending"
                elif signal == "SELL" and holding:
                    action, reason, status = "sell_intent", "reverse_cross", "pending"
                elif holding:
                    reason = "holding"

                row = {
                    "run_id": self.cfg.run_id,
                    "portfolio": portfolio,
                    "symbol": symbol,
                    "asset": asset,
                    "strategy": self.cfg.strategy.name,
                    "bar_end": bar_end,
                    "signal": signal,
                    "signal_price": str(price),
                    "notional": str(self.cfg.portfolio.position_notional),
                    "regime_score": int(regime) if regime is not None else None,
                    "negative_news_z": news_z,
                    "action": action,
                    "reason": reason,
                    "status": status,
                    "config_hash": self.cfg.fingerprint,
                }
                decision_id = self.ledger.record_decision(row) if record else "dry-run"
                results.append({**row, "decision_id": decision_id})
        return results

    def execute_pending(self, asset: str, dry_run: bool = False) -> list[dict[str, Any]]:
        self.ledger.assert_manifest(self.cfg)
        if self.broker is None and not dry_run:
            raise RunSafetyError("A paper broker is required to execute intents")
        if asset == "stock" and self.broker is not None and not self.broker.clock()["is_open"]:
            return [{"action": "deferred_market_closed"}]
        out: list[dict[str, Any]] = []
        decisions = [
            d for d in self.ledger.decisions(self.cfg.run_id, "baseline", "pending")
            if d["asset"] == asset
        ]
        for d in decisions:
            if d["action"] == "buy_intent":
                if self.ledger.is_halted(self.cfg.run_id):
                    self.ledger.set_decision_status(d["decision_id"], "expired", "run_halted")
                    out.append({"decision_id": d["decision_id"], "action": "expired_halt"})
                    continue
                gap = self.cfg.gap_limit_pct(asset) / 100
                raw_limit = float(d["signal_price"]) * (1 + gap)
                current = self.broker.latest_price(d["symbol"], asset) if self.broker else float(d["signal_price"])
                if current > raw_limit:
                    self.ledger.set_decision_status(d["decision_id"], "expired", f"adverse_gap:{current:.8f}")
                    out.append({"decision_id": d["decision_id"], "action": "expired_gap", "price": current})
                    continue
                limit_price = quantize_limit_price(raw_limit, asset)
                if dry_run:
                    out.append({"decision_id": d["decision_id"], "action": "would_buy", "limit_price": limit_price})
                    continue
                client_id = f"{self.cfg.run_id}-{d['decision_id'][:12]}-buy"
                order = self.broker.buy_limit(
                    d["symbol"], float(d["notional"]), asset, limit_price, client_id
                )
                self.ledger.record_order(
                    {
                        "run_id": self.cfg.run_id,
                        "decision_id": d["decision_id"],
                        "portfolio": "baseline",
                        "client_order_id": client_id,
                        "broker_order_id": order["id"],
                        "symbol": d["symbol"],
                        "asset": asset,
                        "side": "buy",
                        "requested_notional": d["notional"],
                        "limit_price": str(limit_price),
                        "status": order["status"],
                        "submitted_at": order["submitted_at"],
                    }
                )
                self.ledger.set_decision_status(d["decision_id"], "submitted")
                out.append({"decision_id": d["decision_id"], "action": "submitted", "order_id": order["id"]})
            elif d["action"] == "sell_intent":
                if dry_run:
                    out.append({"decision_id": d["decision_id"], "action": "would_close"})
                    continue
                order_id = self.broker.close(d["symbol"])
                client_id = f"{self.cfg.run_id}-{d['decision_id'][:12]}-sell"
                self.ledger.record_order(
                    {
                        "run_id": self.cfg.run_id,
                        "decision_id": d["decision_id"],
                        "portfolio": "baseline",
                        "client_order_id": client_id,
                        "broker_order_id": order_id,
                        "symbol": d["symbol"],
                        "asset": asset,
                        "side": "sell",
                        "status": "submitted",
                        "submitted_at": utc_now(),
                    }
                )
                self.ledger.set_decision_status(d["decision_id"], "submitted")
                out.append({"decision_id": d["decision_id"], "action": "submitted", "order_id": order_id})
        return out

    def check_stops(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """Use internal fill cost basis for the frozen 8% protective stop."""
        self.ledger.assert_manifest(self.cfg)
        if self.broker is None:
            raise RunSafetyError("A paper broker is required to check stops")
        broker_positions = {p["symbol"].replace("/", ""): p for p in self.broker.positions()}
        out: list[dict[str, Any]] = []
        for symbol, pos in self.ledger.positions(self.cfg.run_id, "baseline").items():
            broker_pos = broker_positions.get(symbol.replace("/", ""))
            if not broker_pos or broker_pos.get("current_price") is None:
                out.append({"symbol": symbol, "action": "none", "reason": "missing_broker_position"})
                continue
            price = Decimal(str(broker_pos["current_price"]))
            plpc = (price / pos.avg_entry - 1) * 100 if pos.avg_entry else Decimal(0)
            if plpc > -Decimal(str(self.cfg.strategy.stop_loss_pct)):
                out.append({"symbol": symbol, "action": "none", "plpc": float(plpc)})
                continue
            now = utc_now()
            decision_id = self.ledger.record_decision(
                {
                    "run_id": self.cfg.run_id,
                    "portfolio": "baseline",
                    "symbol": symbol,
                    "asset": pos.asset,
                    "strategy": "stop-monitor",
                    "decided_at": now,
                    "bar_end": now,
                    "signal": "STOP",
                    "signal_price": str(price),
                    "notional": "0",
                    "action": "sell_intent",
                    "reason": f"stop:{float(plpc):.4f}%",
                    "status": "pending" if dry_run else "submitted",
                    "config_hash": self.cfg.fingerprint,
                }
            )
            if dry_run:
                out.append({"symbol": symbol, "action": "would_stop", "plpc": float(plpc)})
                continue
            order_id = self.broker.close(symbol)
            client_id = f"{self.cfg.run_id}-{decision_id[:12]}-stop"
            self.ledger.record_order(
                {
                    "run_id": self.cfg.run_id,
                    "decision_id": decision_id,
                    "portfolio": "baseline",
                    "client_order_id": client_id,
                    "broker_order_id": order_id,
                    "symbol": symbol,
                    "asset": pos.asset,
                    "side": "sell",
                    "status": "submitted",
                    "submitted_at": now,
                }
            )
            out.append({"symbol": symbol, "action": "stopped", "plpc": float(plpc), "order_id": order_id})
        return out

    def simulate_shadow(self, prices: Mapping[str, float], observed_at: str | None = None) -> list[dict[str, Any]]:
        """Fill shadow intents at an observed executable price without broker writes."""
        when = observed_at or utc_now()
        out: list[dict[str, Any]] = []
        for portfolio in ("shadow_regime", "shadow_regime_news"):
            for d in self.ledger.decisions(self.cfg.run_id, portfolio, "pending"):
                if d["symbol"] not in prices:
                    continue
                price = float(prices[d["symbol"]])
                side = "buy" if d["action"] == "buy_intent" else "sell"
                if side == "buy":
                    limit = float(d["signal_price"]) * (1 + self.cfg.gap_limit_pct(d["asset"]) / 100)
                    if price > limit:
                        self.ledger.set_decision_status(d["decision_id"], "expired", f"adverse_gap:{price:.8f}")
                        continue
                    qty = Decimal(str(d["notional"])) / Decimal(str(price))
                else:
                    position = self.ledger.positions(self.cfg.run_id, portfolio).get(d["symbol"])
                    if not position:
                        self.ledger.set_decision_status(d["decision_id"], "rejected", "no_shadow_position")
                        continue
                    qty = position.qty
                fill_id = hashlib.sha256(
                    f"{self.cfg.run_id}|{portfolio}|{d['decision_id']}|{side}|{when}".encode()
                ).hexdigest()
                self.ledger.record_fill(
                    {
                        "fill_id": fill_id,
                        "run_id": self.cfg.run_id,
                        "decision_id": d["decision_id"],
                        "portfolio": portfolio,
                        "symbol": d["symbol"],
                        "asset": d["asset"],
                        "side": side,
                        "qty": str(qty),
                        "price": str(price),
                        "transaction_time": when,
                        "simulated": 1,
                    }
                )
                self.ledger.set_decision_status(d["decision_id"], "filled")
                out.append({"portfolio": portfolio, "symbol": d["symbol"], "side": side, "price": price})
            self._record_shadow_equity(portfolio, prices, when)
        return out

    def _record_shadow_equity(
        self, portfolio: str, marks: Mapping[str, float], captured_at: str
    ) -> None:
        cash = Decimal(str(self.cfg.starting_equity))
        for fill in self.ledger.fills(self.cfg.run_id, portfolio):
            qty = Decimal(str(fill["qty"]))
            price = Decimal(str(fill["price"]))
            rate_bps = (
                self.cfg.execution.crypto_taker_fee_bps
                if fill["asset"] == "crypto"
                else self.cfg.execution.equity_slippage_bps
            )
            fee = qty * price * Decimal(str(rate_bps)) / Decimal(10_000)
            cash += qty * price - fee if fill["side"] == "sell" else -(qty * price + fee)
        positions = self.ledger.positions(self.cfg.run_id, portfolio)
        gross = sum(
            (p.qty * Decimal(str(marks.get(symbol, p.avg_entry))) for symbol, p in positions.items()),
            Decimal(0),
        )
        equity = cash + gross
        hwm = self.ledger.high_water(self.cfg.run_id, portfolio) or Decimal(str(self.cfg.starting_equity))
        dd = drawdown_pct(float(equity), max(float(hwm), float(equity)))
        self.ledger.record_equity(
            {
                "run_id": self.cfg.run_id,
                "portfolio": portfolio,
                "captured_at": captured_at,
                "equity": str(equity),
                "cash": str(cash),
                "gross_exposure": str(gross),
                "drawdown_pct": dd,
                "source": "shadow-simulation",
            }
        )

    def reconcile(self) -> dict[str, Any]:
        self.ledger.assert_manifest(self.cfg)
        if self.broker is None:
            raise RunSafetyError("A paper broker is required to reconcile")
        run = self.ledger.run(self.cfg.run_id)
        after = run["started_at"] if run else None
        unknown_orders: list[str] = []
        new_fills = 0
        new_fees = 0
        for activity in self.broker.activities("FILL", after):
            broker_order_id = str(activity.get("order_id", ""))
            order = self.ledger.order_by_broker_id(self.cfg.run_id, broker_order_id)
            symbol, asset = _asset_from_broker_symbol(self.cfg, str(activity.get("symbol", "")))
            fill_id = str(activity.get("id"))
            if not order:
                unknown_orders.append(broker_order_id)
            side = str(activity.get("side", ""))
            qty = Decimal(str(activity.get("qty", 0)))
            price = Decimal(str(activity.get("price", 0)))
            # Alpaca charges the crypto taker fee in kind on a buy: it is deducted
            # from the asset received, so the position always moves by
            # qty * (1 - fee_bps/10_000) (see crypto_buy_fee_in_kind). Recording the
            # raw activity qty made the ledger permanently 25bps richer than the
            # broker, so this fail-closed check halted the run on its first crypto
            # entry (run3, 2026-09-21) and would halt it on every one.
            in_kind_fee = Decimal(0)
            if asset == "crypto" and side == "buy":
                qty, in_kind_fee = crypto_buy_fee_in_kind(self.cfg, qty, price)
            if in_kind_fee:
                new_fees += int(
                    self.ledger.record_fee(
                        {
                            "fee_id": f"{INKIND_FEE_PREFIX}{fill_id}",
                            "run_id": self.cfg.run_id,
                            "broker_order_id": broker_order_id or None,
                            "symbol": symbol,
                            "activity_type": "CFEE",
                            "amount": str(in_kind_fee),
                            "occurred_at": str(
                                activity.get("transaction_time") or utc_now()
                            ),
                            # The gross activity is the evidence for the in-kind
                            # deduction; the fill row keeps only what was received.
                            "raw_json": json.dumps(activity, default=str, sort_keys=True),
                        }
                    )
                )
            new_fills += int(
                self.ledger.record_fill(
                    {
                        "fill_id": fill_id,
                        "run_id": self.cfg.run_id,
                        "order_pk": order["order_pk"] if order else None,
                        "decision_id": order["decision_id"] if order else None,
                        "portfolio": "baseline",
                        "broker_order_id": broker_order_id,
                        "symbol": symbol,
                        "asset": asset,
                        "side": side,
                        "qty": str(qty),
                        "price": str(price),
                        "transaction_time": str(activity.get("transaction_time") or utc_now()),
                    }
                )
            )

        # IOC orders can finish without a fill. Mirror terminal broker state so
        # a canceled order does not remain a permanent submitted-risk hold.
        started = datetime.fromisoformat(run["started_at"]) if run else None
        for broker_order in self.broker.all_orders(after=started):
            order = self.ledger.order_by_broker_id(self.cfg.run_id, broker_order["id"])
            if not order:
                unknown_orders.append(broker_order["id"])
                continue
            status = str(broker_order["status"]).lower()
            self.ledger.update_order_status(order["order_pk"], status)
            decision_id = order["decision_id"]
            if not decision_id:
                continue
            if float(broker_order.get("filled_qty") or 0) > 0 or status == "filled":
                self.ledger.set_decision_status(decision_id, "filled")
            elif status in {"canceled", "expired", "rejected", "done_for_day"}:
                decision_status = "rejected" if status == "rejected" else "expired"
                self.ledger.set_decision_status(decision_id, decision_status, f"broker:{status}")
        for activity_type in ("CFEE", "FEE"):
            for activity in self.broker.activities(activity_type, after):
                amount = abs(Decimal(str(activity.get("net_amount") or 0)))
                if amount == 0 and activity.get("qty") and activity.get("price"):
                    amount = abs(Decimal(str(activity["qty"])) * Decimal(str(activity["price"])))
                new_fees += int(
                    self.ledger.record_fee(
                        {
                            "fee_id": str(activity.get("id")),
                            "run_id": self.cfg.run_id,
                            "broker_order_id": activity.get("order_id"),
                            "symbol": activity.get("symbol"),
                            "activity_type": activity_type,
                            "amount": str(amount),
                            "occurred_at": str(activity.get("date") or activity.get("transaction_time") or utc_now()),
                            "raw_json": json.dumps(activity, default=str, sort_keys=True),
                        }
                    )
                )

        account = self.broker.account()
        broker_positions = {p["symbol"].replace("/", ""): Decimal(str(p["qty"])) for p in self.broker.positions()}
        ledger_positions = {
            s.replace("/", ""): p.qty for s, p in self.ledger.positions(self.cfg.run_id).items()
        }
        mismatches = []
        for symbol in sorted(set(broker_positions) | set(ledger_positions)):
            if abs(broker_positions.get(symbol, Decimal(0)) - ledger_positions.get(symbol, Decimal(0))) > Decimal("0.00000001"):
                mismatches.append(symbol)
        self.record_account_snapshot(account)
        if unknown_orders or mismatches:
            reason = f"reconciliation_failed:unknown_orders={unknown_orders},qty={mismatches}"
            self.ledger.halt(self.cfg.run_id, reason)
        return {
            "new_fills": new_fills,
            "new_fees": new_fees,
            "unknown_orders": unknown_orders,
            "quantity_mismatches": mismatches,
            "equity": account["equity"],
            "halted": self.ledger.is_halted(self.cfg.run_id),
        }

    def record_account_snapshot(self, account: Mapping[str, Any]) -> dict[str, Any]:
        run_row = self.ledger.run(self.cfg.run_id)
        halt_reason = run_row["halt_reason"] if run_row else None
        # A resumed run measures drawdown from its resume time, not from the peak
        # it already lost — otherwise the halt condition stays true forever and a
        # recovery rule can never take effect.
        baseline_since = (
            run_row["halted_at"]
            if run_row and str(halt_reason or "").startswith("resumed:")
            else None
        )
        hwm = self.ledger.high_water(self.cfg.run_id, since=baseline_since)
        hwm = hwm or Decimal(str(self.cfg.starting_equity))
        equity = float(account["equity"])
        hwm_float = max(float(hwm), equity)
        dd = drawdown_pct(equity, hwm_float)
        gross = sum((p.cost_basis for p in self.ledger.positions(self.cfg.run_id).values()), Decimal(0))
        row = {
            "run_id": self.cfg.run_id,
            "portfolio": "baseline",
            "captured_at": utc_now(),
            "equity": str(equity),
            "cash": str(account["cash"]),
            "gross_exposure": str(gross),
            "drawdown_pct": dd,
            "source": "alpaca-paper",
        }
        self.ledger.record_equity(row)
        run_row = self.ledger.run(self.cfg.run_id)
        halted = self.ledger.is_halted(self.cfg.run_id)
        halt_reason = run_row["halt_reason"] if run_row else None
        days_since_halt = None
        if (
            halted
            and self.cfg.portfolio.halt_recovery_days is not None
            and run_row
            and run_row["halted_at"]
        ):
            days_since_halt = (
                datetime.fromisoformat(utc_now())
                - datetime.fromisoformat(str(run_row["halted_at"]))
            ).total_seconds() / 86_400
        action = halt_action(
            self.cfg,
            dd,
            halted=halted,
            halt_reason=halt_reason,
            days_since_halt=days_since_halt,
        )
        if action == "halt":
            self.ledger.halt(self.cfg.run_id, f"{DRAWDOWN_HALT_PREFIX}{dd:.4f}%")
        elif action == "resume":
            # The resumed run's baseline starts at this snapshot, so the peak it
            # already lost is not counted again (see ledger.high_water).
            self.ledger.resume(
                self.cfg.run_id,
                f"resumed:{halt_reason}@{dd:.4f}%",
                when=str(row["captured_at"]),
            )
        return row
