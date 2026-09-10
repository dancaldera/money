"""Coverage for trading.data.alpaca and trading.live.broker (fully mocked)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trading.data import alpaca as alpaca_mod
from trading.data.alpaca import AlpacaDataError, _as_utc, _credentials, _frame, fetch_alpaca_daily
from trading.live import broker as broker_mod
from trading.live.broker import BrokerError, PaperBroker, _position_symbol


def _bars_df(n=3, start="2026-08-20"):
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {"open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n,
         "close": [10.5] * n, "volume": [100.0] * n},
        index=idx,
    )


# --- data/alpaca.py ----------------------------------------------------------- #
def test_position_symbol_strips_slash():
    assert _position_symbol("BTC/USD") == "BTCUSD"


def test_credentials_require_both_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(AlpacaDataError, match="required"):
        _credentials()
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    with pytest.raises(AlpacaDataError, match="required"):
        _credentials()
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert _credentials() == ("k", "s")


def test_as_utc_branches():
    assert _as_utc(None) is None
    naive = _as_utc("2026-01-01")
    assert naive.tzinfo is not None
    aware = _as_utc("2026-01-01T00:00:00-05:00")
    assert aware.tzinfo is not None and aware.hour == 5
    assert _as_utc(datetime(2026, 1, 1)).tzinfo is not None


def test_frame_rejects_missing_or_empty_bars():
    with pytest.raises(AlpacaDataError, match="no daily bars"):
        _frame(SimpleNamespace(df=None), "AAPL")
    with pytest.raises(AlpacaDataError, match="no daily bars"):
        _frame(SimpleNamespace(df=pd.DataFrame()), "AAPL")


def test_frame_falls_back_to_level_zero_index():
    index = pd.MultiIndex.from_product(
        [["AAPL"], pd.to_datetime(["2026-08-21T04:00:00Z", "2026-08-22T04:00:00Z"])],
        names=["ticker", "timestamp"],  # no "symbol" level -> KeyError -> level-0 fallback
    )
    result = SimpleNamespace(df=pd.DataFrame(
        {"open": [1, 2], "high": [2, 3], "low": [0.5, 1.5],
         "close": [1.5, 2.5], "volume": [10, 20]},
        index=index,
    ))
    out = _frame(result, "AAPL")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(out) == 2


def test_frame_rejects_missing_columns():
    df = _bars_df().drop(columns=["volume"])
    with pytest.raises(AlpacaDataError, match="missing"):
        _frame(SimpleNamespace(df=df), "AAPL")


def test_frame_rejects_all_incomplete_bars():
    df = _bars_df()
    df.loc[:, :] = float("nan")
    with pytest.raises(AlpacaDataError, match="no complete daily bars"):
        _frame(SimpleNamespace(df=df), "AAPL")


def _patch_data_clients(monkeypatch, df):
    stock = MagicMock()
    stock.get_stock_bars.return_value = SimpleNamespace(df=df)
    crypto = MagicMock()
    crypto.get_crypto_bars.return_value = SimpleNamespace(df=df)
    monkeypatch.setattr(alpaca_mod, "StockHistoricalDataClient", MagicMock(return_value=stock))
    monkeypatch.setattr(alpaca_mod, "CryptoHistoricalDataClient", MagicMock(return_value=crypto))
    return stock, crypto


def test_fetch_stock_and_crypto_paths(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    stock, crypto = _patch_data_clients(monkeypatch, _bars_df())
    out = fetch_alpaca_daily("AAPL", "stock", since="2026-01-01")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert out.index.tz is None and out.index.name == "Date"
    stock.get_stock_bars.assert_called_once()
    out = fetch_alpaca_daily("BTC/USD", "crypto", until="2026-08-24T00:00:00+00:00")
    assert len(out) == 3
    crypto.get_crypto_bars.assert_called_once()


def test_fetch_requires_credentials(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(AlpacaDataError, match="required"):
        fetch_alpaca_daily("AAPL", "stock")


def test_fetch_rejects_unknown_asset(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(AlpacaDataError, match="Unknown asset"):
        fetch_alpaca_daily("X", "option")


# --- live/broker.py ----------------------------------------------------------- #
def _account_ns():
    return SimpleNamespace(id="acct-1", equity="100000", cash="90000",
                           buying_power="180000", portfolio_value="100500")


def _clock_ns():
    return SimpleNamespace(
        is_open=True,
        timestamp=datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc),
        next_open=datetime(2026, 8, 25, 13, 30, tzinfo=timezone.utc),
        next_close=datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
    )


def _position_ns(symbol="AAPL", qty="10", plpc="5.0", price="110", mv="1100", pl="50"):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price="105",
                           current_price=price, market_value=mv,
                           unrealized_pl=pl, unrealized_plpc=plpc)


class _FakeTradingClient:
    def __init__(self, *args, **kwargs):
        assert kwargs.get("paper") is True
        self.positions_list = []
        self.orders_list = []
        self.submitted = []
        self.closed_syms = []
        self.get_pages = []
        self.get_calls = []

    def get_account(self):
        return _account_ns()

    def get_clock(self):
        return _clock_ns()

    def get_all_positions(self):
        return self.positions_list

    def get_orders(self, req):
        return self.orders_list

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="order-9", client_order_id="c9", symbol="X",
                               status="accepted", side="buy", submitted_at=None,
                               filled_at=None, filled_qty=None, filled_avg_price=None)

    def close_position(self, symbol):
        self.closed_syms.append(symbol)
        return SimpleNamespace(id="close-9")

    def get(self, path, data):
        self.get_calls.append(dict(data))
        return self.get_pages.pop(0) if self.get_pages else []


@pytest.fixture
def fake_clients(monkeypatch):
    trading = _FakeTradingClient(paper=True)
    monkeypatch.setattr(broker_mod, "TradingClient", lambda *a, **k: trading)
    monkeypatch.setattr(broker_mod, "StockHistoricalDataClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr(broker_mod, "CryptoHistoricalDataClient", lambda *a, **k: MagicMock())
    return trading


def test_broker_init_requires_keys(monkeypatch, fake_clients):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(BrokerError, match="not found"):
        PaperBroker()
    b = PaperBroker(api_key="k", secret_key="s")
    assert b.client is not None


def test_broker_init_reads_env(monkeypatch, fake_clients):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert PaperBroker().client is not None


def test_account_clock_positions(fake_clients):
    b = PaperBroker(api_key="k", secret_key="s")
    acct = b.account()
    assert acct == {"id": "acct-1", "equity": 100000.0, "cash": 90000.0,
                    "buying_power": 180000.0, "portfolio_value": 100500.0}
    clock = b.clock()
    assert clock["is_open"] is True and clock["timestamp"].startswith("2026-08-24")
    fake_clients.positions_list = [
        _position_ns(),
        _position_ns(symbol="BTCUSD", qty="0.5", plpc=None, price=None, mv=None, pl=None),
    ]
    positions = b.positions()
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["unrealized_plpc"] == 500.0
    assert positions[1]["current_price"] is None
    assert positions[1]["market_value"] is None
    assert positions[1]["unrealized_pl"] == 0.0
    assert positions[1]["unrealized_plpc"] == 0.0


def test_holding_open_order_plpc(fake_clients):
    b = PaperBroker(api_key="k", secret_key="s")
    fake_clients.positions_list = [_position_ns()]
    assert b.is_holding("AAPL") is True
    assert b.is_holding("BTC/USD") is False
    fake_clients.orders_list = [SimpleNamespace(symbol="BTCUSD")]
    assert b.has_open_order("BTC/USD") is True
    assert b.has_open_order("AAPL") is False
    assert b.position_plpc("AAPL") == 500.0
    assert b.position_plpc("MSFT") is None
    fake_clients.positions_list = [_position_ns(plpc=None)]
    assert b.position_plpc("AAPL") == 0.0


def test_latest_price_both_asset_classes(monkeypatch, fake_clients):
    stock_data, crypto_data = MagicMock(), MagicMock()
    monkeypatch.setattr(broker_mod, "StockHistoricalDataClient", lambda *a, **k: stock_data)
    monkeypatch.setattr(broker_mod, "CryptoHistoricalDataClient", lambda *a, **k: crypto_data)
    stock_data.get_stock_latest_trade.return_value = {"AAPL": SimpleNamespace(price="150.5")}
    crypto_data.get_crypto_latest_trade.return_value = {"BTC/USD": SimpleNamespace(price=60000)}
    b = PaperBroker(api_key="k", secret_key="s")
    assert b.latest_price("AAPL", "stock") == 150.5
    assert b.latest_price("BTC/USD", "crypto") == 60000.0


def _order_ns(**over):
    base = dict(id="o1", client_order_id="c1", symbol="AAPL",
                status=SimpleNamespace(value="filled"), side=SimpleNamespace(value="buy"),
                submitted_at=datetime(2026, 8, 24, 14, tzinfo=timezone.utc),
                filled_at=None, filled_qty=None, filled_avg_price=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_all_orders_uses_order_dict(fake_clients):
    fake_clients.orders_list = [
        _order_ns(),
        _order_ns(id="o2", status="canceled", side="sell",
                  filled_at=datetime(2026, 8, 24, 15, tzinfo=timezone.utc),
                  filled_qty="2", filled_avg_price="101.5"),
    ]
    b = PaperBroker(api_key="k", secret_key="s")
    out = b.all_orders()
    assert out[0]["status"] == "filled" and out[0]["side"] == "buy"
    assert out[0]["filled_at"] is None and out[0]["filled_qty"] == 0.0
    assert out[1]["status"] == "canceled" and out[1]["side"] == "sell"
    assert out[1]["filled_avg_price"] == 101.5


def test_activities_short_page_and_after_truncation(fake_clients):
    b = PaperBroker(api_key="k", secret_key="s")
    fake_clients.get_pages = [[{"id": "a1"}, {"id": "a2"}]]
    out = b.activities("FILL", after="2026-08-24T10:00:00+00:00")
    assert [a["id"] for a in out] == ["a1", "a2"]
    assert fake_clients.get_calls[0]["after"] == "2026-08-24"
    fake_clients.get_pages = ["not-a-list"]
    assert b.activities("FILL") == []


def test_activities_paginates_until_short_page(fake_clients):
    b = PaperBroker(api_key="k", secret_key="s")
    page1 = [{"id": f"p1-{i}"} for i in range(100)]
    page2 = [{"id": "last"}]
    fake_clients.get_pages = [page1, page2]
    out = b.activities("FILL")
    assert len(out) == 101
    assert fake_clients.get_calls[1]["page_token"] == "p1-99"


def test_activities_stops_on_empty_or_repeated_token(fake_clients):
    b = PaperBroker(api_key="k", secret_key="s")
    full_no_id = [{"x": 1} for _ in range(100)]
    fake_clients.get_pages = [full_no_id]
    assert len(b.activities("FILL")) == 100
    same_token = [[{"id": "tok"}] * 100, [{"id": "tok"}] * 100]
    fake_clients.get_pages = same_token
    assert len(b.activities("FILL")) == 200  # second page repeats the token -> stop


def test_buy_notional_time_in_force(fake_clients):
    from alpaca.trading.enums import TimeInForce

    b = PaperBroker(api_key="k", secret_key="s")
    assert b.buy_notional("BTCUSD", 625, "crypto") == "order-9"
    assert b.buy_notional("AAPL", 625, "stock") == "order-9"
    assert fake_clients.submitted[0].time_in_force == TimeInForce.GTC
    assert fake_clients.submitted[1].time_in_force == TimeInForce.DAY


def test_buy_limit_time_in_force(fake_clients):
    from alpaca.trading.enums import TimeInForce

    b = PaperBroker(api_key="k", secret_key="s")
    out = b.buy_limit("BTCUSD", 625, "crypto", 60000.123456789, "c1")
    assert out["id"] == "order-9"
    out = b.buy_limit("AAPL", 625, "stock", 100.456, "c2")
    assert fake_clients.submitted[0].time_in_force == TimeInForce.IOC
    assert fake_clients.submitted[1].time_in_force == TimeInForce.DAY
    assert fake_clients.submitted[1].limit_price == 100.46


def test_close_strips_slash(fake_clients):
    b = PaperBroker(api_key="k", secret_key="s")
    assert b.close("BTC/USD") == "close-9"
    assert fake_clients.closed_syms == ["BTCUSD"]
