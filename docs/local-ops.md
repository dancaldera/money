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
| money · daily paper run | every day at 18:35 | `no_agent` script (`~/.hermes/scripts/money_daily_run.sh` → `scripts/daily_paper_run.sh`) |
| money · daily scan catch-up (silencioso) | every day at 22:00 | `no_agent` script (`scripts/cron_catchup_daily.sh`), runs the desk if tonight's slot never scanned |
| money · stop monitor (silencioso) | every 30 min | `no_agent` script |
| money · health watchdog (silencioso) | every hour | `no_agent` script — alerts, and salvages a bar the ledger never evaluated (coverage mode of the same guard) |
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

The 18:35 daily run is a **`no_agent` script** job on purpose. As an agent job it
died before finishing the wrapper on the 600s inactivity watchdog (2026-09-22 and
2026-09-24, `~/.hermes/cron/executions.db`: `idle for 909s (limit 600s)`), and the
day's only scan became the 22:00 catch-up — the same bar, but a crypto entry ~3.4h
later than the slot it was designed for, which is the drift the 18:35 slot exists
to avoid. The failure mode is a property of the mode, not of the wrapper: the
script job gets a 3600s budget and, unlike the agent, cannot die in the model API
before it reaches the shell (that is how the 2026-09-10 scan was lost). Nothing is
delivered on success anyway (`deliver: local`), so the agent half was invisible;
the desk's report comes from the 09:00 morning brief.

Failure is still permanent by design — `paper-scan` only evaluates the latest
closed bar, so a fresh cross on the bar a missed slot never scanned is never seen
again. The 22:00 catch-up guard is the second line: it runs the wrapper unless the
`.last_success_paperscan` heartbeat is from **after today's slot time**, not merely
from today (an early-in-the-day manual run must not silence it, and the re-run it
allows is idempotent per bar). Preview its decision with
`CHECK_ONLY=1 bash scripts/cron_catchup_daily.sh`.

Job health has to be read from `~/.hermes/cron/executions.db`, not from
`last_status`: check `started_at`/`finished_at` (a 909s outlier is a watchdog kill,
a 400-1500s one is a hung broker/DNS call) and `cron_incidents` for the error text.

The intraday stop monitor (30-min tick) has the opposite problem: it is the only
protection an open position has between daily scans and it has **no catch-up**, so a
tick lost to the network leaves the book unprotected until the next one — measured
2026-10-08 03:48 CST (`ConnectionResetError(54)`) and 07:40 CST
(`NameResolutionError`), ~54 min with BTC already -5%. It used to be invisible too:
`intraday_stop_run.sh` always exited 0 (the failure branch ends in `|| true` and an
`if` returns 0), so `cron_silent_stop_run.sh`'s `rc -ne 0` branch was dead code and
the Hermes job still reported ok — the desktop notification was the only alert and
`EMAIL_*` is unset (the email path just logs "Email reports not configured"). Both
halves are fixed and pinned by tests in `tests/test_schedule_scripts.py`: a
**transient transport** failure (`ConnectionError`, `NameResolutionError`,
`ConnectionResetError`, `Max retries exceeded`, read/connect timeouts) is retried in
place (`STOP_MAX_ATTEMPTS` 3, `STOP_RETRY_SLEEP` 45s), the loop stops the moment any
`action=stopped`/`would_stop` line appears (a re-run after a submitted close could
double-submit), a non-transient error is not retried at all, and the CLI's exit code
now reaches the caller so the silent job speaks. Test seams (opt-in, like
`PAPERSCAN_HEARTBEAT`): `MONEY_BIN`, `STOP_LOG`, `STOP_HEARTBEAT`. A **hang** is still
unbounded — `requests` runs without a timeout here and macOS has no `timeout(1)` — so
a hung call is only caught by the job's 3600s watchdog (2026-10-07 10:24); that is the
next gap to close.

