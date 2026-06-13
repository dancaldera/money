# money — strategy backtesting & learning lab

A small Python project to **learn how trading strategies behave** on real historical
data for **crypto** and **North-American stocks** (NASDAQ/NYSE). You define an
"assumption" (a rule-based strategy), run it over history, and the tool reports your
**Wins & Losses**: win rate, profit/loss, drawdown, and an equity curve. Every run is
logged so you can compare strategies and improve over time.

> ⚠️ **Educational / simulation only.** This project **never places real orders and
> never moves real money.** It downloads *historical* prices and simulates trades with
> fake capital (backtesting). There are no exchange/broker API keys with trading or
> withdrawal rights anywhere in this code. Backtest results are **not** predictions and
> past performance does not guarantee future results. Do your own research before
> risking real money.

## What's inside

```
src/trading/
  data/        fetch candles: ccxt (crypto) + yfinance (stocks), cached as parquet
  signals/     tradingview-ta wrapper -> TradingView's BUY/SELL/NEUTRAL recommendation
  strategies/  your "assumptions": sma_cross, rsi_meanrev (add your own here)
  backtest/    runs a strategy and computes the W&L metrics
  reporting/   logs every run to results/journal.csv + saves an equity-curve chart
  cli.py       command-line entrypoint
```

## Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .          # installs deps and the `money` command
```

## Usage

```bash
# Backtest one crypto pair
money backtest --symbol BTC/USD --asset crypto --strategy sma_cross

# Backtest one stock
money backtest --symbol AAPL --asset stock --strategy rsi_meanrev

# Run a strategy across the whole watchlist in config/settings.yaml
money scan --strategy sma_cross

# Get TradingView's live indicator recommendation for a symbol
money signal --symbol AAPL --asset stock
```

(You can also run it without installing: `python -m trading.cli ...` from `src/`,
or use the provided `.venv`: `.venv/bin/python -m trading.cli ...`.)

Results land in `results/`:
- `journal.csv` — one row per backtest (symbol, strategy, win rate, return, drawdown…)
- `<symbol>_<strategy>.html` — interactive equity-curve / trades chart

## Paper trading (live prices, fake money)

Once a strategy looks good in backtests, you can run it against **real-time prices
on an Alpaca paper account** — fake money, real order mechanics, zero financial risk.

> The Alpaca client is hardcoded to `paper=True`. There is no code path to live
> trading and no funding/withdrawal permissions are ever requested.

**One-time setup (free):**
1. Create an account at <https://alpaca.markets>.
2. Toggle to **Paper Trading** in the dashboard (top-left).
3. Generate an API key (**Home → API Keys → Generate**).
4. `cp .env.example .env` and paste your **paper** key + secret into `.env`
   (it's gitignored, so keys stay local).

**Commands:**
```bash
# Account equity, buying power, and open positions with live P&L
money paper-status

# Evaluate a strategy on fresh prices; place a paper order only if it signals
money paper-run --symbol AAPL --asset stock --strategy rsi_meanrev
money paper-run --symbol BTC/USD --asset crypto --strategy sma_cross --dry-run

# Manually close an open paper position
money paper-close --symbol AAPL
```

`paper-run` fetches recent bars (via the same yfinance/ccxt fetchers), computes the
strategy's BUY/SELL/HOLD signal on the latest bar, checks whether you already hold the
asset, and submits a market order to the **paper** account when warranted. Use `--dry-run`
to preview the decision without sending anything. To trade on a schedule, wrap it in
cron or `watch` — e.g. once a day after the close for a daily strategy.

## Add your own strategy

Create a file in `src/trading/strategies/`, subclass `backtesting.Strategy`, implement
`init()` (compute indicators) and `next()` (entry/exit rules), then register it in
`strategies/__init__.py`'s `STRATEGIES` dict. See `sma_cross.py` for the pattern.

## Roadmap (not built yet)
- Parameter optimization and walk-forward validation.
- Track realized paper-trade P&L over time (a live counterpart to the backtest journal).
- Scheduled/automated `paper-run` (cron) with notifications.
- More markets and richer reporting.
