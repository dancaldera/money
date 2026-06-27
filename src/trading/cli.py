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

import pandas as pd
import yaml

from .backtest import run_backtest
from .data import drop_forming_bar, fetch_crypto, fetch_equity, load_or_fetch
from .live import BrokerError, PaperBroker, evaluate, stop_breached
from .reporting import RESULTS_DIR, record_paper_action, record_run, summarize
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
        return load_or_fetch(
            key,
            lambda: fetch_crypto(symbol, timeframe, since),
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
    """Fetch fresh recent candles (uncached) for a live signal decision.

    Drops the still-forming current bar so the signal is computed on the last
    *closed* candle, matching the backtest (which only acts on closed bars).
    """
    lookback_days = 400 if timeframe == "1d" else 45
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    if asset == "crypto":
        bars = fetch_crypto(symbol, timeframe, since)
    elif asset == "stock":
        bars = fetch_equity(symbol, timeframe, since)
    else:
        raise SystemExit(f"Unknown asset '{asset}' (use 'crypto' or 'stock')")
    return drop_forming_bar(bars, timeframe)


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


def _window_alpha(df, strategy, cash, commission) -> dict | None:
    """Backtest ``strategy`` over ``df`` and return its stats plus alpha vs B&H."""
    if len(df) < 60:  # need warmup + a few trades to mean anything
        return None
    stats = summarize(run_backtest(df, strategy, cash=cash, commission=commission))
    stats["alpha"] = round(stats["return_pct"] - stats["buy_hold_pct"], 2)
    return stats


def cmd_validate(args, cfg):
    """Compare strategies out-of-sample: train vs a held-out test window.

    The strategies have fixed (non-fitted) parameters, so this is a held-out
    consistency check rather than a parameter optimisation: a strategy is only
    trustworthy if it beats buy & hold (positive alpha) in BOTH windows. Scoring
    is on alpha (return - buy&hold), never raw return, so a rising market alone
    doesn't look like skill.
    """
    d = cfg.get("defaults", {})
    timeframe = args.timeframe or d.get("timeframe", "1d")
    train_start = args.train_start or d.get("since", "2022-01-01")
    split = args.split
    cash = d.get("cash", 10_000)
    commission = d.get("commission", 0.002)
    split_ts = pd.Timestamp(split)

    targets: list[tuple[str, str]] = []
    if args.asset in (None, "crypto"):
        targets += [(s, "crypto") for s in cfg.get("crypto", {}).get("symbols", [])]
    if args.asset in (None, "stock"):
        targets += [(s, "stock") for s in cfg.get("stocks", {}).get("symbols", [])]
    if not targets:
        raise SystemExit("No symbols found in config/settings.yaml")

    strategies = [args.strategy] if args.strategy else sorted(STRATEGIES)

    print(f"\nOut-of-sample validation ({timeframe})")
    print(f"  train: {train_start} -> {split}    test: {split} -> latest")
    print("  metric: alpha = strategy return - buy&hold return (per window)\n")

    summary: dict[str, dict] = {}
    for strat in strategies:
        strategy = get_strategy(strat)
        print(f"== {strat} ==")
        print(f"  {'symbol':<10} {'train α':>8} {'test α':>8}   verdict")
        test_alphas: list[float] = []
        beats_both = 0
        for symbol, asset in targets:
            try:
                df = get_candles(symbol, asset, timeframe, train_start, cfg, args.refresh)
                tr = _window_alpha(df[df.index < split_ts], strategy, cash, commission)
                te = _window_alpha(df[df.index >= split_ts], strategy, cash, commission)
                if tr is None or te is None:
                    print(f"  {symbol:<10}   (insufficient history, skipped)")
                    continue
                both = tr["alpha"] > 0 and te["alpha"] > 0
                beats_both += int(both)
                test_alphas.append(te["alpha"])
                verdict = (
                    "robust (both)" if both
                    else "test only" if te["alpha"] > 0
                    else "train only" if tr["alpha"] > 0
                    else "underperforms"
                )
                print(f"  {symbol:<10} {tr['alpha']:>7.1f}% {te['alpha']:>7.1f}%   {verdict}")
            except Exception as e:  # noqa: BLE001
                print(f"  {symbol:<10}   ERROR: {e}")
        if test_alphas:
            mean_te = round(sum(test_alphas) / len(test_alphas), 1)
            summary[strat] = {"mean_test_alpha": mean_te, "beats_both": beats_both, "n": len(test_alphas)}
            print(f"  -> mean test alpha {mean_te:+.1f}%   robust in both windows: {beats_both}/{len(test_alphas)}\n")
        else:
            print()

    if summary:
        print("Verdict (ranked by out-of-sample / test alpha):")
        ranked = sorted(summary.items(), key=lambda kv: kv[1]["mean_test_alpha"], reverse=True)
        for strat, s in ranked:
            print(f"  {strat:<12} mean test alpha {s['mean_test_alpha']:+6.1f}%   robust {s['beats_both']}/{s['n']}")
        best_strat, best = ranked[0]
        if best["mean_test_alpha"] <= 0:
            print(f"\nNo strategy beats buy & hold out-of-sample (best is {best_strat} at "
                  f"{best['mean_test_alpha']:+.1f}%). Honest answer: no edge here yet.")
        else:
            print(f"\nBest out-of-sample: {best_strat} ({best['mean_test_alpha']:+.1f}% mean test alpha, "
                  f"robust {best['beats_both']}/{best['n']}).")
    print()


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

    stop_loss = args.stop_loss if args.stop_loss is not None else cfg.get("paper", {}).get("stop_loss_pct", 0)

    broker = PaperBroker()
    bars = recent_bars(args.symbol, args.asset, timeframe, cfg)
    res = evaluate(
        broker, args.symbol, args.asset, args.strategy, bars,
        notional=notional, stop_loss_pct=stop_loss, dry_run=args.dry_run,
    )
    mode = " (dry run)" if args.dry_run else ""
    print(f"\nPaper evaluate{mode}: {args.strategy} on {args.symbol} ({args.asset}, {timeframe})")
    print(f"  last price: ${res['last_price']:,.2f}")
    pl = f"  (P&L {res['plpc']:+.2f}%)" if res["plpc"] is not None else ""
    print(f"  signal:     {res['signal']}   (currently holding: {res['holding']}){pl}")
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


def _config_symbol_index(cfg: dict) -> dict[str, tuple[str, str]]:
    """Map Alpaca's slash-less position symbol -> (config symbol, asset class).

    Positions come back as ``BTCUSD``/``AAPL``; this lets us recover the original
    ``BTC/USD`` form and the asset class for logging and closing.
    """
    index: dict[str, tuple[str, str]] = {}
    for s in cfg.get("crypto", {}).get("symbols", []):
        index[s.replace("/", "")] = (s, "crypto")
    for s in cfg.get("stocks", {}).get("symbols", []):
        index[s.replace("/", "")] = (s, "stock")
    return index


def cmd_paper_stops(args, cfg):
    """Check open positions against the stop-loss and close any that breach it.

    Unlike ``paper-scan`` this does no signalling or data-fetching — it just reads
    each position's live P&L from Alpaca and cuts losers. It's cheap enough to run
    frequently (e.g. every 30 min) so a falling position is stopped intraday
    rather than waiting for the once-a-day signal scan.
    """
    stop_loss = args.stop_loss if args.stop_loss is not None else cfg.get("paper", {}).get("stop_loss_pct", 0)
    broker = PaperBroker()
    positions = broker.positions()
    index = _config_symbol_index(cfg)

    mode = " (dry run)" if args.dry_run else ""
    print(f"\nStop-loss monitor{mode}: threshold {stop_loss}% across {len(positions)} open position(s)")
    if not stop_loss:
        print("  stop-loss disabled (stop_loss_pct=0); nothing to do.\n")
        return
    if not positions:
        print("  no open positions.\n")
        return

    stopped = 0
    for p in positions:
        symbol, asset = index.get(p["symbol"], (p["symbol"], ""))
        plpc = p["unrealized_plpc"]
        breached = stop_breached(plpc, stop_loss)
        action, order_id = "none", None
        if breached:
            action = "would_stop" if args.dry_run else "stopped"
            if not args.dry_run:
                order_id = broker.close(symbol)
            stopped += 1
        record_paper_action(
            {
                "symbol": symbol, "asset": asset, "strategy": "stop-monitor",
                "signal": "STOP" if breached else "-", "holding": True,
                "action": action, "order_id": order_id,
                "last_price": round(p["current_price"], 2) if p["current_price"] else "",
            }
        )
        flag = "  <-- STOP" if breached else ""
        print(f"  {symbol:<10} [{asset:<6}] P&L={plpc:+.2f}%  action={action}{flag}")

    verb = "would be closed" if args.dry_run else "closed"
    print(f"\n{stopped} position(s) {verb}. Logged to: {RESULTS_DIR / 'paper_journal.csv'}\n")


def cmd_paper_scan(args, cfg):
    """Evaluate one strategy across the whole watchlist and act on each signal."""
    d = cfg.get("defaults", {})
    timeframe = args.timeframe or d.get("timeframe", "1d")
    notional = args.notional or cfg.get("paper", {}).get("notional", 1000)
    stop_loss = args.stop_loss if args.stop_loss is not None else cfg.get("paper", {}).get("stop_loss_pct", 0)

    targets: list[tuple[str, str]] = []
    if args.asset in (None, "crypto"):
        targets += [(s, "crypto") for s in cfg.get("crypto", {}).get("symbols", [])]
    if args.asset in (None, "stock"):
        targets += [(s, "stock") for s in cfg.get("stocks", {}).get("symbols", [])]
    if not targets:
        raise SystemExit("No symbols found in config/settings.yaml")

    broker = PaperBroker()
    mode = " (dry run)" if args.dry_run else ""
    print(f"\nPaper scan{mode}: {args.strategy} across {len(targets)} symbols ({timeframe})")
    acted = 0
    for symbol, asset in targets:
        try:
            bars = recent_bars(symbol, asset, timeframe, cfg)
            res = evaluate(
                broker, symbol, asset, args.strategy, bars,
                notional=notional, stop_loss_pct=stop_loss, dry_run=args.dry_run,
            )
            record_paper_action(res)
            flag = "" if res["action"] == "none" else "  <-- ACTION"
            print(
                f"  {symbol:<10} [{asset:<6}] signal={res['signal']:<4} "
                f"holding={str(res['holding']):<5} action={res['action']}{flag}"
            )
            if res["action"] != "none":
                acted += 1
        except Exception as e:  # noqa: BLE001 — keep scanning other symbols
            print(f"  {symbol:<10} [{asset:<6}] ERROR: {e}")

    verb = "would be placed" if args.dry_run else "placed"
    print(f"\n{acted} order(s) {verb}. Logged to: {RESULTS_DIR / 'paper_journal.csv'}\n")


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

    vl = sub.add_parser("validate", help="Out-of-sample train/test comparison of strategies vs buy & hold")
    vl.add_argument("--strategy", choices=strat_choices, help="limit to one strategy (default: all)")
    vl.add_argument("--asset", choices=["crypto", "stock"], help="limit to one asset class")
    vl.add_argument("--timeframe", help="1d or 1h (default from config)")
    vl.add_argument("--train-start", help="train window start YYYY-MM-DD (default from config 'since')")
    vl.add_argument("--split", default="2025-01-01", help="train/test boundary YYYY-MM-DD (default 2025-01-01)")
    vl.add_argument("--refresh", action="store_true", help="ignore cache, refetch data")
    vl.set_defaults(func=cmd_validate)

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
    pr.add_argument("--stop-loss", type=float, help="close if position falls this %% (default from config; 0=off)")
    pr.add_argument("--dry-run", action="store_true", help="show the action but place no order")
    pr.set_defaults(func=cmd_paper_run)

    pc = sub.add_parser("paper-close", help="Close an open paper position")
    pc.add_argument("--symbol", required=True)
    pc.set_defaults(func=cmd_paper_close)

    pscan = sub.add_parser("paper-scan", help="Run a strategy across the watchlist on the paper account")
    pscan.add_argument("--strategy", required=True, choices=strat_choices)
    pscan.add_argument("--asset", choices=["crypto", "stock"], help="limit to one asset class")
    pscan.add_argument("--timeframe", help="1d or 1h (default from config)")
    pscan.add_argument("--notional", type=float, help="$ per new position (default from config)")
    pscan.add_argument("--stop-loss", type=float, help="close if position falls this %% (default from config; 0=off)")
    pscan.add_argument("--dry-run", action="store_true", help="show actions but place no orders")
    pscan.set_defaults(func=cmd_paper_scan)

    pstops = sub.add_parser("paper-stops", help="Check open positions and close any breaching the stop-loss (run often)")
    pstops.add_argument("--stop-loss", type=float, help="close if position falls this %% (default from config; 0=off)")
    pstops.add_argument("--dry-run", action="store_true", help="show breaches but close nothing")
    pstops.set_defaults(func=cmd_paper_stops)

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