Inspect: `hermes cron list` / `cronjob_manage(action='list')`. Trading jobs
save locally (`deliver: local`). The 09:00 buy/hold/sell briefing posts into
the Hermes chat it was created from (continuable).

Hermes runs cron scripts only from `~/.hermes/scripts/`, so thin shims live
there (`money_stop_monitor.sh`, `money_health.sh`, `money_morning_brief.sh`,
`money_daily_run.sh`)
and `exec` the repo scripts (`scripts/cron_silent_*.sh`, `scripts/daily_paper_run.sh`,
`scripts/morning_brief.sh`). Repo = single source of truth.

The silent jobs use the watchdog pattern: nothing printed = nothing sent. They speak
only when a stop fires, the run halts, a heartbeat goes stale, a wrapper fails, or
the hourly health watchdog salvages a bar the ledger never evaluated (see
"Catch-up and salvage" below).

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
(all read-only, all regression-tested):

```bash
.venv/bin/python scripts/analysis_execution_gaps.py    # what the desk's slot costs per signal
.venv/bin/python scripts/analysis_symbol_edge.py \
    --artifact results/exp-scale-2x/portfolio-backtest --notional 1250
.venv/bin/python scripts/analysis_halt_episodes.py \
    --artifact results/exp-slots-all-2x-recover/portfolio-backtest \
    --run-config config/experiments/exp-slots-all-2x-recover.yaml
.venv/bin/python scripts/analysis_live_deployment.py   # live run: deployment + attribution
.venv/bin/python scripts/analysis_limit_entry.py \     # what a passive (maker) entry is worth
    --artifact results/exp-slots-all-recover/portfolio-backtest --notional 625
.venv/bin/python scripts/analysis_experiment_table.py \   # one table for a sweep of replays
    exp-slots-all-recover exp-sma-3-15 exp-sma-5-20 exp-sma-8-24
.venv/bin/python scripts/analysis_marginal_trades.py \   # what a rule change added
    --control exp-slots-all-recover exp-corr-off exp-corr-match2
```

The second one answers "should we cut the losing symbols?" with a walk-forward
test (per-trade quality vs total dollars, plus a random-k control and split-half
persistence). Measured answer so far: raising per-trade expectancy always lost
dollars, see `docs/experiments.md`.

The third prices a replay's drawdown halt — how many times it fired and how many
sessions the desk sat flat waiting for the recovery rule — by re-running the same
`halt_action` state machine the live service uses over an artifact's `equity.csv`.
A latch manifest shows one episode that never resumes.

The fourth is the capital lens on the live run, which `run-report` does not show:
positions against the frozen caps (slots, gross, per-asset), per-position
market value and unrealised P&L marked to the newest recorded close, and a census
of what the guards did with each fresh cross (allowed / blocked by which guard /
filled / pending / expired). It is read-only and needs no broker or network call —
if the live book is at 4% of equity, that ratio is what a size step has to move,
and the scan census is the live evidence for whether a cap or the entry rule is
holding it back.

The sweep table (`analysis_experiment_table.py`, the fifth command above) answers
"which of these replays actually won" without hand-copying five `summary.json`
files — and refuses to imply a comparison it cannot support, since rows replayed
on different cache dates are not measured on the same bars.

The last one (`analysis_marginal_trades.py`) answers the follow-up the sweep
table cannot: *which* trades the variant actually added or dropped, and whether
they are better or worse than the control's average trade (it matches fills on
date+symbol+side and prices the added round trips at their own realized P&L).
That is the number that decides a guard or filter: a rule that blocks
below-average trades earns its keep even when it costs a little return, while
one that blocks average trades is only throttling deployment. Read it beside the
`blocked_by` census in `decisions.csv`, which names the guard that freed the
slot.

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

### Catch-up and salvage

`cron_catchup_daily.sh` has two modes and one rule each, and both end in the same
place (run `daily_paper_run.sh`, which is idempotent per bar):

* **slot mode** (default, the 22:00 job): run the desk unless the success
  heartbeat is from after today's 18:35 slot.
