# Discovery → validation promotion

Discovery output is not deployable. This layer turns the discovery Pareto frontier into a
fingerprinted, immutable candidate set and then evaluates those exact strategies on
validation windows without parameter retuning.

Holdout is not available anywhere in this workflow.

## 1. Freeze discovery candidates

```bash
pextract freeze-candidates \
  --search-result discovery-search.json \
  --output frozen-candidates.json
```

The frozen file contains:

- the SHA-256 of the discovery-search result file;
- search, study and dataset fingerprints;
- the exact execution assumptions;
- one SHA-256 fingerprint per normalized strategy;
- the discovery metrics carried forward as historical evidence;
- one fingerprint over the complete candidate set;
- `parameters_frozen: true`.

The frontier is frozen as a set. It is sorted by candidate fingerprint, not discovery
profit rank. No parameter is changed during freezing.

If any strategy value is edited later, candidate-set verification fails before validation.

## 2. Define validation gates before viewing validation results

Example `validation.json`:

```json
{
  "schema_version": 1,
  "name": "BTC validation gate v1",
  "source_candidate_set_fingerprint_sha256": "COPY_FROM_FROZEN_CANDIDATES",
  "gates": {
    "min_total_trades": 20,
    "min_positive_window_fraction": 0.5,
    "min_median_window_return_pct": 0.0,
    "min_worst_window_return_pct": -3.0,
    "min_worst_mae_pct": -10.0,
    "max_open_at_end_windows": 1
  }
}
```

Those numeric values are examples, **not recommended thresholds**. The important rule is
that the validation contract is written and fingerprinted before validation output is
inspected. If the gates are changed after seeing validation, that is a new experiment and
must receive a new validation fingerprint.

## 3. Validate the frozen set

```bash
pextract validate-candidates \
  --study study.json \
  --candidates frozen-candidates.json \
  --validation validation.json \
  --data-directory . \
  --output validation-result.json
```

The runner checks all provenance before evaluating a candle:

- candidate-set self-fingerprint;
- every individual strategy fingerprint;
- candidate-set fingerprint pinned by `validation.json`;
- current study fingerprint equals the study that produced discovery;
- dataset fingerprint is unchanged;
- symbol and execution assumptions are unchanged.

Then each exact strategy is evaluated with `phases=("validation",)` only.

The result explicitly records:

```text
parameters_retuned = false
discovery_accessed = false
validation_accessed = true
holdout_accessed = false
```

Every frozen candidate remains in the result. Candidates are marked `PASS` or `REJECT`,
and rejected candidates carry machine-readable reasons such as:

- `INSUFFICIENT_TRADES`
- `POSITIVE_WINDOW_FRACTION`
- `MEDIAN_WINDOW_RETURN`
- `WORST_WINDOW_RETURN`
- `WORST_MAE`
- `OPEN_AT_END_WINDOWS`

There is no validation-time optimizer and no automatic parameter repair.

## What PASS means

PASS means only that a discovery hypothesis survived the predeclared validation gates. It
does **not** mean the candidate is ready for the live bot.

The next layers should test parameter-neighborhood stability and remove near-equivalent
parameter/signal families before any holdout reveal or `teams.csv` export.
