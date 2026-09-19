# Parameter experiments (research only)

`config/run2.yaml` is the frozen manifest of the audited live paper run. Editing
it changes the stored hash and breaks every command, so parameters are explored
in **separate manifests** under `config/experiments/` and replayed read-only.

## Guard rails

- A manifest under `config/experiments/` must use its **own `run_id`**; the live
  `run2` id is refused.
- Live commands (`scan`, `paper-run`, `execute-intents`, `paper-stops`,
  `reconcile`, `run-init`, …) load the manifest with the **strict** loader and
  still reject any value that differs from the frozen ones. An experiment
  manifest cannot reach a broker.
- `portfolio-backtest` is the research path: it loads with `strict=False`
  (parameters may differ; safety checks, `paper_only`, no margin, exposure
  consistency and uniqueness still apply) and writes to
  `results/<run_id>/portfolio-backtest/` (gitignored).
- Artifacts from an experiment are never the live run's evidence. The printed
  banner says so, and `summary.json` carries its own `run_id` + `config_hash`.

## Running one

```bash
.venv/bin/money portfolio-backtest --run-config config/experiments/scale-2x.yaml
cat results/exp-scale-2x/portfolio-backtest/summary.json
```

`--run-id` is now optional here and only cross-checks the manifest; omit it.

## Measured: capital deployment ladder (2022-01-01 → 2026-09-17, 4.71y)

Same signals, same 8% stop, same fees/slippage (5bps equities / 25bps crypto);
only `position_notional`, the exposure caps and the halt/recovery rule change
(columns below). All rows replayed on the same cached data.

| manifest | notional / gross cap | halt | recovery | return | maxDD | trades | expectancy | Sharpe |
|---|---|---|---|---|---|---|---|---|
| `scale-1x.yaml` (copy of run2 values) | $625 / $5k | 5% | latch | +5.29% | -2.38% | 240 | +$19.32 | 0.854 |
| `scale-2x.yaml` | $1,250 / $10k | 5% | latch | +10.57% | -4.76% | 240 | +$38.65 | 0.854 |
| `scale-2x-recover.yaml` | $1,250 / $10k | 5% | dd ≤2.5% or 20d | +10.57% | -4.76% | 240 | +$38.65 | 0.854 |
| `scale-4x.yaml` | $2,500 / $20k | 5% | latch | **-4.30%** | -5.10% | **24** | -$179.06 | -0.690 |
| `scale-4x-recover-10d.yaml` | $2,500 / $20k | 5% | dd ≤2.5% or 10d | **+21.50%** | -9.19% | 233 | +$81.16 | 0.697 |
| `scale-4x-recover.yaml` | $2,500 / $20k | 5% | dd ≤2.5% or 20d | +16.82% | -9.66% | 232 | +$61.35 | 0.696 |
| `scale-4x-recover-60d.yaml` | $2,500 / $20k | 5% | dd ≤2.5% or 60d | +20.53% | -8.77% | 217 | +$82.66 | 0.697 |
| `scale-4x-halt10.yaml` | $2,500 / $20k | 10% | latch | +21.14% | -9.47% | 240 | +$77.29 | 0.694 |

`scale-1x.yaml` is a copy of the frozen run's numbers under its own run_id, because
`portfolio-backtest` refuses to replay the live `run2` (it is the research path);
it is the 1x reference row, not the live run. Re-run any row with:

```bash
.venv/bin/money portfolio-backtest --run-config config/experiments/<manifest>
```

Findings:

1. **Return scales linearly with deployment while the halt holds.** 1x → 2x gives
   5.29% → 10.57%, and the 4x variants that survive the halt reach +16.8% to
   +21.5%. Expectancy per trade in % of notional is 3.07% at every size: sizing
   moves dollars, not edge.
2. **The frozen 5% drawdown halt was the binding constraint, and as a one-way
   latch it was fatal.** `scale-4x` trips it and trading stops at 24 trades:
   equity flat-lines at -4.30% for the remaining ~4.2 years. The live halt
   behaved the same way.