* **coverage mode** (`COVERAGE_ONLY=1`, called by the hourly
  `cron_silent_health.sh`): run the desk when `money health` reports any
  `coverage_<scope>: ... behind=1`, i.e. the ledger never evaluated the newest
  closed bar — regardless of the heartbeat. It stays silent and costs one
  read-only health call while coverage is current, refuses to act inside the
  bar-close window (18:00 CST close → 18:35 slot), and bounds retries with
  `results/.last_salvage_attempt` (`SALVAGE_COOLDOWN_S`, default 1800s; stamped on
  every attempt, success or failure).

  It also needs the gap **confirmed on a later tick** (`results/.last_coverage_behind`,
  keyed `scope@newest_closed`, `SALVAGE_CONFIRM_S` default 3000s): a bar that has
  just closed is `behind=1` by construction, and the window check cannot see a
  *late* slot. Measured 2026-09-30: the 18:35 slot started 9m late, so the
  watchdog's 18:45 tick read `behind=1` while that slot's own context was still
  running and started a full duplicate desk run (watchdog 18:44:58 → 21:32:23 —
  two contexts, two scans of the same bar: idempotent, but pure waste and a
  second writer). One tick of delay still leaves ~23h of margin, and a gap the
  slot clears deletes the marker.

Why it exists: on 2026-09-29 the 18:35 run died on a full DNS outage (Alpaca *and*
the CBOE VIX unresolvable), the 22:45 catch-up salvaged the bar, and nothing
covered the case where that second run fails too. A report-only watchdog plus one
evening slot leaves a ~24h hole — after the next bar closes, the skipped bar can
never be scanned again (a fresh cross is required to re-enter), so the entry is
lost for good. Preview either mode with `CHECK_ONLY=1` (plus `HEALTH_SNAPSHOT=`,
`PAPERSCAN_HEARTBEAT=`, `PRE_SLOT_HHMM=`, `SALVAGE_STAMP=`,
`COVERAGE_CONFIRM_STAMP=` to inject state).

The live bar window (`_run2_bars`, `recent=True`) starts 400 days before the 1st
of the current month, so every run inside a month shares one cache key. The
loader still refetches it on every run (freshness is the point), but a previous
successful fetch is now on disk, so a broker/DNS failure degrades to those bars
instead of failing the whole scan — the failure is loud (`coverage_*` reports
`behind=1`, the watchdog alerts) and the next slot can still evaluate the bar.
A per-day key (`today-400d`) silently disabled that fallback — today's key never
had a file — and wrote one new 400-day parquet per symbol per day (~17/day).

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

## Clearing a halt

A drawdown halt clears itself when the manifest opts into recovery. A
`reconciliation_failed` halt does not — it is fail-closed, so the schedule will
never clear it and no wrapper can. Diagnose it first (health + the ledger), fix
the cause, then re-arm explicitly; the stated reason is kept on the run row as
`resumed:<note>` and becomes the drawdown baseline:

```bash
.venv/bin/money health --run-id run3 --run-config config/run3.yaml
.venv/bin/money reconcile --run-id run3 --run-config config/run3.yaml
.venv/bin/money run-rearm --run-id run3 --run-config config/run3.yaml --note "why"
```

First real case (2026-09-21): the crypto taker fee is deducted in kind, so a
crypto buy filled 25bps fewer units than the activity reported and the qty check
latched the run — see [docs/run2.md](run2.md).

## Repo & push policy

- Objective of the morning agent: **make money** (paper P&L). It measures the ledger,
  researches ideas on the web, deletes what is unnecessary, implements one small
  tested improvement, commits and pushes to `main`.
- Hard invariants: paper only (live is a human decision); never commit `.env`,
  `results/` or `.venv/`, never print keys; never edit the frozen `config/run3.yaml`
  (use a separate manifest + offline backtest and propose the swap); small, useful,
  reversible changes with green tests — no force-push, no history rewrite, no deleting
  runtime data or tests to make things pass.
