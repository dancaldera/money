from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from trading.run2.config import load_run_config
from trading.run2.ledger import RunLedger


ROOT = Path(__file__).resolve().parents[1]


def run2_config():
    return load_run_config(ROOT / "config" / "run2.yaml")


def initialized_ledger(tmp_path):
    cfg = run2_config()
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.initialize_run(cfg, "paper-account", "2026-08-24T00:00:00+00:00")
    return cfg, ledger


def ohlcv(close, start="2026-01-01", opens=None):
    close = np.asarray(close, dtype=float)
    opens = close.copy() if opens is None else np.asarray(opens, dtype=float)
    index = pd.date_range(start, periods=len(close), freq="D")
    return pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, close) * 1.01,
            "Low": np.minimum(opens, close) * 0.99,
            "Close": close,
            "Volume": np.ones(len(close)),
        },
        index=index,
    )


def fresh_buy_frame():
    return ohlcv([10.0] * 30 + [9.0, 20.0])


def add_fill(ledger, cfg, symbol, asset, side, qty, price, suffix, portfolio="baseline"):
    return ledger.record_fill(
        {
            "fill_id": suffix,
            "run_id": cfg.run_id,
            "portfolio": portfolio,
            "symbol": symbol,
            "asset": asset,
            "side": side,
            "qty": str(qty),
            "price": str(price),
            "transaction_time": f"2026-08-24T00:00:{suffix[-2:].zfill(2)}+00:00",
            "simulated": int(portfolio != "baseline"),
        }
    )