3. **A recoverable halt removes that failure mode and turns the dead desk back
   into the ladder.** Same 5% halt, same size, only
   `halt_recovery_drawdown_pct: 2.5` + `halt_recovery_days: <n>` added:
   -4.30% (24 trades) → **+16.8% to +21.5%** (217-233 trades). The three cooldown
   settings (10/20/60 days) all land in that range with maxDD -8.8% to -9.7%, so
   the result does not rest on a tuned constant.
4. **Recovery costs nothing on the paths that never halt.** `scale-2x-recover` is
   bit-identical to `scale-2x` (same return, DD, trades, expectancy) — the rule
   only fires after a halt, so it is insurance, not a new bet. It matters because
   2x sits only 0.24pp from the halt (-4.76% vs 5%).
5. **Recovery matches the "raise the halt" alternative without relying on
   luck.** `scale-4x-halt10` (+21.14%, DD -9.47%) wins by setting the halt where
   it never trips; if it ever did, that variant freezes permanently too. The
   recovery variants reach the same return/DD region *and* define what happens
   after a halt.
6. **Two design facts, both measured, that any recovery rule must respect.**
   (a) A drawdown-only recovery rule is inert: the halted desk is flat, so its
   equity cannot rise and the drawdown never falls back — identical output to the
   latch. The calendar cooldown is what actually fires. (b) Without re-baselining
   the drawdown at the resume, the peak that was already lost keeps the halt
   condition true: the desk resumed only every 20th day (96 trades, +4.71%).
   `ledger.high_water(..., since=resume_time)` / the replay's
   `high_water = equity` re-baseline is what makes it resume properly (232
   trades).
7. Expectancy CI90 still crosses zero at every size, and buy & hold returned
   +60.48% over the same window. The per-trade edge is a point estimate, not a
   statistically established one; deployment decisions rest on the DD/halt path,
   not on significance.

## Consequence (human decision)

Sizing is the only lever that changes dollars, and it interacts with the halt
that can stop the desk. Any change to `position_notional` / exposure caps /
`drawdown_halt_pct` / `halt_recovery_*` is a manifest change of the live run and
needs the human — the strict loader pins all of them (including
`halt_recovery_*` to absent) and the ledger's stored hash rejects an edit of
`run2.yaml`, so adopting recovery live required a **new run_id**, not an edit of
the audited run.

**Done 2026-09-19:** `run3` was opened on the breadth row (`config/run3.yaml`:
17 slots x $625, gross $10,625) with the recovery rule (2.5% / 20d) included from
day one — its recovery variant was verified bit-identical to the latch on this
path (`exp-slots-all-recover`). The live loader learned per-run pinned baselines
(`_FROZEN_BASELINES` in `src/trading/run2/config.py`): registered live runs pass
with their own values, everything else still must match the audited baseline or
replay read-only, so experiment manifests stay broker-unreachable.
Deployment has a second dimension this ladder did not move — *breadth* (how many
slots the same gross exposure is spread over). It is measured in the slot ladder
below, and it is the cheaper of the two levers on drawdown.

Options, in order of what the measurements support:

1. Keep size at 1x and accept ~+5.3%/4.7y.
2. 2x (`position_notional: 1250`, caps ×2) with a halt-recovery rule defined from
   day one: ~+10.6%/4.7y on the replayed path, ~+17-21% if a 4x-sized variant is
   ever wanted, and no permanent freeze when the drawdown arrives.
3. Raise the halt to 10% instead: same region (+21.1%) but no answer for what
   happens if the 10% is ever reached.

## Measured: does symbol selection beat keeping the universe? (says no)

`scripts/analysis_symbol_edge.py` attributes a portfolio-backtest's realized P&L
to symbols and asset classes, and — more usefully — tests whether the ranking
persists, because "drop the losers" is the change hindsight always recommends.

