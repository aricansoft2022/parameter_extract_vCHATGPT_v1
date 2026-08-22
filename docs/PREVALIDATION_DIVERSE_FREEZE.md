# Pre-validation diverse freeze

This optional stage reduces a discovery Pareto frontier to a smaller immutable hypothesis set
**before any validation candle is inspected**. It exists for cases where the research goal is to
carry a deliberately diverse fixed number of teams into validation instead of validating every
Pareto point.

It is not the later family-clustering stage. Family clustering uses behavioral evidence from
both discovery and validation after neighborhood robustness. This stage is intentionally
weaker and earlier: it uses discovery evidence only and never claims that selected strategies
are true behavioral families.

## Contract

`pextract-freeze-diverse` first runs the normal discovery-freeze integrity checks. Therefore the
source must be a discovery-only result with both:

- `validation_accessed: false`
- `holdout_accessed: false`

The selector then:

1. guarantees one strong seed for every exit mode present on the Pareto frontier;
2. guarantees one strong seed for every RSI period present;
3. preserves the four Pareto-objective extremes;
4. fills remaining slots with deterministic farthest-point sampling.

Farthest-point distance gives equal group weight to:

- normalized parameter space; and
- normalized discovery behavior space.

Parameter features include RSI period/entry, ADX bounds, exit-mode indicators and exit value.
Discovery behavior includes aggregate metrics plus each discovery window's return, trade count,
MAE, drawdown, holding and open-at-end state. Every feature is min-max normalized using only the
source Pareto frontier. Distances within each group are RMS distances; the two group distances
are combined with equal weight.

When two candidates have the same distance, existing discovery Pareto evidence is used only as
a deterministic tie break: worst-window return, median-window return, worst MAE, max drawdown,
then trade count. Candidate fingerprint is the final tie break.

No parameter value is synthesized, averaged or changed.

## Output integrity

The output remains a normal `parameter_extract.frozen_candidate_set`, so the existing
`pextract validate-candidates` command accepts it directly. The candidate-set fingerprint pins:

- the original discovery-result SHA-256 and lineage;
- the exact selected strategies and discovery evidence;
- the diversity method and requested count;
- exit-mode and RSI-period coverage counts;
- the complete selection order and selection distances;
- explicit `parameters_retuned: false`;
- explicit `validation_accessed: false` and `holdout_accessed: false`.

## Run

For a 39-team pre-validation set:

```bash
pextract-freeze-diverse \
  --search-result discovery-result.json \
  --count 39 \
  --output frozen-candidates-39.json
```

The command fails closed when the requested count is larger than the source frontier or too
small to preserve the mandatory exit-mode/RSI-period/Pareto-extreme coverage seeds.

## Interpretation

The 39 selected teams are discovery hypotheses, not deployable teams and not validation
winners. Selection does not make validation in-sample: the policy and exact strategies are
frozen before validation. After validation, the normal robustness, family, portfolio and
selection stages still apply, with holdout remaining sealed until their policies are frozen.
