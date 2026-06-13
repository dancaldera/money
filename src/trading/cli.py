"""Command-line entrypoint for the backtesting lab.

Examples
--------
    money backtest --symbol BTC/USD --asset crypto --strategy sma_cross
    money backtest --symbol AAPL --asset stock --strategy rsi_meanrev
    money scan --strategy sma_cross
    money signal --symbol AAPL --asset stock
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import yaml

from .backtest import run_backtest
from .data import fetch_crypto, fetch_equity, load_or_fetch
from .live import BrokerError, PaperBroker, evaluate
from .reporting import RESULTS_DIR, record_run
from .signals import get_signal
from .strategies import STRATEGIES, get_strategy

try:  # optional: load ALPACA_* keys from a local .env if present
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}
    return {}


def get_candles(symbol: str, asset: str, timeframe: str, since: str, cfg: dict, refresh: bool):
    """Fetch (cached) OHLCV for a symbol, dispatching by asset class."""
    key = f"{asset}_{symbol}_{timeframe}_{since}"
    if asset == "crypto":
        exchange = cfg.get("crypto", {}).get("exchange", "kraken")
        return load_or_fetch(
            key,
            lambda: fetch_crypto(symbol, timeframe, since, exchange=exchange),
            refresh=refresh,
        )
    if asset == "stock":
        return load_or_fetch(
            key,
            lambda: fetch_equity(symbol, timeframe, since),
            refresh=refresh,
        )
    raise SystemExit(f"Unknown asset '{asset}' (use 'crypto' or 'stock')")


def recent_bars(symbol: str, asset: str, timeframe: str, cfg: dict):
    """Fetch fresh recent candles (uncached) for a live signal decision."""
    lookback_days = 400 if timeframe == "1d" else 45
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    if asset == "crypto":
        exchange = cfg.get("crypto", {}).get("exchange", "kraken")
        return fetch_crypto(symbol, timeframe, since, exchange=exchange)
    if asset == "stock":
        return fetch_equity(symbol, timeframe, since)
    raise SystemExit(f"Unknown asset '{asset}' (use 'crypto' or 'stock')")


def _one_backtest(symbol, asset, strategy_name, timeframe, since, cash, commission, cfg, refresh, plot):
    df = get_candles(symbol, asset, timeframe, since, cfg, refresh)
    strategy = get_strategy(strategy_name)
    plot_path = None
    if plot:
        safe = f"{asset}_{symbol.replace('/', '-')}_{strategy_name}".replace(" ", "")
        plot_path = RESULTS_DIR / f"{safe}.html"
    stats = run_backtest(df, strategy, cash=cash, commission=commission, plot_path=plot_path)
    row = record_run(
        {"symbol": symbol, "asset": asset, "strategy": strategy_name,
         "timeframe": timeframe, "since": since},
        stats,
    )
    return row, plot_path


def _fmt_row(row: dict) -> str:
    return (
        f"  trades={row['trades']:<4} "
        f"win={row['win_rate_pct']:>6.2f}%  "
        f"return={row['return_pct']:>8.2f}%  "
        f"buy&hold={row['buy_hold_pct']:>8.2f}%  "
        f"maxDD={row['max_drawdown_pct']:>7.2f}%  "
        f"sharpe={row['sharpe']:>6.3f}"
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_backtest(args, cfg):
    d = cfg.get("defaults", {})
    timeframe = args.timeframe or d.get("timeframe", "1d")
    since = args.since or d.get("since", "2022-01-01")
    cash = args.cash or d.get("cash", 10_000)
    commission = d.get("commission", 0.002)

    print(f"\nBacktesting {args.strategy} on {args.symbol} ({args.asset}, {timeframe}, since {since})")
    row, plot_path = _one_backtest(
        args.symbol, args.asset, args.strategy, timeframe, since,
        cash, commission, cfg, args.refresh, plot=not args.no_plot,
    )
    print(_fmt_row(row))
    print(f"  final equity: ${row['final_equity']:,.2f}")
    if plot_path:
        print(f"  chart: {plot_path}")
    print(f"  logged to: {RESULTS_DIR / 'journal.csv'}\n")


def cmd_scan(args, cfg):
    d = cfg.get("defaults", {})
    timeframe = args.timeframe or d.get("timeframe", "1d")
    since = args.since or d.get("since", "2022-01-01")
    cash = d.get("cash", 10_000)
    commission = d.get("commission", 0.002)

    targets: list[tuple[str, str]] = []
    if args.asset in (None, "crypto"):
        targets += [(s, "crypto") for s in cfg.get("crypto", {}).get("symbols", [])]
    if args.asset in (None, "stock"):
        targets += [(s, "stock") for s in cfg.get("stocks", {}).get("symbols", [])]

    if not targets:
        raise SystemExit("No symbols found in config/settings.yaml")

    print(f"\nScanning {args.strategy} across {len(targets)} symbols ({timeframe}, since {since})\n")
    results = []
    for symbol, asset in targets:
        try:
            row, _ = _one_backtest(
                symbol, asset, args.strategy, timeframe, since,
                cash, commission, cfg, args.refresh, plot=False,
            )
            results.append((symbol, asset, row))
            print(f"{symbol:<10} [{asset:<6}]{_fmt_row(row)}")
        except Exception as e:  # noqa: BLE001 — keep scanning other symbols
            print(f"{symbol:<10} [{asset:<6}]  ERROR: {e}")

    if results:
        results.sort(key=lambda r: r[2]["return_pct"], reverse=True)
        best = results[0]
        print(f"\nBest return: {best[0]} ({best[1]}) at {best[2]['return_pct']:.2f}%")
    print(f"All runs logged to: {RESULTS_DIR / 'journal.csv'}\n")


def cmd_signal(args, cfg):
    exchange = None
    if args.asset == "crypto":
        exchange = cfg.get("crypto", {}).get("tradingview_exchange", "BINANCE")
    sig = get_signal(args.symbol, args.asset, interval=args.timeframe or "1d", exchange=exchange)
    print(f"\nTradingView signal for {sig['symbol']} on {sig['exchange']}:")
    print(f"  RECOMMENDATION: {sig['recommendation']}")
    print(f"  buy={sig['buy']}  sell={sig['sell']}  neutral={sig['neutral']}\n")


# --------------------------------------------------------------------------- #
# Paper trading (Alpaca paper account — fake money)
# --------------------------------------------------------------------------- #
def cmd_paper_status(args, cfg):
    broker = PaperBroker()
    acct = broker.account()
    print("\nPaper account (Alpaca — fake money):")
    print(f"  equity:        ${acct['equity']:,.2f}")
    print(f"  cash:          ${acct['cash']:,.2f}")
    print(f"  buying power:  ${acct['buying_power']:,.2f}")

    positions = broker.positions()
    if not positions:
        print("  positions:     none\n")
        return
    print("  positions:")
    for p in positions:
        print(
            f"    {p['symbol']:<10} qty={p['qty']:<12.6f} "
            f"value=${p['market_value'] or 0:,.2f}  "
            f"P&L=${p['unrealized_pl']:,.2f} ({p['unrealized_plpc']:+.2f}%)"
        )
    print()


def cmd_paper_run(args, cfg):
    d = cfg.get("defaults", {})
    timeframe = args.timeframe or d.get("timeframe", "1d")
    notional = args.notional or cfg.get("paper", {}).get("notional", 1000)

    broker = PaperBroker()
    bars = recent_bars(args.symbol, args.asset, timeframe, cfg)
    res = evaluate(
        broker, args.symbol, args.asset, args.strategy, bars,
        notional=notional, dry_run=args.dry_run,
    )
    mode = " (dry run)" if args.dry_run else ""
    print(f"\nPaper evaluate{mode}: {args.strategy} on {args.symbol} ({args.asset}, {timeframe})")
    print(f"  last price: ${res['last_price']:,.2f}")
    print(f"  signal:     {res['signal']}   (currently holding: {res['holding']})")
    print(f"  action:     {res['action']}")
    if res["order_id"]:
        print(f"  order id:   {res['order_id']}")
    print()


def cmd_paper_close(args, cfg):
    broker = PaperBroker()
    if not broker.is_holding(args.symbol):
        print(f"\nNo open paper position for {args.symbol}.\n")
        return
    order_id = broker.close(args.symbol)
    print(f"\nClosed paper position {args.symbol} (order {order_id}).\n")


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="money", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    strat_choices = sorted(STRATEGIES)

    bt = sub.add_parser("backtest", help="Backtest one strategy on one symbol")
    bt.add_argument("--symbol", required=True, help="e.g. BTC/USD or AAPL")
    bt.add_argument("--asset", required=True, choices=["crypto", "stock"])
    bt.add_argument("--strategy", required=True, choices=strat_choices)
    bt.add_argument("--timeframe", help="1d or 1h (default from config)")
    bt.add_argument("--since", help="start date YYYY-MM-DD (default from config)")
    bt.add_argument("--cash", type=float, help="starting capital (default from config)")
    bt.add_argument("--no-plot", action="store_true", help="skip saving the HTML chart")
    bt.add_argument("--refresh", action="store_true", help="ignore cache, refetch data")
    bt.set_defaults(func=cmd_backtest)

    sc = sub.add_parser("scan", help="Run a strategy across the watchlist")
    sc.add_argument("--strategy", required=True, choices=strat_choices)
    sc.add_argument("--asset", choices=["crypto", "stock"], help="limit to one asset class")
    sc.add_argument("--timeframe", help="1d or 1h (default from config)")
    sc.add_argument("--since", help="start date YYYY-MM-DD (default from config)")
    sc.add_argument("--refresh", action="store_true", help="ignore cache, refetch data")
    sc.set_defaults(func=cmd_scan)

    sg = sub.add_parser("signal", help="TradingView BUY/SELL recommendation for a symbol")
    sg.add_argument("--symbol", required=True)
    sg.add_argument("--asset", required=True, choices=["crypto", "stock"])
    sg.add_argument("--timeframe", help="1d, 1h, 4h, 1w (default 1d)")
    sg.set_defaults(func=cmd_signal)

    ps = sub.add_parser("paper-status", help="Show Alpaca paper account & positions")
    ps.set_defaults(func=cmd_paper_status)

    pr = sub.add_parser("paper-run", help="Evaluate a strategy now and place a paper order if signaled")
    pr.add_argument("--symbol", required=True, help="e.g. BTC/USD or AAPL")
    pr.add_argument("--asset", required=True, choices=["crypto", "stock"])
    pr.add_argument("--strategy", required=True, choices=strat_choices)
    pr.add_argument("--timeframe", help="1d or 1h (default from config)")
    pr.add_argument("--notional", type=float, help="$ per new position (default from config)")
    pr.add_argument("--dry-run", action="store_true", help="show the action but place no order")
    pr.set_defaults(func=cmd_paper_run)

    pc = sub.add_parser("paper-close", help="Close an open paper position")
    pc.add_argument("--symbol", required=True)
    pc.set_defaults(func=cmd_paper_close)

    return p


def main(argv=None):
    if load_dotenv is not None:
        load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()
    try:
        args.func(args, cfg)
    except BrokerError as e:
        raise SystemExit(f"\nPaper trading error: {e}\n")


if __name__ == "__main__":
    main()