```bash
.venv/bin/python scripts/analysis_symbol_edge.py \
    --artifact results/exp-scale-2x/portfolio-backtest --notional 1250
```

239 closing trades, 2022-02-11 → 2026-09-01, `scale-2x`. Walk-forward: at each of
137 split dates rank symbols by *trailing* mean realized P&L, keep the top k, and
score the same future window with and without them.

| k | per-trade edge | splits where subset has better expectancy | **total** edge | subset total | universe total | random-k total | vs random | trades kept/all |
|---|---|---|---|---|---|---|---|---|
| 5 | +$116.23 | 87.6% | **-$2,788** | $2,318 | $5,106 | $1,680 | +38.0% | 26/104 |
| 8 | +$34.34 | 73.7% | **-$2,181** | $2,925 | $5,106 | $2,647 | +10.5% | 45/104 |
| 11 | +$10.69 | 62.8% | **-$1,573** | $3,533 | $5,106 | $3,133 | +12.8% | 67/104 |

Per asset class (same run): crypto 129 trades (54% of activity) → +$1,315 (14% of
P&L) and $408 of the $481 fees (85%), mean **+0.82%** of notional per trade;
stocks 110 trades → +$7,868 (86% of P&L), $73 fees, mean **+5.72%** per trade.

Findings:

1. **Pruning by trailing P&L loses money at every k.** Expectancy per trade rises
   ($10.69 → $116.23 vs the universe) but the subset trades 26-67 times against
   104, and the total is $1.6k-$2.8k below just keeping everything. The desk's
   dollars come from *how many* positions it takes, not from picking winners —
   the same conclusion as the sizing ladder, from the other direction.
2. **The ranking is not pure noise, and still not usable.** The top-k subset beats
   a random same-size subset by +10% to +38% of total P&L, but loses to the full
   universe. If anything, the signal is an argument for *tilting* size toward
   trailing winners, never for deleting symbols.
3. **The ranking does not persist.** Split-half per-symbol P&L: pearson -0.16,
   spearman -0.01, top-5 overlap 1/5. The 2022-2026 leaderboard (AMD +$3.0k,
   DOGE +$2.3k, AVAX -$1.0k, ETH -$0.7k) is in-sample ranking, not a stable edge.
   Removing AVAX/ETH/LTC/MSFT/GOOGL would have added +$3.1k *in sample* and is
   exactly the kind of change this test exists to block.
4. Crypto pays 25bps taker against the equity leg's 5bps, and 54% of the trades
   absorb 85% of the fees for 14% of the P&L — but the half-samples disagree
   (crypto -$644 in the first half, +$1,960 in the second), so that is a cost
   structure observation, not a demonstrated edge gap. Testing it needs a
   *re-simulated* subset (its own manifest), because this script re-uses trades
   the full universe generated and cannot see how cash, caps and the correlation
   guard would re-route a smaller universe.

Follow-ups, in order of expected dollars: (a) **done** — the legs are re-simulated
in "Measured: the legs" below (crypto's isolated contribution is thin, +0.94% over
4.7y; stocks carry the run); (b) test a
**trailing-P&L tilt** (keep all 17 symbols, size toward trailing winners) — the
one use of finding 2 that adds exposure without dropping trades; (c) the sizing
ladder above — its halt-recovery blocker is now measured (see
`scale-*-recover.yaml`), so the next step there is a human decision on a new
run_id, not more research.

`tests/test_symbol_edge_analysis.py` pins the primitives, including the trap case
where pruning improves expectancy per trade and still loses dollars.

## Measured: deployment breadth (slots) vs size (same 4.71y window, through 2026-09-18)

The ladder above moved `position_notional` with the caps, so it measured *dollars
per trade* and left the slot count at 8. But the frozen desk is capped twice over:
`max_positions: 8` and `max_gross_exposure: 5000` are the same constraint at $625
(8 × 625 = 5000), and the replay now records which guard killed each fresh cross
(`decisions.csv` → `blocked_by`, same names as the live gate in
`trading.run2.risk.entry_allowed`).

