# Run 2 operating protocol

Run 2 is a prospective **paper-only** experiment. The baseline strategy is
frozen in `config/run2.yaml`; research inputs create simulated shadow portfolios
and can never authorize or size a broker order.

## Frozen baseline

- $100,000 starting cash and equity; no margin is consumed.
- SMA 10/30 fresh cross on the latest closed daily bar.
- The 17-symbol archived watchlist: 8 crypto pairs and 9 US stocks.
- $625 per entry; at most 8 positions and $5,000 gross cost exposure.
- Crypto: at most 4 positions / $2,500. Stocks: at most 6 / $3,750.
- Reject a candidate correlated at 0.80 or above with more than one holding,
  using 60 daily returns.
- 8% protective stop based on the ledger's fill-derived average entry.
- Reject adverse entries above the signal close by more than 3% for crypto or
  2% for stocks.
- At a 5% high-water drawdown, halt all new buys. Exits and reconciliation stay
  active.
- Keep the manifest unchanged for at least 90 days **and** 50 completed trades.

The manifest hash is stored when the run is initialized. Commands refuse a
later config whose hash differs. The Alpaca trading client is hardcoded to
`paper=True`; this repository has no live-order route.

## Data and research boundary

Run 2 price history comes from Alpaca daily bars (IEX for equities, Alpaca US
spot for crypto). Cboe VIX history, FRED high-yield option-adjusted spread, and
Alpaca/Benzinga structured news are captured with timestamps, hashes, and raw
payloads under `results/run2/raw-context/`.

The research arms are:

1. `baseline` — the only portfolio that can submit paper orders.
2. `shadow_regime` — baseline entries gated by a pre-registered 0–4 regime
   score; minimum 3. Research snapshots older than 36 hours are treated as
   missing, never silently reused.
3. `shadow_regime_news` — the regime gate plus a prospective negative-news
   robust z-score gate.

FinBERT is pinned by model revision. Install its optional runtime before news
collection:

```bash
source .venv/bin/activate
pip install -e '.[research]'
```

Use `--skip-news` to test regime collection without that optional dependency.
X posts and scraped paywalled articles are intentionally excluded: their access,
licensing, deletion history, bot activity, and timestamp reproducibility make
them unsuitable for an auditable first prospective test. They can be evaluated
later as a separately pre-registered shadow source, never added mid-run.

SEC EDGAR requires a declared user agent. Set a real contact address in `.env`:

```bash
SEC_USER_AGENT=money research your-email@example.com
```

Only the form allowlist frozen in `config/run2.yaml` is captured. Filing events
are stored as primary-source context but are not interpreted as automatic
negative signals. Use `--skip-sec` only for an explicit diagnostic run.

## One-time initialization

1. Create or reset a dedicated Alpaca **paper** account to exactly $100,000.
2. Confirm it has no positions and no order history. Put only its paper keys in
   `.env`.
3. Install dependencies and preview the non-ordering data paths:

   ```bash
   money paper-status
   money collect-context --run-id run2 --skip-news
   ```

4. Initialize once:

   ```bash
   money run-init --run-id run2
   ```

Initialization only reads Alpaca account state and creates the local frozen
ledger. It refuses the wrong balance, any position, or any historic order.

Do not delete `results/run2/ledger.sqlite` after initialization. Back it up with
the raw context directory. Never include `.env` in an archive.

## Daily flow

The scheduled flow is deliberately split at market boundaries:

```text
00:05 UTC daily:
  capture context -> scan all closed daily bars -> execute crypto intents
  -> simulate shadows -> reconcile fills/fees

09:31 America/New_York, weekdays:
  execute prior-close stock intents -> simulate shadows -> reconcile

every 30 minutes:
  check fill-derived 8% stops -> reconcile
```

The entry path uses an immediately-or-cancel marketable limit at the adverse-gap
cap. A price already beyond the cap expires the intent. A stock intent is not
submitted outside the regular session.

Safe previews:

```bash
DRY_RUN=1 bash scripts/daily_paper_run.sh
DRY_RUN=1 bash scripts/execute_stock_intents.sh
DRY_RUN=1 bash scripts/intraday_stop_run.sh
```

Dry Run 2 scans do not write decisions. Dry execution reads prices but does not
submit or reconcile orders.

## Ledger, reconciliation, and reports

`results/run2/ledger.sqlite` records the frozen run, decisions, broker orders,
fills, fee activities, equity snapshots, and research features. Positions and
cost basis are rebuilt from fills rather than Alpaca's position cost-basis
field. Repeated activity imports are idempotent.

Reconciliation halts entries if it sees an unknown fill/order or a quantity
mismatch between Alpaca and the internal ledger. Investigate the log and ledger;
do not edit the database manually or simply unhalt the run.

```bash
money reconcile --run-id run2
money run-report --run-id run2
money run-report --run-id run2 --json
money portfolio-backtest --run-id run2
money dashboard
money email-report --dry-run
```

The report compares baseline and shadows on account return, completed
round-trip expectancy, win rate, maximum drawdown, Sharpe, a deflated-Sharpe
probability, and a 90% block-bootstrap expectancy interval. The primary passive
benchmark is monthly rebalanced 25% equal-weight stocks, 25% equal-weight
crypto, and 50% cash. A missing or unattributed broker fee remains visible in
the account-level fee total rather than being silently invented.

## Evidence gate

Do not promote a shadow rule because of a few good weeks. Evaluate only after
the later of 90 calendar days or 50 completed baseline trades. Prefer account
return and drawdown against the passive benchmark, fee-net completed-trade
expectancy with its interval, fill gaps, reconciliation health, and the full
distribution of outcomes—not social-media mood or a single headline.
