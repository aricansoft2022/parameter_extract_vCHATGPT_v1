# Exit-mode filtered pre-validation diverse freeze

Use this stage when an operational constraint is declared **before validation**, such as carrying
only TP-exit strategies forward. The filter is applied only to the already-produced discovery
Pareto frontier; it does not rerun search, synthesize parameters, inspect validation, or reveal
holdout.

For the current BTC research run, the discovery frontier contains 113 candidates, of which 102
use `exit_mode=tp`. A request for 39 TP teams therefore selects 39 diverse representatives only
from those 102 eligible TP candidates.

```bash
pextract-freeze-diverse-filtered \
  --search-result discovery-result.json \
  --exit-mode tp \
  --count 39 \
  --output frozen-candidates-39-tp.json
```

The output remains a normal `parameter_extract.frozen_candidate_set` accepted by the existing
validation runner. Its fingerprinted provenance records the original discovery-result SHA-256,
the full frontier count, eligible frontier count, exact exit-mode filter, selected strategies,
selection order, and explicit `validation_accessed=false` / `holdout_accessed=false` flags.

The deterministic diversity method is the same discovery-only k-center method used by the
unfiltered pre-validation freeze, but normalization and seed coverage are computed only inside
the eligible exit-mode pool. Every selected strategy must retain the requested exit mode or the
stage fails closed.