```bash
.venv/bin/python -c "import pandas as pd; d=pd.read_csv('results/exp-scale-1x/portfolio-backtest/decisions.csv'); \
print(d[d.signal=='BUY'].groupby(['asset','blocked_by'], dropna=False).size())"
```

On the 1x reference replay: **458 BUY signals → 253 actionable, 205 (45%)
suppressed** — `max_gross_exposure` 134 (65% of the blocks), `max_crypto_exposure`
36, `max_stock_exposure` 13, `correlation_cap` 11, position counts 10. So the
desk is throttled by total deployment, not by symbol quality, and the correlation
guard — the obvious suspect — costs 11 signals in 4.7 years: leave it alone.

Breadth ladder (`position_notional` $625, caps scaled to whole-universe slots):

| manifest | notional / slots | gross cap | return | maxDD | trades | win% | expectancy | Sharpe | PF |
|---|---|---|---|---|---|---|---|---|---|
| `scale-1x.yaml` (frozen values) | $625 / 8 | $5,000 | +5.35% | -2.38% | 242 | 29.8% | +$19.75 | 0.863 | 1.67 |
| `exp-slots-2x.yaml` | $625 / 16 | $10,000 | +6.65% | -2.79% | 379 | 30.3% | +$15.64 | 0.730 | 1.53 |
| `exp-slots-all.yaml` | $625 / 17 | $10,625 | +10.25% | -2.79% | 364 | 30.2% | +$26.18 | 0.776 | 1.88 |
| `scale-2x.yaml` (same gross as the two above) | $1,250 / 8 | $10,000 | +10.70% | -4.76% | 242 | 29.8% | +$39.49 | 0.863 | 1.67 |
| `exp-slots-all-2x.yaml` | $1,250 / 17 | $21,250 | **-4.47%** | -5.23% | **70** | 17.1% | -$63.91 | -0.696 | 0.16 |

Findings:

1. **At the same $10k of gross exposure, spend it on breadth, not size.** 8 slots
   of $1,250 return +10.70% with a -4.76% drawdown; 17 slots of $625 return
   +10.25% — 96% of the return — with a -2.79% drawdown, i.e. 59% of the risk.
   Return per unit of drawdown 3.67 against 2.25. The size row sits 0.24pp from
   the 5% halt; the breadth row keeps 2.21pp.
2. **Breadth buys trades, and the marginal trade is average, not worse.**
   8 → 17 slots turns 205 suppressed crosses into live ones: 242 → 364-379 trades
   at the frozen $625 (`win%` unchanged at ~30%). Per-trade expectancy does not
   rise (marginal trades are average, exactly as `analysis_symbol_edge.py`
   predicted); the total does, because the desk takes 50% more of them.
3. **One slot is not a trend.** `exp-slots-2x` (16) and `exp-slots-all` (17) differ
   by a single stock slot yet land at +6.65% and +10.25% — single-row paths are
   noisy. The robust claim is the row *against its same-gross alternative*, not
   the ordering among slot variants.
4. **More deployment still dies on the one-way halt.** `exp-slots-all-2x` ($21,250
   gross, 17 slots) trips the 5% latch at trade 70 and freezes at -4.47%. Same
   lesson as `scale-4x`: any step up in dollars needs the halt-recovery rule
   (`halt_recovery_drawdown_pct` / `halt_recovery_days`) defined from day one, or
   the desk is one bad week from a permanent stop.
5. Expectancy CI90 still crosses zero and buy & hold still wins (+63.5% over the
   window). These rows choose between risk paths, not between proven edges.

**Consequence:** run3 (opened 2026-09-19) implements this first step — breadth at
the frozen size (17 slots, $10,625 gross) plus the recovery rule as insurance;
run2 stays at 1x/8 slots as the archived predecessor. Any *further* size increase
should still ship with a re-measured risk review.

