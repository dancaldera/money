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

## Measured: capital deployment ladder (2022-01-01 → 2026-09-13, 4.70y)

Same signals, same 8% stop, same fees/slippage (5bps equities / 25bps crypto);
only `position_notional` and the exposure caps change (halt as noted).

| manifest | notional / gross cap | exposure | return | maxDD | trades | expectancy | Sharpe | halt? |
|---|---|---|---|---|---|---|---|---|
| `run2.yaml` (frozen) | $625 / $5k | 5% of equity | +5.20% | -2.38% | 239 | +$19.21 | 0.843 | no |
| `scale-2x.yaml` | $1,250 / $10k | 10% | +10.40% | -4.76% | 239 | +$38.43 | 0.844 | no (0.24pp headroom) |
| `scale-4x.yaml` | $2,500 / $20k | 20% | **-4.30%** | -5.10% | **24** | -$179.06 | -0.691 | **yes, early** |
| `scale-4x-halt10.yaml` | $2,500 / $20k, halt 10% | 20% | +20.80% | -9.47% | 239 | +$76.85 | 0.842 | no |

Findings:

1. **Return scales exactly linearly with deployment while the halt holds.**
   1x → 2x → 4x(halt10) gives 5.20% → 10.40% → 20.80%, and expectancy per trade
   in % of notional is 3.07% at every size. Sharpe is unchanged (0.842–0.844):
   sizing moves dollars, not edge.
2. **The frozen 5% drawdown halt is the binding constraint, and it is a
   one-way latch.** `scale-4x` trips it, after which the simulation "preserves
   exits but makes the entry gate permanently false": trading stops at 24
   trades and equity flat-lines for the remaining ~4.5 years (-4.30% instead of
   ~+20%). The live halt behaves the same way (`ledger.is_halted` blocks new
   entries until a human re-initialises/settles the run).
3. **2x leaves only 0.24pp of headroom** to that latch (-4.76% vs 5%), on a path
   with ±$2.4k single-position swings. A slightly worse live sequence trips it
   and freezes the desk.
4. Expectancy CI90 already crosses zero at 1x ([-1.43, +42.66]) and at every
   size, and buy & hold returned +59.48% over the same window. The per-trade
   edge is a point estimate, not a statistically established one; deployment
   decisions rest on the DD/halt path, not on significance.

## Consequence (human decision)

Sizing is the only lever that changes dollars, and it interacts with the halt
that can permanently stop the desk. Any change to `position_notional` /
exposure caps / `drawdown_halt_pct` is a manifest change of the live run and
needs the human. Options, in order of what the measurements support:

1. Keep size at 1x and accept 5%/4.7y.
2. 2x (`position_notional: 1250`, caps ×2) **together with** a defined halt
   recovery path — otherwise a single drawdown freezes the desk.
3. Raise size only if the halt becomes recoverable (e.g. a documented resume
   procedure with reduced size), which the frozen manifest does not define.

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

Follow-ups, in order of expected dollars: (a) re-simulate `stocks-only` and
`crypto-only` manifests to price the fees the crypto leg really costs; (b) test a
**trailing-P&L tilt** (keep all 17 symbols, size toward trailing winners) — the
one use of finding 2 that adds exposure without dropping trades; (c) the sizing
ladder above, which still needs a halt-recovery answer.

`tests/test_symbol_edge_analysis.py` pins the primitives, including the trap case
where pruning improves expectancy per trade and still loses dollars.
