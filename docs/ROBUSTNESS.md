# Neighborhood robustness

Neighborhood robustness is a **diagnostic after validation**, not another optimizer.

The center candidates are the exact `PASS` rows from a pinned validation-result file. Their
parameters remain frozen. The tool creates small one-axis-at-a-time variants around each
center and asks whether the local area behaves reasonably on discovery and validation.
A neighbor can never replace, promote, or retune the center candidate.

## Why axis neighbors first

V1 varies only one coordinate at a time:

- `rsi_period`: ±1 when enabled and still inside 14..19;
- `rsi_entry`: ± configured step;
- `adx_min`: ± configured step;
- `adx_max`: ± configured step;
- TP candidates: `tp_price_pct` ± configured step;
- RSI-exit candidates: `rsi_exit` ± configured step.

Invalid strategies are discarded by the same `StrategySpec` validation used elsewhere.
This keeps the diagnostic interpretable: a failure can be tied to one parameter axis instead
of being hidden inside a Cartesian cloud of simultaneous changes.

## Integrity chain

A robustness contract pins the exact validation-result file by SHA-256. Before any neighbor
is evaluated, the tool additionally checks:

- validation-result schema/kind and phase-access flags;
- `parameters_retuned == false`;
- the embedded validation contract and its fingerprint;
- candidate/promoted/rejected counts;
- every center candidate's strategy fingerprint;
- duplicate strategy fingerprints;
- study, dataset, symbol and execution assumptions against the current verified study.

A mutated validation row therefore cannot silently become a new center simply because the
robustness JSON was pointed at the file.

## Phase discipline

Neighbors are evaluated on `discovery` and `validation` only. Holdout is never requested.
The returned study result must explicitly report exactly those two phases or the run fails.

Validation is no longer pristine after this diagnostic because new neighboring strategies
have now been measured on it. That is intentional: validation is the development/OOS layer
used to reject fragile local peaks. The sealed holdout remains the final untouched test.

## Example contract

```json
{
  "schema_version": 1,
  "name": "btc axis robustness",
  "source_validation_result_sha256": "SHA256_OF_VALIDATION_RESULT_JSON",
  "steps": {
    "include_rsi_period": true,
    "rsi_entry": 0.5,
    "adx_min": 1.0,
    "adx_max": 1.0,
    "tp_price_pct": 0.1,
    "rsi_exit": 0.5
  },
  "gates": {
    "min_neighbor_count": 6,
    "min_validation_pass_fraction": 0.7,
    "min_discovery_stable_neighbor_fraction": 0.7,
    "min_neighbor_discovery_positive_window_fraction": 0.5,
    "max_center_validation_advantage_pct": 5.0
  },
  "max_neighbor_evaluations": 10000
}
```

`min_validation_pass_fraction` is the fraction of valid axis neighbors that must still pass
the already-frozen validation gates.

`min_neighbor_discovery_positive_window_fraction` defines when one neighbor is considered
stable on discovery. For example `0.5` means at least half its discovery windows must be
positive. `min_discovery_stable_neighbor_fraction` then says what fraction of all neighbors
must meet that condition.

`max_center_validation_advantage_pct` is a spike detector. It compares the center's
compounded validation return with the median neighbor compounded validation return. A center
that towers far above its surroundings is treated as suspicious rather than automatically
celebrated.

## Run

```bash
pextract robustness \
  --study study.json \
  --validation-result validation-result.json \
  --robustness robustness.json \
  --data-directory . \
  --output robustness-result.json
```

The result labels every center `ROBUST` or `FRAGILE` and keeps all neighbor evidence. It also
records:

- `parameters_retuned: false`;
- `neighbor_strategies_promotable: false`;
- `discovery_accessed: true`;
- `validation_accessed: true`;
- `holdout_accessed: false`.

## What ROBUST does not mean

`ROBUST` is not permission to deploy and is not a guarantee of profit. It only says that a
validated frozen center is not obviously a one-coordinate knife-edge under the declared
neighborhood contract. The next stages are candidate-family deduplication / signal-overlap
analysis, portfolio slot replay, and only then a sealed holdout decision.
