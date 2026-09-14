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
