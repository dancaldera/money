"""Run 2 decision, guarded execution, and broker reconciliation service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping

import pandas as pd

from ..live.broker import PaperBroker, quantize_limit_price
from ..strategies.base import sma_cross_signal
from .config import RunConfig
from .ledger import RunLedger, utc_now
from .risk import check_entry, drawdown_pct


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
        for activity in self.broker.activities("FILL", after):
            broker_order_id = str(activity.get("order_id", ""))
            order = self.ledger.order_by_broker_id(self.cfg.run_id, broker_order_id)
            symbol, asset = _asset_from_broker_symbol(self.cfg, str(activity.get("symbol", "")))
            fill_id = str(activity.get("id"))
            if not order:
                unknown_orders.append(broker_order_id)
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
                        "side": str(activity.get("side", "")),
                        "qty": str(activity.get("qty", 0)),
                        "price": str(activity.get("price", 0)),
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
        new_fees = 0
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
        hwm = self.ledger.high_water(self.cfg.run_id) or Decimal(str(self.cfg.starting_equity))
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
        if dd >= self.cfg.portfolio.drawdown_halt_pct:
            self.ledger.halt(self.cfg.run_id, f"drawdown_halt:{dd:.4f}%")
        return row
