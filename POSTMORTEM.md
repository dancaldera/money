# Post-mortem — the money experiment (2026-09-09 → 2026-10-28)

This repository is the lab notebook of a 7-week experiment: a **paper-only** trading desk
(Alpaca PAPER, fake money) plus the backtesting rig used to justify every parameter on it.
It is archived. This document is the record of what it found, why it stopped, and what is
still worth keeping.

Nothing here is investment advice, and no live-money route was ever wired.

---

## 1. What was running

`run3`, opened 2026-09-19 with $100,000 of paper equity, frozen manifest `config/run3.yaml`:

| Knob | Value |
|---|---|
| Signal | SMA cross 10/30 on daily bars |
| Universe | 9 US equities + 8 crypto pairs |
| Slot | $625 flat, 17 slots, $10,625 gross cap |
| Stop | 8%, fill-derived |
| Halt | 5% drawdown, opt-in recovery at 2.5% / 20 days |
| Guards | exposure caps (8 crypto / 9 stock), correlation ≥0.80, 60-day window |
| Fees modelled | crypto 25 bps taker, 5 bps equity slippage |

Everything ran from Hermes cron on a Mac: a 18:35 CST daily scan, a 30-minute stop monitor,
an 08:31 Mon–Fri stock executor, an hourly health watchdog, a 22:00 catch-up, and two
agent jobs (09:00 brief, 08:00 improvement).

## 2. What it did live

45 sessions, 2026-09-19 → 2026-10-27.

```
equity          $99,958.25        return  -0.04%      maxDD  -0.54%
benchmark       $97,495.61        return  -2.50%      maxDD  -6.18%
deployed        $1,268 (1.27% of equity) · 2 of 17 slots
closed trips    7    win 28.6%    expectancy -$7.64    net -$53.47
fees booked     $12.15
by leg          crypto 4 trips, 1 win, net -$77.42 | stock 3 trips, 1 win, net +$23.94
open            AAPL +5.68%, AMZN -2.73%
```

Only one halt: `reconciliation_failed` on NVDA on 2026-10-21, a same-second fill race, cleared
after re-reconcile with `run-rearm`.

**The live record says nothing about the strategy, and that is the point.** Against the
replayed control (366 closed trips, $25.75/trade, sd $251.41) the live gap is −0.35σ and
P(wins ≤ 2) = 0.65 — indistinguishable from noise. Reaching a 2σ read at the observed cadence
of 1.27 trips/week needs ~227 round trips, i.e. **3.4 years**. The desk was working correctly;
it was simply too slow to answer the question it was built to answer.

What it did prove is *parity*: 9 of 9 entry intents filled, 0 blocked by any guard, fill gap
mean +0.467%, no skipped bar, fees matching the model, coverage clean on all but one evening.

## 3. What the lab actually settled

This is the real output — every lever was measured on one clock against a replayed control
(`exp-slots-all-recover`, 2022-01-01 → 2026-10, 1x basis: +10.6% / maxDD −2.79% / 366 trades /
expectancy $25.75). Full tables in `docs/experiments.md`.

| Lever | Verdict | Headline |
|---|---|---|
| Size (1x → 2x → 3x) | **open, human decision** | 2.17% / 4.04% / ~5.4% CAGR; the only lever that moves dollars |
| Breadth (17×$625 vs 8×$1,250) | keep 17 | 96% of the return for 59% of the drawdown |
| Stop width | keep 8% | tightening costs $4–4.8k; loosening is noise that flips sign |
| Take-profit | **closed** | 24% TP → −$6,979 net; clips winners, freed slots don't earn it back |
| Time stop | **closed** | −$519 (60 bars) to −$4,857 (10 bars); the long holds are the P&L |
| Re-entry after stop | **closed** | −$1.2k to −$2.3k net; freezes `require_fresh_cross_after_stop` |
| Per-asset notional tilt | **closed** | loses in both directions — $625 is the unique cap-coincident notional |
| Volatility targeting | **closed** | −$681 to −$5,666; buys drawdown, never dollars |
| Limit (maker) entry | **closed** | equity leg loses 60–82% of P&L to missed gap-ups |
| Correlation guard | keep | 48 of 79 blocks, but relaxing it costs ret/DD 3.92 → 3.29 |
| SMA pair | keep 10/30 | interior optimum; 3/15 gets 2.3x the trades and −$2,953 |
| Whipsaw gate | **closed** | −60% of trades for −60% of dollars, expectancy unchanged |
| Symbol pruning | **closed** | top-k raises per-trade expectancy and still loses $1.6–2.8k |
| Entry timing (18:35 CST) | keep | crypto bar closes 00:00 UTC; measured drift on stock entries is noise |

