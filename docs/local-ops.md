# Local ops (this Mac) — money

How the paper desk actually runs on this machine. Paper only: Alpaca `paper=True`,
`PK…` keys, fake money, no live route anywhere in the code.

## Layout

- Repo/home: `~/money` (public repo: <https://github.com/dancaldera/money>).
  Kept out of `~/Documents` on purpose — macOS TCC blocks background jobs there.
- Ledger (live): `results/run3/ledger.sqlite` (event ledger; positions rebuilt from
  fills). `results/run2/ledger.sqlite` is run2's evidence archive.
- Frozen manifest (live): `config/run3.yaml` — opened 2026-09-19: $100,000, SMA 10/30,
  $625 entries, 8% stop, 5% drawdown halt with opt-in recovery (2.5% / 20d), breadth
  caps (17 slots / $10,625 gross). Hash stored at init; editing it breaks every command.
  `config/run2.yaml` stays frozen as the audited predecessor.
  Parameter experiments go in `config/experiments/` and replay read-only with
  `money portfolio-backtest --run-config config/experiments/<name>.yaml`
  (never reachable by live commands) — see `docs/experiments.md`.
- Credentials: `.env` (paper keys, `chmod 600`, gitignored). `EMAIL_*` optional.
- Evidence from the previous desk (`veto` fork): `results/legacy-veto/` (its ledger,
  logs and heartbeats) and `~/Legacy/money-veto-2026-09-09/` (full archive).

## Scheduling = Hermes cron (not launchd)

The macOS launchd agents were removed on purpose: one scheduler with visible state.

| Job | Schedule | Mode |
|---|---|---|
| money · morning buy/hold/sell | every day at 09:00 | agent + `scripts/morning_brief.sh` (read-only resume in Hermes) |
| money · daily paper run | every day at 18:35 | agent (terminal, workdir `~/money`) |
| money · daily scan catch-up (silencioso) | every day at 22:00 | `no_agent` script (`scripts/cron_catchup_daily.sh`), only runs the desk if today never scanned |
| money · stop monitor (silencioso) | every 30 min | `no_agent` script |
| money · health watchdog (silencioso) | every hour | `no_agent` script |
| money · mejora diaria (agente) | every day at 08:00 | agent (objective: make money) |

### Why 18:35 and not 17:00

Crypto daily bars close at 00:00 UTC. At 17:00 CST (23:00 UTC) the newest
*closed* crypto bar is one full UTC day old, so a crypto entry executes ~23h
after its signal — and the frozen 3% adverse-gap cap expires the intent when the
price has already run, which a fresh-cross strategy can never recover (no fresh
cross, no re-entry). Measured over 8 cryptos / 4.7 years
(`python scripts/analysis_execution_gaps.py`):

- 18% of buy intents (~10/year) fall outside the 3% cap and are never taken;
- the remaining entries pay ~0.06% of adverse drift per cross.

At 18:35 CST (00:35 UTC) the just-closed bar is the one scanned and execution
lands ~35 min after its close, which is also what `portfolio-backtest` models
("execute prior closed-bar intents at the next available bar open"). 35 minutes
(rather than 00:05) leaves Alpaca time to publish the completed bar; a bar that
is still forming is dropped either way (`data/clean.py: drop_forming_bar`), and
at 00:05 a lagging publication would quietly reuse the stale bar. Stocks are
unaffected by the crypto clock: their daily bar is complete long before 18:35.
Their intents execute at the weekday stock slot, which on this Mac is 08:31 local
(local clock = UTC-6, so ET = local + 2h) — **~10:31 ET, about an hour after the
09:30 ET open**, plus cron jitter (observed 10:31-11:11 ET, 2026-09-10..16), not
09:31 ET as the launchd/systemd timers do on other machines. Measured on 30-min
bars over 199 stock signals, entering at +60min vs the official open drifts
-0.035% mean / 0.000% median, so the delay is not costing money: do not move the
slot on that theory.

The 18:35 daily run is an **agent** job, so it can die before it ever reaches
`daily_paper_run.sh` (model/API outage, credit exhaustion, inactivity timeout) and
nothing trades. That is unrecoverable by design: `paper-scan` only evaluates the
latest closed bar, so a fresh cross on the missed bar is never seen again. The
22:00 catch-up guard closes that hole — it runs the wrapper only when today's
`.last_success_paperscan` date is not today, which also makes it idempotent
(a day that already scanned stays silent). Preview its decision with
`CHECK_ONLY=1 bash scripts/cron_catchup_daily.sh`.

Inspect: `hermes cron list` / `cronjob_manage(action='list')`. Trading jobs
save locally (`deliver: local`). The 09:00 buy/hold/sell briefing posts into
the Hermes chat it was created from (continuable).

Hermes runs cron scripts only from `~/.hermes/scripts/`, so thin shims live
there (`money_stop_monitor.sh`, `money_health.sh`, `money_morning_brief.sh`)
and `exec` the repo scripts (`scripts/cron_silent_*.sh`,
`scripts/morning_brief.sh`). Repo = single source of truth.

The silent jobs use the watchdog pattern: nothing printed = nothing sent. They speak
only when a stop fires, the run halts, a heartbeat goes stale, or a wrapper fails.

launchd equivalents are still in the repo for other machines: `scripts/com.money.*.plist`
and `scripts/systemd/`.

## Daily loop

```bash
bash scripts/morning_brief.sh        # read-only 09:00 dump: health + Alpaca status + stops/scan dry-run
bash scripts/daily_paper_run.sh      # context + closed-bar scan + crypto execute + reconcile
bash scripts/execute_stock_intents.sh # guarded stock execution (08:31 local ~ 10:31 ET here)
bash scripts/intraday_stop_run.sh    # 8% stop + reconcile (every 30 min)
bash scripts/health_check.sh         # heartbeats + halt state (hourly)
bash scripts/cron_catchup_daily.sh   # 22:00 guard: runs the desk only if today never scanned
DRY_RUN=1 bash scripts/daily_paper_run.sh   # preview, no ledger writes / no orders
```

Logs: `results/paper_scan.log`, `results/stop_monitor.log`, `results/health.log`,
`results/morning_brief.log` (rotation keeps 2000 lines). Heartbeats: `results/.last_success_*`.

Research/attribution scripts over the cached bars and the gitignored artifacts
(both read-only, both regression-tested):

```bash
.venv/bin/python scripts/analysis_execution_gaps.py    # what the desk's slot costs per signal
.venv/bin/python scripts/analysis_symbol_edge.py \
    --artifact results/exp-scale-2x/portfolio-backtest --notional 1250
```

The second one answers "should we cut the losing symbols?" with a walk-forward
test (per-trade quality vs total dollars, plus a random-k control and split-half
persistence). Measured answer so far: raising per-trade expectancy always lost
dollars, see `docs/experiments.md`.

`DRY_RUN=1` never advances `.last_success_paperscan` — it writes
`.last_preview_paperscan` instead. The 22:00 catch-up guard decides on the success
heartbeat, so a preview must not be able to make a missed evening look scanned
(and the hourly watchdog keeps reporting the real run).

`money health` (and therefore the 09:00 brief and the hourly watchdog) also prints
one `coverage_<scope>:` line comparing the ledger's newest recorded bar with the
newest closed bar in the cached data. `behind=1` means a run fetched fresh data
and the ledger did not advance — the bar can still be scanned by re-running the
desk, so `health_check.sh` alerts on it. Older `gaps` (bars never evaluated at
all, see `docs/run2.md`) stay visible without alerting forever. A
`pending_intents:` line reports the oldest un-executed buy intent (count, symbol,
age): a stuck intent permanently reserves exposure and blocks its symbol
(`buy_already_pending`), so the watchdog alerts past `PENDING_MAX_AGE_H`
(default 96h — a normal weekend plus slot jitter fits).

## Mid-history account

This account had already traded when the merged desk was bound, so plain
`money run-init` (clean $100k account, no order history) could never pass again.
It was bound with:

```bash
.venv/bin/money run-init --resume     # imports open positions as simulated baseline fills
```

`--resume` binds the frozen manifest to the account as it is now, so
broker qty == ledger qty and `reconcile` has nothing unknown to halt on. From there
the loop is identical to a fresh init. Read-only status: `.venv/bin/money health`.

run3 was bound the same way on 2026-09-19 (`money run-init --run-id run3
--run-config config/run3.yaml --resume`), importing AAPL and AMD as its baseline
fills; run2's ledger remains its archived evidence.

## Repo & push policy

- Objective of the morning agent: **make money** (paper P&L). It measures the ledger,
  researches ideas on the web, deletes what is unnecessary, implements one small
  tested improvement, commits and pushes to `main`.
- Hard invariants: paper only (live is a human decision); never commit `.env`,
  `results/` or `.venv/`, never print keys; never edit the frozen `config/run3.yaml`
  (use a separate manifest + offline backtest and propose the swap); small, useful,
  reversible changes with green tests — no force-push, no history rewrite, no deleting
  runtime data or tests to make things pass.
