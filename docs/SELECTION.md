# Portfolio selection

The selection stage turns one explicit full-portfolio replay into a frozen KEEP/DROP decision without opening a new subset-optimization loop.

It consumes:

- the same `study.json` used upstream;
- the exact `families-result.json` that supplied the frozen representatives;
- the exact `portfolio-result.json` produced from those representatives;
- a predeclared `selection.json` pinned to that portfolio result by SHA-256.

Holdout is not available to this stage.

## Why this is one-pass

Testing every possible subset would create another large optimization surface on validation data. V1 therefore uses a deliberately restricted leave-one-out diagnostic:

1. replay the complete portfolio and require it to reproduce the supplied portfolio result exactly;
2. remove each family representative once, independently, while keeping every other representative and the same slot count;
3. measure that representative's marginal effect against the full portfolio;
4. apply the predeclared gates once;
5. KEEP or DROP every representative simultaneously;
6. replay the resulting selected set once, preserving the original relative priority order.

A DROP does not trigger another round of leave-one-out testing. There is no greedy deletion loop, permutation search, parameter retuning or priority reoptimization.

## Contract

Example `selection.json`:

```json
{
  "schema_version": 1,
  "name": "BTC portfolio selection v1",
  "source_portfolio_result_sha256": "COPY_SHA256_OF_portfolio-result.json",
  "gates": {
    "min_discovery_marginal_return_pct": 0.0,
    "min_validation_marginal_return_pct": 0.0,
    "min_validation_accepted_entries": 3,
    "max_validation_drawdown_worsening_pct": 1.0,
    "max_validation_contention_added_fraction": 0.10
  }
}
```

The thresholds are research policy, not universal defaults. They should be written before inspecting the leave-one-out results.

## Marginal evidence

For candidate `i`, V1 compares the full portfolio with the portfolio containing every representative except `i`.

It records:

- `discovery_marginal_return_pct`;
- `validation_marginal_return_pct`;
- `validation_accepted_entries` for the candidate in the full portfolio;
- `validation_drawdown_worsening_pct` attributable to keeping the candidate;
- slot contention added to *other* families.

The contention denominator excludes the candidate's own signals. A candidate being blocked by higher-priority families is not counted as harm caused by that candidate.

## Priority semantics

The source `portfolio.json` declares priority. Selection never searches priorities.

If some families are dropped, the survivors retain their original relative order. The output also stores each survivor's `original_priority`; compact output priorities are only 1..N labels for the reduced list.

## Integrity gates

Before evaluation, selection verifies that:

- the selection contract SHA-pins the exact portfolio result;
- the portfolio result SHA-pins the exact families result supplied to the command;
- study, dataset, symbol and execution assumptions agree across the chain;
- portfolio family/strategy/fingerprint rows exactly match the frozen family representatives;
- a fresh full replay reproduces the stored portfolio windows and phase aggregates.

The generated selection result then self-verifies:

- selection contract fingerprint;
- strategy fingerprints;
- KEEP/DROP and selected-set consistency;
- selected-set fingerprint;
- preserved relative priority;
- selected portfolio window aggregates;
- absence of holdout access, leverage, parameter retuning, iterative subset search and priority reoptimization.

## CLI

```bash
pextract select-portfolio \
  --study study.json \
  --families-result families-result.json \
  --portfolio-result portfolio-result.json \
  --selection selection.json \
  --data-directory . \
  --output selection-result.json
```

## Interpretation

A KEEP result means only that the frozen representative survived this predeclared development-stage portfolio policy. It is not yet a deployable team and it is not evidence of future profitability.

The next research boundary is the sealed holdout. The selected-set fingerprint should be frozen before holdout is revealed. If holdout is then used to retune gates, restore dropped representatives, alter priorities or change strategy parameters, it has ceased to be a holdout and a new untouched period is required.