## Measured: the legs, re-simulated (stocks-only / crypto-only)

Cheap follow-up (a) from the symbol-selection section: the same parameters with
one leg switched off by zeroing its caps (the loader forbids empty watchlists) —
`exp-stocks-only.yaml` / `exp-crypto-only.yaml`, 2022-01-01 → 2026-09-18.

| manifest | return | maxDD | trades | win% | expectancy | Sharpe |
|---|---|---|---|---|---|---|
| `scale-1x.yaml` (both legs) | +5.35% | -2.38% | 242 | 29.8% | +$19.75 | 0.863 |
| `exp-stocks-only.yaml` | +3.21% | -1.00% | 130 | 35.4% | +$23.26 | 0.900 |
| `exp-crypto-only.yaml` | +0.94% | -1.72% | 149 | 24.2% | +$4.24 | 0.208 |

Findings: crypto's isolated contribution is thin (+0.94% over 4.7y, Sharpe 0.21,
expectancy $4.24 while paying 25bps) and stocks carry the run (best win rate and
expectancy of the three); the legs sum to +4.15% against the joint +5.35%, so the
full desk also benefits from interaction (shared cash, caps, correlation routing).
This is not an argument to prune a leg outright — the same trap as symbol pruning —
but it prices why the crypto fee bill is worth watching, and it sets the bar any
crypto-only variant would have to beat.

## Measured: stop refinements — breakeven helps a little; trailing hurts

The 8% stop is fixed and fill-derived: a deep winner like AMD (+15.9% on
2026-09-19) still has its stop 8% *below entry*, so nothing protects the gain
above it. Two experiment-only knobs were added to measure refinements without
touching a live run (both pinned absent for `run2`/`run3`, enforced by a test):

- `stop_breakeven_at_pct: X` — once a close is ≥ +X%, the stop moves up to entry;
- `stop_trail_pct: Y` — the stop trails Y% under the highest close since entry.

Replays on the run3 basis (`exp-slots-all-recover` = control), 2022-01-01 → 2026-09-18:

| variant | return | maxDD | trades | win% | expectancy | Sharpe | DSR |
|---|---|---|---|---|---|---|---|
| control (no knob) | +10.25% | -2.79% | 364 | 30.2% | +$26.18 | 0.776 | 0.914 |
| breakeven @ +6% | +9.48% | -2.52% | 371 | — | +$23.81 | 0.762 | 0.930 |
| breakeven @ +8% | +9.95% | -2.62% | 367 | — | +$25.35 | 0.774 | 0.922 |
| breakeven @ +10% | **+10.51%** | **-2.53%** | 366 | 26.0% | **+$26.95** | **0.804** | 0.932 |
| breakeven @ +12% | +10.31% | -2.60% | 364 | — | +$26.33 | 0.786 | 0.921 |
| breakeven @ +15% | +10.09% | -2.81% | 364 | — | +$25.73 | 0.766 | 0.909 |
| trailing @ 6% | +2.60% | -1.06% | 401 | 36.2% | +$6.08 | 0.651 | 0.725 |

Read: a 6% daily-close trail is destructive — it stops winners out inside normal
noise (its expectancy CI90, [-0.57, +12.75], includes zero; 37 extra round trips
for a third of the return). Breakeven is mildly useful, but only set high
(+10–12%): +0.2–0.3pp of return and drawdown versus control; set low (+6–8%) it
cuts winners early and costs return. The effect is small and not conclusive
(DSR ~0.93 for all variants), but its sign is consistent across
return/DD/expectancy/Sharpe at +10–12%.

**Consequence:** neither knob is adoptable by edit (both are pinned absent on
every live run). Adoption would ride a future run decision; breakeven@10 is the
measured candidate, the trail is rejected. Until then the fixed 8% stop remains
the desk's only downside rule and AMD-style gains stay unprotected above entry.
