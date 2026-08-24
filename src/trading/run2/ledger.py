"""SQLite event ledger for Run 2 decisions, fills, fees, and features."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .config import RunConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


@dataclass(frozen=True)
class Position:
    portfolio: str
    symbol: str
    asset: str
    qty: Decimal
    avg_entry: Decimal
    cost_basis: Decimal
    realized_pl: Decimal


class LedgerError(RuntimeError):
    """Raised when ledger invariants or the frozen manifest are violated."""


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    starting_equity TEXT NOT NULL,
    broker_account_id TEXT,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    halted_at TEXT,
    halt_reason TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    portfolio TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL,
    strategy TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    bar_end TEXT NOT NULL,
    signal TEXT NOT NULL,
    signal_price TEXT NOT NULL,
    notional TEXT NOT NULL,
    regime_score INTEGER,
    negative_news_z REAL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'recorded',
    config_hash TEXT NOT NULL,
    UNIQUE(run_id, portfolio, symbol, bar_end, signal)
);

CREATE TABLE IF NOT EXISTS orders (
    order_pk TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    decision_id TEXT REFERENCES decisions(decision_id),
    portfolio TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_notional TEXT,
    requested_qty TEXT,
    limit_price TEXT,
    status TEXT NOT NULL,
    submitted_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, client_order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    order_pk TEXT REFERENCES orders(order_pk),
    decision_id TEXT REFERENCES decisions(decision_id),
    portfolio TEXT NOT NULL,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    transaction_time TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fees (
    fee_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    broker_order_id TEXT,
    symbol TEXT,
    activity_type TEXT NOT NULL,
    amount TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    portfolio TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    gross_exposure TEXT NOT NULL,
    drawdown_pct REAL NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(run_id, portfolio, captured_at, source)
);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    feature_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope TEXT NOT NULL,
    symbol TEXT,
    observed_at TEXT NOT NULL,
    published_at TEXT,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    raw_path TEXT,
    UNIQUE(run_id, scope, symbol, observed_at, source, name, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_decisions_pending
    ON decisions(run_id, portfolio, status, asset, bar_end);
CREATE INDEX IF NOT EXISTS idx_fills_position
    ON fills(run_id, portfolio, symbol, transaction_time);
CREATE INDEX IF NOT EXISTS idx_features_latest
    ON feature_snapshots(run_id, scope, symbol, name, observed_at);
"""


class RunLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        # Forward-compatible with ledgers created by an early Run 2 build.
        fill_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(fills)")
        }
        if "decision_id" not in fill_columns:
            self.conn.execute("ALTER TABLE fills ADD COLUMN decision_id TEXT")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RunLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def initialize_run(
        self,
        cfg: RunConfig,
        broker_account_id: str | None = None,
        started_at: str | None = None,
    ) -> None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (cfg.run_id,)).fetchone()
        if row:
            if row["config_hash"] != cfg.fingerprint:
                raise LedgerError("Run manifest changed after initialization")
            return
        self.conn.execute(
            """INSERT INTO runs
               (run_id, config_hash, config_json, starting_equity, broker_account_id, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                cfg.run_id,
                cfg.fingerprint,
                json.dumps(cfg.raw, sort_keys=True),
                str(cfg.starting_equity),
                broker_account_id,
                started_at or utc_now(),
            ),
        )
        self.conn.commit()

    def assert_manifest(self, cfg: RunConfig) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (cfg.run_id,)).fetchone()
        if not row:
            raise LedgerError(f"Run {cfg.run_id!r} is not initialized")
        if row["config_hash"] != cfg.fingerprint:
            raise LedgerError("Run manifest hash does not match the frozen manifest")
        return row

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def halt(self, run_id: str, reason: str, when: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status = 'halted', halted_at = ?, halt_reason = ? WHERE run_id = ?",
            (when or utc_now(), reason, run_id),
        )
        self.conn.commit()

    def is_halted(self, run_id: str) -> bool:
        row = self.run(run_id)
        return bool(row and row["status"] != "active")

    def record_decision(self, row: dict[str, Any]) -> str:
        decision_id = row.get("decision_id") or str(uuid4())
        values = {
            "decision_id": decision_id,
            "decided_at": utc_now(),
            "regime_score": None,
            "negative_news_z": None,
            "action": "none",
            "reason": "",
            "status": "recorded",
            **row,
        }
        self.conn.execute(
            """INSERT OR IGNORE INTO decisions
               (decision_id, run_id, portfolio, symbol, asset, strategy, decided_at,
                bar_end, signal, signal_price, notional, regime_score, negative_news_z,
                action, reason, status, config_hash)
               VALUES (:decision_id, :run_id, :portfolio, :symbol, :asset, :strategy,
                       :decided_at, :bar_end, :signal, :signal_price, :notional,
                       :regime_score, :negative_news_z, :action, :reason, :status,
                       :config_hash)""",
            values,
        )
        existing = self.conn.execute(
            """SELECT decision_id FROM decisions
               WHERE run_id=? AND portfolio=? AND symbol=? AND bar_end=? AND signal=?""",
            (
                values["run_id"], values["portfolio"], values["symbol"],
                values["bar_end"], values["signal"],
            ),
        ).fetchone()
        self.conn.commit()
        return str(existing["decision_id"] if existing else decision_id)

    def decisions(self, run_id: str, portfolio: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM decisions WHERE run_id = ?"
        args: list[Any] = [run_id]
        if portfolio:
            sql += " AND portfolio = ?"
            args.append(portfolio)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY bar_end, decided_at, symbol"
        return list(self.conn.execute(sql, args))

    def set_decision_status(self, decision_id: str, status: str, reason: str | None = None) -> None:
        if reason is None:
            self.conn.execute("UPDATE decisions SET status=? WHERE decision_id=?", (status, decision_id))
        else:
            self.conn.execute(
                "UPDATE decisions SET status=?, reason=? WHERE decision_id=?",
                (status, reason, decision_id),
            )
        self.conn.commit()

    def record_order(self, row: dict[str, Any]) -> str:
        order_pk = row.get("order_pk") or str(uuid4())
        values = {
            "order_pk": order_pk,
            "decision_id": None,
            "broker_order_id": None,
            "requested_notional": None,
            "requested_qty": None,
            "limit_price": None,
            "submitted_at": None,
            "updated_at": utc_now(),
            **row,
        }
        self.conn.execute(
            """INSERT INTO orders
               (order_pk, run_id, decision_id, portfolio, client_order_id,
                broker_order_id, symbol, asset, side, requested_notional,
                requested_qty, limit_price, status, submitted_at, updated_at)
               VALUES (:order_pk, :run_id, :decision_id, :portfolio, :client_order_id,
                       :broker_order_id, :symbol, :asset, :side, :requested_notional,
                       :requested_qty, :limit_price, :status, :submitted_at, :updated_at)
               ON CONFLICT(run_id, client_order_id) DO UPDATE SET
                   broker_order_id=COALESCE(excluded.broker_order_id, orders.broker_order_id),
                   status=excluded.status, updated_at=excluded.updated_at""",
            values,
        )
        self.conn.commit()
        found = self.conn.execute(
            "SELECT order_pk FROM orders WHERE run_id=? AND client_order_id=?",
            (values["run_id"], values["client_order_id"]),
        ).fetchone()
        return str(found["order_pk"])

    def order_by_broker_id(self, run_id: str, broker_order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM orders WHERE run_id=? AND broker_order_id=?",
            (run_id, broker_order_id),
        ).fetchone()

    def update_order_status(self, order_pk: str, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE order_pk=?",
            (status, utc_now(), order_pk),
        )
        self.conn.commit()

    def record_fill(self, row: dict[str, Any]) -> bool:
        values = {
            "order_pk": None,
            "decision_id": None,
            "broker_order_id": None,
            "simulated": 0,
            **row,
        }
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO fills
               (fill_id, run_id, order_pk, decision_id, portfolio, broker_order_id,
                symbol, asset, side, qty, price, transaction_time, simulated)
               VALUES (:fill_id, :run_id, :order_pk, :decision_id, :portfolio, :broker_order_id,
                       :symbol, :asset, :side, :qty, :price, :transaction_time, :simulated)""",
            values,
        )
        self.conn.commit()
        return cur.rowcount == 1

    def record_fee(self, row: dict[str, Any]) -> bool:
        values = {"broker_order_id": None, "symbol": None, "raw_json": "{}", **row}
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO fees
               (fee_id, run_id, broker_order_id, symbol, activity_type, amount,
                occurred_at, raw_json)
               VALUES (:fee_id, :run_id, :broker_order_id, :symbol, :activity_type,
                       :amount, :occurred_at, :raw_json)""",
            values,
        )
        self.conn.commit()
        return cur.rowcount == 1

    def fills(self, run_id: str, portfolio: str = "baseline") -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM fills WHERE run_id=? AND portfolio=?
                   ORDER BY transaction_time, fill_id""",
                (run_id, portfolio),
            )
        )

    def positions(self, run_id: str, portfolio: str = "baseline") -> dict[str, Position]:
        state: dict[str, dict[str, Any]] = {}
        for fill in self.fills(run_id, portfolio):
            symbol = fill["symbol"]
            item = state.setdefault(
                symbol,
                {"asset": fill["asset"], "qty": Decimal(0), "cost": Decimal(0), "realized": Decimal(0)},
            )
            qty, price = _d(fill["qty"]), _d(fill["price"])
            if fill["side"] == "buy":
                item["cost"] += qty * price
                item["qty"] += qty
            else:
                sold = min(qty, item["qty"])
                avg = item["cost"] / item["qty"] if item["qty"] else Decimal(0)
                item["realized"] += sold * (price - avg)
                item["qty"] -= sold
                item["cost"] -= sold * avg
                if abs(item["qty"]) < Decimal("0.000000000001"):
                    item["qty"] = Decimal(0)
                    item["cost"] = Decimal(0)
        out: dict[str, Position] = {}
        for symbol, item in state.items():
            if item["qty"] <= 0:
                continue
            out[symbol] = Position(
                portfolio=portfolio,
                symbol=symbol,
                asset=item["asset"],
                qty=item["qty"],
                avg_entry=item["cost"] / item["qty"],
                cost_basis=item["cost"],
                realized_pl=item["realized"],
            )
        return out

    def total_fees(self, run_id: str) -> Decimal:
        rows = self.conn.execute("SELECT amount FROM fees WHERE run_id=?", (run_id,))
        return sum((_d(r["amount"]) for r in rows), Decimal(0))

    def fees(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM fees WHERE run_id=? ORDER BY occurred_at, fee_id",
                (run_id,),
            )
        )

    def fill_gaps(self, run_id: str, portfolio: str) -> list[float]:
        rows = self.conn.execute(
            """SELECT f.price, d.signal_price
               FROM fills f
               JOIN decisions d ON d.decision_id = f.decision_id
               WHERE f.run_id=? AND f.portfolio=? AND f.side='buy'
                 AND CAST(d.signal_price AS REAL) > 0
               ORDER BY f.transaction_time, f.fill_id""",
            (run_id, portfolio),
        )
        return [
            (float(row["price"]) / float(row["signal_price"]) - 1) * 100
            for row in rows
        ]

    def record_equity(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO equity_snapshots
               (run_id, portfolio, captured_at, equity, cash, gross_exposure,
                drawdown_pct, source)
               VALUES (:run_id, :portfolio, :captured_at, :equity, :cash,
                       :gross_exposure, :drawdown_pct, :source)""",
            row,
        )
        self.conn.commit()

    def equity_history(self, run_id: str, portfolio: str = "baseline") -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM equity_snapshots WHERE run_id=? AND portfolio=?
                   ORDER BY captured_at""",
                (run_id, portfolio),
            )
        )

    def high_water(self, run_id: str, portfolio: str = "baseline") -> Decimal | None:
        row = self.conn.execute(
            "SELECT MAX(CAST(equity AS REAL)) AS hwm FROM equity_snapshots WHERE run_id=? AND portfolio=?",
            (run_id, portfolio),
        ).fetchone()
        return Decimal(str(row["hwm"])) if row and row["hwm"] is not None else None

    def record_feature(self, row: dict[str, Any]) -> bool:
        values = {"feature_id": str(uuid4()), "published_at": None, "symbol": None, "raw_path": None, **row}
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO feature_snapshots
               (feature_id, run_id, scope, symbol, observed_at, published_at,
                captured_at, source, name, value_json, payload_hash, raw_path)
               VALUES (:feature_id, :run_id, :scope, :symbol, :observed_at,
                       :published_at, :captured_at, :source, :name, :value_json,
                       :payload_hash, :raw_path)""",
            values,
        )
        self.conn.commit()
        return cur.rowcount == 1

    def latest_feature(
        self, run_id: str, scope: str, name: str, symbol: str | None = None
    ) -> sqlite3.Row | None:
        if symbol is None:
            return self.conn.execute(
                """SELECT * FROM feature_snapshots
                   WHERE run_id=? AND scope=? AND name=? AND symbol IS NULL
                   ORDER BY observed_at DESC, captured_at DESC LIMIT 1""",
                (run_id, scope, name),
            ).fetchone()
        return self.conn.execute(
            """SELECT * FROM feature_snapshots
               WHERE run_id=? AND scope=? AND name=? AND symbol=?
               ORDER BY observed_at DESC, captured_at DESC LIMIT 1""",
            (run_id, scope, name, symbol),
        ).fetchone()
