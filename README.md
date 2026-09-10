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
  data/        Yahoo backtest data + Alpaca Run 2 daily bars, cached as parquet
  signals/     tradingview-ta wrapper -> TradingView's BUY/SELL/NEUTRAL recommendation
  strategies/  your "assumptions": sma_cross, rsi_meanrev (add your own here)
  backtest/    runs a strategy and computes the W&L metrics
  reporting/   logs every run to results/journal.csv + saves an equity-curve chart
  run2/        frozen manifest, event ledger, risk, context, shadows, statistics
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

# Run a strategy across the WHOLE watchlist on the paper account
money paper-scan --strategy rsi_meanrev
money paper-scan --strategy rsi_meanrev --dry-run   # preview, place nothing
```

`paper-scan` skips any symbol you already hold *or* have an unfilled order for, so
running it repeatedly never stacks duplicate buys. Every evaluation is appended to
`results/paper_journal.csv`.

### Auditable Run 2

The current experiment uses the frozen [Run 2 manifest](config/run2.yaml):
$10,000, SMA 10/30 on closed daily bars, $625 entries, portfolio/asset/correlation
caps, an 8% fill-derived stop, adverse-gap limits, and a 5% drawdown entry halt.
Only the baseline can reach Alpaca paper trading. Regime and FinBERT news features
are prospective shadow portfolios, not discretionary overrides.

```bash
pip install -e '.[research]'                 # optional, required for FinBERT news
money run-init --run-id run2                 # once, on a clean $10k paper account
money collect-context --run-id run2
money paper-scan --run-id run2 --strategy sma_cross --dry-run
money execute-intents --run-id run2 --asset crypto --dry-run
money reconcile --run-id run2
money run-report --run-id run2
money portfolio-backtest --run-id run2        # synchronized $10k portfolio replay
```

The complete initialization, data-source, scheduling, reconciliation, reporting,
and evidence protocol is in **[docs/run2.md](docs/run2.md)**.

### Email updates

The daily scheduled run emails you a **complete update**: paper-account equity and
open positions with live P&L, every symbol's signal from the latest scan,
backtest alpha vs buy & hold per symbol ("is the strategy improving?"), system
health of the scheduled jobs, Run 2 baseline/shadow metrics, and insights. Short alert emails go out immediately if
a stop-loss fires or a scheduled run fails — so problems reach your inbox, not
just a log file.

Setup: pick any SMTP provider (Gmail works well — create an App Password at
<https://myaccount.google.com/apppasswords>), then uncomment and fill the
`EMAIL_*` block in `.env` (see `.env.example`). Test it:

```bash
money email-report --dry-run   # render the digest locally, send nothing
money email-report             # send it for real
```

Leave the `EMAIL_*` variables unset and everything else works exactly as before
(emailing is best-effort and never fails a trading run).

### Automated daily runs

> On this Mac the schedule runs through **Hermes cron jobs**, not launchd — see
> [docs/local-ops.md](docs/local-ops.md) for the live jobs, the silent watchdog
> scripts and the mid-history `run-init --resume` binding. The launchd/systemd
> instructions below remain for other machines.

Three schedules drive the paper account (all wrappers append to logs under
`results/` and send a desktop notification on failure):

- **Context, closed-bar scan, and crypto execution** —
  `scripts/daily_paper_run.sh` at **00:05 UTC**.
- **Guarded stock execution** — `scripts/execute_stock_intents.sh` at **09:31
  America/New_York**, Monday–Friday, for decisions made after the prior close.
- **Intraday stop monitor** — `scripts/intraday_stop_run.sh`, every **30
  minutes**, closing any open position that has fallen through the stop-loss
  threshold instead of waiting for the daily signal scan.

#### Linux (systemd user timers) — this machine

Units live in `scripts/systemd/`; install and enable with:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/*.service scripts/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now money-paperscan.timer money-stockexecution.timer money-stopmonitor.timer

# Check they're scheduled (next fire times)
systemctl --user list-timers 'money-*'

# Test the exact scheduled runs without placing orders
DRY_RUN=1 bash scripts/daily_paper_run.sh
DRY_RUN=1 bash scripts/execute_stock_intents.sh
DRY_RUN=1 bash scripts/intraday_stop_run.sh

# Disable all schedules
systemctl --user disable --now money-paperscan.timer money-stockexecution.timer money-stopmonitor.timer
```

`Persistent=true` fires a missed run after boot/wake (the stop monitor also
checks ~10 min after login). If the project moves, update `WorkingDirectory`
and `ExecStart` in the three `.service` files and reinstall them.

> Full details — concept map from launchd, testing without orders, log/heartbeat
> locations, and troubleshooting: **[docs/linux-systemd.md](docs/linux-systemd.md)**.

#### macOS (launchd)

```bash
# Install / reload the schedule
cp scripts/com.money.paperscan.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.money.paperscan.plist

# Check it's registered
launchctl list | grep com.money.paperscan

# Disable the schedule
launchctl unload -w ~/Library/LaunchAgents/com.money.paperscan.plist
```

To change the time on macOS, edit `StartCalendarInterval` in the plist (then
reload). Note: a laptop must be awake at 18:05; launchd will run a missed job
once the machine wakes.

### Starting a clean paper run

1. Archive `data/` and `results/`; never archive `.env` with them.
2. Create/reset a dedicated Alpaca paper account to exactly $10,000 with no
   positions or order history, then install its paper keys in `.env`.
3. Review `config/run2.yaml`; after initialization its hash is immutable.
4. Run `money run-init --run-id run2` once.
5. Preview all three wrapper scripts with `DRY_RUN=1`, then enable the timers.

Keep one strategy, watchlist, position size, and stop rule unchanged for an
entire run so the result can be attributed to a stable experiment.

> ⚠️ **macOS only: keep the project OUT of `~/Documents`, `~/Desktop`, and
> `~/Downloads`.** Those are macOS privacy-protected (TCC) folders — launchd
> background jobs are blocked from reading them. On **Linux** (including this
> machine) no such restriction exists; living under `~/Documents` is fine.

Legacy `paper-run` fetches recent bars via Yahoo, computes the
strategy's BUY/SELL/HOLD signal on the latest bar, checks whether you already hold the
asset, and submits a market order to the **paper** account when warranted. Use `--dry-run`
to preview the decision without sending anything. To trade on a schedule, wrap it in
cron or `watch` — e.g. once a day after the close for a daily strategy.

## Add your own strategy

Create a file in `src/trading/strategies/`, subclass `backtesting.Strategy`, implement
`init()` (compute indicators) and `next()` (entry/exit rules), then register it in
`strategies/__init__.py`'s `STRATEGIES` dict. See `sma_cross.py` for the pattern.

## Roadmap

- Complete the pre-registered Run 2 evidence window before changing parameters.
- Add any new alternative data only as a separately frozen shadow arm.
- Expand walk-forward and multiple-testing diagnostics without touching Run 2.