The pattern is consistent and is the experiment's central finding: **the desk is capital-
constrained, not signal-constrained.** Every filter that removed trades removed dollars at the
same rate, and every allocation change that looked like edge was a drawdown trade, not a return
trade. The exchange rate across all of them was ≈$1.7–2.7k of P&L per 1pp of max drawdown.

## 4. Operational findings (the engineering half)

Bugs that only appear when the thing actually trades:

- **Crypto fees are booked in kind.** Alpaca deducts the taker fee from the asset received, so
  recording the raw activity qty leaves the ledger 25 bps richer than the broker forever and the
  fail-closed qty check halts the run on the next crypto entry. Cost 13h halted.
- **That fee is stored twice** — fill-time plus Alpaca's own `CFEE` activity ~4h later. A plain
  `SUM` reported 2× the truth ($9.14 vs $4.57).
- **Same-second fill races** produce a transient `reconciliation_failed`: the executor's
  reconcile sees the position before Alpaca posts the FILL activity. Fixed with a 2s re-read on
  qty-only mismatches.
- **Dead alert branches.** Wrappers that end a block in `heartbeat` or `|| true` swallow the exit
  code, so `rc -ne 0` alerts never fired and a failed slot reported "ok". Fixed with `run_step`
  + `replay_since`.
- **A bar is only evaluated when it is the newest closed bar.** A skipped bar is a permanent
  signal loss. The `coverage_*` lines, the hourly watchdog, the 22:00 catch-up and a
  confirm-on-a-later-tick salvage rule all exist because of this.
- **The scheduler inherits the host's sleep state.** On 2026-10-25 the Mac was in clamshell
  sleep across the 18:35 slot; the job fired at 20:21 when the machine woke, and the watchdog
  logged two UNHEALTHY ticks for a bar that was still evaluable. No data was lost — but the
  failure was invisible in the job status, which said "completed".
- **The FIFO 400-day cache key must be month-stable**, or a broker/DNS failure has no fallback
  file to serve and the whole scan dies — the one failure that loses a bar permanently.

## 5. Why it stopped

1. **Ceiling.** 2.17%/yr at 1x, 4.04%/yr at 2x, with 366 trades over 4.7 years and a halt rule
   that parks the book flat for 20 sessions. Below what risk-free paper paid over the same
   window. There is no size at which this becomes a business.
2. **No open questions.** Every parameter has a measured verdict (§3). The remaining "lever" —
   size — does not help an experiment at all: the per-trade gap in σ is scale-invariant, so
   doubling size doubles the fake dollars and the noise together.
3. **No resolution on a useful horizon.** 3.4 years to a 2σ read is attention spent on a coin
   flip, for a question the replay already answered inside its own error bars.

The desk was not broken. It was finished.

## 6. What survives

- `docs/experiments.md` — every measured table, with the clock each row was replayed on.
- The wrapper/scheduling contract in `scripts/` and `docs/local-ops.md` — the retry, heartbeat,
  replay-to-stdout and salvage rules are reusable for any scheduled job, trading or not.
- The ledger (`results/run3/ledger.sqlite`, ~11 MB) is **not** in this repo (public repo, and
  runtime data is gitignored). It was archived locally before the working tree was deleted.
- Private keys and credentials were never committed and were moved to a local secret store.

## 7. Closing state

- Repository archived 2026-10-28, read-only.
- All 8 Hermes cron jobs and their wrapper shims removed from the host.
- Paper account left with its two open positions; no orders, no live route, no further scans.

— Daniel Caldera, 2026-10-28
