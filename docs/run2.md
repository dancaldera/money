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

Live timing deviates from this design on this Mac: the crypto slot runs at
18:35 CST (00:35 UTC) and the stock slot fires at 08:31 local (~10:31 ET) — see
`docs/local-ops.md`.

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

## Halt semantics (and the opt-in recovery rule)

A halt is an **event**, not a level: `runs.status` flips to `halted` and every
live command refuses new buys (`risk.check_entry` → `run_halted`) while exits and
reconciliation keep running. Two reasons write it:

- `drawdown_halt:<dd>%` — the account equity fell `portfolio.drawdown_halt_pct`
  below its high-water mark (`service.record_account_snapshot`).
- `reconciliation_failed:...` — fail-closed on an unknown order or a quantity
  mismatch. This one is **never** recoverable, by design.

The frozen run (`run2`) keeps the documented **one-way latch**: once halted, only
a human re-initialises/resettles the run. A manifest may instead opt into a
recovery rule, and then it applies the same policy in the replay
(`portfolio.simulate_portfolio`) and in the live service (`risk.halt_action`):

```yaml
portfolio:
  halt_recovery_drawdown_pct: 2.5   # re-arm once drawdown falls back to 2.5%
  halt_recovery_days: 20            # or after 20 calendar days, whichever first
```

- Only drawdown halts recover; a reconciliation halt stays latched.
- The cooldown is what actually fires. A halted desk is flat, so its equity
  cannot rise and `halt_recovery_drawdown_pct` alone is inert (measured,
  `docs/experiments.md`).
- On resume the ledger keeps the halt text (`halt_reason` becomes
  `resumed:drawdown_halt:5.1000%@2.4000%`) and sets `halted_at` to the resume
  time; `ledger.high_water(..., since=resume_time)` then measures drawdown from
  the resume, so a peak already lost is not counted twice. Without that
  re-baseline the halt condition stays true and the desk only re-enters every
  cooldown period.
- Reports label that state as `recovered from: ...` — a resumed run is not a halt.
- Both keys are pinned to **absent** for `run2` by the strict loader, and adding
  them to `run2.yaml` would change its hash and lock the ledger: adopting recovery
  live means a new `run_id` with its own `run-init`.

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

## Scan coverage

`paper-scan` evaluates only the newest *closed* bar, so a bar that is never the
newest one when a run looks is never evaluated at all — and the frozen strategy
re-enters only on a fresh SMA cross, so a cross on that bar is gone for good.
Two real ways to lose one silently: a wrapper that fetches fresh data and then
records nothing new (the desk keeps trading the older bar), and a schedule step
that walks over the 00:00 UTC crypto close (a run at 17:00 CST advances the data
clock by two days and steps over one bar; that is how the crypto bar of
2026-09-11 was never evaluated after the slot moved to 18:35).

`money health` therefore reports, per scope, how far the ledger is from the data:

```
coverage_crypto: gap newest_closed=2026-09-12T00:00:00 newest_recorded=2026-09-12T00:00:00 behind=0 gaps=1 recent_gaps=1 missing=2026-09-11T00:00:00
coverage_equity: ok  newest_closed=2026-09-11T04:00:00 newest_recorded=2026-09-11T04:00:00 behind=0 gaps=0 recent_gaps=0 missing=-
```

* `behind=1` — the ledger has not evaluated the newest closed bar of the cached
  data. Still actionable, so `scripts/health_check.sh` alerts on it.
* `gaps` / `missing` — complete bars inside the recorded window with no decision:
  already-permanent losses, kept in the log and the 09:00 brief as the evidence
  trail. `recent_gaps` marks a skip inside the newest three bars; older skips stay
  on record without alerting forever.

The lines are read-only and offline (local parquet cache, no broker, no network)
and are printed by `scripts/morning_brief.sh` and logged by the hourly watchdog.

## Evidence gate

Do not promote a shadow rule because of a few good weeks. Evaluate only after
the later of 90 calendar days or 50 completed baseline trades. Prefer account
return and drawdown against the passive benchmark, fee-net completed-trade
expectancy with its interval, fill gaps, reconciliation health, and the full
distribution of outcomes—not social-media mood or a single headline.
