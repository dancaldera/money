# Local ops (this Mac) — money

How the paper desk actually runs on this machine. Paper only: Alpaca `paper=True`,
`PK…` keys, fake money, no live route anywhere in the code.

## Layout

- Repo/home: `~/money` (public repo: <https://github.com/dancaldera/money>).
  Kept out of `~/Documents` on purpose — macOS TCC blocks background jobs there.
- Ledger: `results/run2/ledger.sqlite` (Run 2 event ledger; positions rebuilt from fills).
- Frozen manifest: `config/run2.yaml` — $100,000, SMA 10/30, $625 entries, 8% stop,
  5% drawdown halt. Its hash is stored at init; editing it breaks every command.
- Credentials: `.env` (paper keys, `chmod 600`, gitignored). `EMAIL_*` optional.
- Evidence from the previous desk (`veto` fork): `results/legacy-veto/` (its ledger,
  logs and heartbeats) and `~/Legacy/money-veto-2026-09-09/` (full archive).

## Scheduling = Hermes cron (not launchd)

The macOS launchd agents were removed on purpose: one scheduler with visible state.

| Job | Schedule | Mode |
|---|---|---|
| money · morning buy/hold/sell | every day at 09:00 | agent + `scripts/morning_brief.sh` (read-only resume in Hermes) |
| money · daily paper run | every day at 17:00 | agent (terminal, workdir `~/money`) |
| money · daily scan catch-up (silencioso) | every day at 22:00 | `no_agent` script (`scripts/cron_catchup_daily.sh`), only runs the desk if today never scanned |
| money · stop monitor (silencioso) | every 30 min | `no_agent` script |
| money · health watchdog (silencioso) | every hour | `no_agent` script |
| money · mejora diaria (agente) | every day at 08:00 | agent (objective: make money) |

The 17:00 daily run is an **agent** job, so it can die before it ever reaches
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
bash scripts/execute_stock_intents.sh # guarded stock execution (09:31 ET on upstream timers)
bash scripts/intraday_stop_run.sh    # 8% stop + reconcile (every 30 min)
bash scripts/health_check.sh         # heartbeats + halt state (hourly)
bash scripts/cron_catchup_daily.sh   # 22:00 guard: runs the desk only if today never scanned
DRY_RUN=1 bash scripts/daily_paper_run.sh   # preview, no ledger writes / no orders
```

Logs: `results/paper_scan.log`, `results/stop_monitor.log`, `results/health.log`,
`results/morning_brief.log` (rotation keeps 2000 lines). Heartbeats: `results/.last_success_*`.

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

## Repo & push policy

- Objective of the morning agent: **make money** (paper P&L). It measures the ledger,
  researches ideas on the web, deletes what is unnecessary, implements one small
  tested improvement, commits and pushes to `main`.
- Hard invariants: paper only (live is a human decision); never commit `.env`,
  `results/` or `.venv/`, never print keys; never edit the frozen `config/run2.yaml`
  (use a separate manifest + offline backtest and propose the swap); small, useful,
  reversible changes with green tests — no force-push, no history rewrite, no deleting
  runtime data or tests to make things pass.
