# Discovery search v1

The first search implementation is deliberately **correctness-first**, not a replacement
for the later factorized high-throughput engine. Every candidate is replayed through the
truth engine. Use coarse ranges and the candidate safety cap; do not point this version at
a 16-million-row grid.

The search path is allowed to inspect **discovery windows only**. It does not call
validation or holdout. Its output records `validation_accessed: false` and
`holdout_accessed: false` as an audit assertion.

## Search contract

Example `search.json`:

```json
{
  "schema_version": 1,
  "name": "BTC coarse discovery",
  "exit_modes": ["tp", "rsi"],
  "min_adx_width": 4.0,
  "ranges": {
    "rsi_period": [14, 15, 16, 17, 18, 19],
    "rsi_entry": {"start": 24.0, "stop": 42.0, "step": 2.0},
    "adx_min": {"start": 10.0, "stop": 30.0, "step": 4.0},
    "adx_max": {"start": 30.0, "stop": 60.0, "step": 6.0},
    "tp_price_pct": {"start": 0.25, "stop": 2.0, "step": 0.25},
    "rsi_exit": {"start": 55.0, "stop": 80.0, "step": 5.0}
  },
  "gates": {
    "min_total_trades": 30,
    "min_positive_window_fraction": 0.5,
    "min_worst_window_return_pct": -5.0,
    "min_worst_mae_pct": -12.0
  },
  "refinement": {
    "enabled": true,
    "step_divisor": 2,
    "radius_steps": 1,
    "max_seeds": 20,
    "max_candidates": 50000
  }
}
```

The numeric gate values above are examples, **not recommended trading thresholds**. They
belong to the study design and should be chosen before looking at validation or holdout.

Run:

```bash
pextract search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-search.json
```

## What the search selects

The search does not rank candidates by net profit. First, candidates must pass explicit
sample/downside gates. Survivors are reduced to a Pareto frontier with four objectives:

- maximize the worst discovery-window return;
- maximize median discovery-window return;
- maximize worst MAE (less negative is better);
- maximize maximum closed-equity drawdown (less negative is better).

A high-return/high-risk candidate and a lower-return/lower-risk candidate can therefore
both survive. A candidate disappears only when another candidate is at least as good on
all four objectives and strictly better on at least one.

`compounded_window_return_pct` is reported but is deliberately **not** a Pareto objective.
The frontier has a deterministic reporting/refinement order, but that order is not a
scalar fitness score and must not be interpreted as "rank #1 is best".

## Coarse-to-fine behavior

1. Enumerate the coarse grid.
2. Replay every unique coarse candidate on discovery windows only.
3. Apply the explicit gates.
4. Build the Pareto frontier.
5. Pick up to `max_seeds` frontier rows in deterministic conservative order.
6. Refine the continuous dimensions around those seeds using `step / step_divisor`.
7. Replay refined candidates through the same truth path.
8. Recompute gates and the Pareto frontier across coarse + refined candidates.

RSI period is not interpolated during refinement. TP and RSI-exit branches stay separate.
ADX pairs narrower than `min_adx_width` are never generated.

## Safety cap

`max_candidates` is a hard failure boundary, not a truncation setting. If the coarse grid
is already above it, the run refuses to start. If refinement would exceed it, the run
fails rather than silently returning a partial search and pretending it was exhaustive.

This is intentional. For very large searches, the next engineering step is a parity-tested
factorized accelerator, not simply raising the cap until the truth engine takes days.

## Reproducibility

The output carries:

- the complete normalized `search_spec`;
- `search_fingerprint_sha256`;
- the study fingerprint;
- the dataset fingerprint;
- the literal execution model inherited from the study;
- the Pareto objective declaration;
- counts for coarse, refined, evaluated, gated and frontier candidates.

The search output contains the frontier, not every rejected candidate. A later run-bundle
layer should persist the complete candidate table in a columnar artifact before we scale
searches substantially.

## What comes next

Search output is still discovery material, not a deployable team set. The next layer must:

1. freeze the selected discovery candidates;
2. evaluate them on validation with **no parameter retuning**;
3. measure parameter-neighborhood stability;
4. cluster near-equivalent parameter/signal families;
5. only after those decisions are frozen, reveal holdout.

No `teams.csv` exporter should promote discovery-frontier rows directly into the live bot.
