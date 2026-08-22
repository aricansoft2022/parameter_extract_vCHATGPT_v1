# Sealed holdout evaluation

The sealed holdout stage is the first stage that is allowed to inspect `study.json` holdout
windows for the selected portfolio. It is **evaluation, not development**.

The input portfolio has already passed this chain:

`discovery -> validation -> robustness -> families -> shared-slot portfolio -> one-pass selection`

The holdout stage must not repair anything that it observes.

## Contract

`holdout.json` must be written before the holdout is evaluated and pins both the exact
selection-result file and the selected-set fingerprint inside that file.

```json
{
  "schema_version": 1,
  "name": "final sealed evaluation",
  "source_selection_result_sha256": "COPY_SHA256_OF_SELECTION_RESULT",
  "source_selected_set_fingerprint_sha256": "COPY_SELECTED_SET_FINGERPRINT",
  "gates": {
    "min_total_closed_trades": 20,
    "min_positive_window_fraction": 0.5,
    "min_fixed_baseline_total_return_pct": 0.0,
    "min_median_window_return_pct": 0.0,
    "min_worst_window_return_pct": -2.0,
    "min_worst_within_window_closed_drawdown_pct": -5.0,
    "max_open_at_end_windows": 0,
    "max_pending_at_end_windows": 0
  }
}
```

Run:

```bash
pextract sealed-holdout \
  --study study.json \
  --selection-result selection-result.json \
  --holdout holdout.json \
  --data-directory . \
  --output holdout-result.json
```

## Frozen facts

The evaluator inherits these facts from `selection-result.json` and does not accept overrides:

- exact selected strategy rows;
- strategy fingerprints;
- selected-set fingerprint;
- slot count;
- compact selected priority order;
- original relative family priority;
- execution assumptions;
- dataset and study fingerprints.

The result records:

```text
strategy_parameters_retuned: false
selection_gates_retuned: false
selected_set_changed: false
slot_count_changed: false
priority_reoptimized: false
leverage_applied: false
evaluator_discovery_accessed: false
evaluator_validation_accessed: false
holdout_accessed: true
```

The selected-set fingerprint is independently recomputed inside the holdout result from the
stored selected rows, original priorities, slot count and source portfolio SHA.

## PASS and FAIL

The predeclared gates produce one final `PASS` or `FAIL`. A `FAIL` is **not** permission to
change a threshold, drop a family, change priority, alter a strategy or rerun selection and
then call the same data holdout again.

Once holdout observations have influenced a design decision, those observations are no longer
sealed holdout data for that decision. Further development requires a new research lineage and,
for a credible final test, future data or another genuinely untouched holdout.

The software can prove which files, fingerprints and gates were used for one run. It cannot
cryptographically prove that a human never inspected the same market period through another
program or earlier run. Research discipline remains necessary.

## Window accounting

Only the study's named `holdout` windows are replayed. Discovery and validation are not loaded
through the evaluator's replay path. Each holdout window follows the same portfolio truth
semantics as earlier stages:

- independent window begins flat;
- warm-up candles initialize indicators but cannot open trades;
- raw signals are coordinated under shared finite slots;
- `PENDING_ENTRY` reserves a slot at signal time;
- priority order is frozen;
- fees, slippage and funding use the same execution model;
- open or pending state at the window end is recorded, not fabricated closed.

## Gate signs

Return minima use the natural sign: `0.0` means non-negative, `-2.0` allows a -2% result.
Drawdown is also negative. Therefore

```json
"min_worst_within_window_closed_drawdown_pct": -5.0
```

means a value worse than -5% fails.

Trade count and positive-window fraction are deliberately separate gates. A positive return
with too few closed trades can still fail for insufficient evidence.

## What comes next

A PASS does not choose leverage. Holdout validation finishes the unlevered alpha/portfolio
research lineage. Leverage, liquidation-distance policy and deployment sizing belong to a
separate risk layer. Only after that layer is fixed should a ccbot-compatible `teams.csv` be
exported.
