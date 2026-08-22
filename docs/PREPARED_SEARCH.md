# Prepared exact discovery search

`prepared_exact_v1` is the first acceleration layer. It is deliberately not called the
fully factorized engine yet.

The reference command remains:

```bash
pextract search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-slow.json
```

The prepared path is:

```bash
pextract-fast-search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-prepared.json
```

Both use the same study/search contracts, candidate grid, refinement rules, hard gates,
Pareto objectives and frontier ordering.

## What is accelerated

For every discovery window the engine prepares the exact warm-up + evaluation candle
slice once. Within each real contiguous one-minute segment it computes:

- ADX(14) once;
- ADR(14) once;
- Wilder RSI once for each requested RSI period.

Those arrays are reused by every candidate whose thresholds differ only in RSI entry,
ADX band or exit threshold/TP.

Real data gaps still reset every recursive indicator. Nothing is forward-filled.

## What remains the truth path

Prepared search deliberately continues to call the existing `replay_signals()` for each
candidate. Therefore the following are not reimplemented in the accelerator:

- one-position occupancy;
- pending next-open entry behavior;
- pending cancellation on a candle gap;
- TP and completed-candle RSI exits;
- slippage and taker fees;
- funding, including conservative same-minute TP/funding ambiguity;
- MAE/MFE and holding duration;
- censored open-at-end positions.

This makes the first speedup smaller than a fully factorized search, but it removes the
most obvious repeated indicator work while retaining one execution/accounting source of
truth.

## Runtime parity gate

Before bulk candidate evaluation starts, the prepared runner evaluates the first three
coarse candidates (or all candidates when fewer exist) through both:

- the original truth search evaluator;
- the prepared evaluator.

The complete candidate row — window metrics and aggregate — must compare exactly. A
single mismatch aborts the run before the fast loop.

CI additionally covers gap-reset indicator values, raw signal times, TP and RSI branches,
funding, execution friction, candidate metrics and full small-grid frontier parity.

## Research phase isolation

Only `StudySpec.discovery` windows are prepared. Validation and holdout are neither cached
nor evaluated. The result explicitly records:

```text
phase_used: discovery
validation_accessed: false
holdout_accessed: false
search_engine: prepared_exact_v1
reference_engine: truth_replay
runtime_parity_passed: true
```

Because the result keeps `kind: parameter_extract.discovery_search` and the same frontier
schema, it can be passed to `pextract freeze-candidates` after the runtime parity gate
passes.

## Deliberate remaining limit

This version still loops through candidates and replays each candidate. It is not intended
for the final multi-million-combination workload.

The next acceleration step may invert RSI crossing thresholds and aggregate ADX-band
membership instead of enumerating every signal test, but that implementation must prove
exact candidate/frontier parity against `prepared_exact_v1` before its candidate cap is
raised substantially.
