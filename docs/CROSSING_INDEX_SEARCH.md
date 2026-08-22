# Crossing-index exact discovery search

`crossing_index_exact_v1` is the second acceleration layer. It sits between
`prepared_exact_v1` and a future fully factorized threshold/band aggregation engine.

Run it with:

```bash
pextract-indexed-search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-indexed.json
```

The original `pextract search` remains the truth oracle. `pextract-fast-search` remains the
prepared-exact intermediate oracle.

## Candidate-independent event index

For every prepared discovery window and RSI period, the engine keeps only candles where:

- the current/previous indicator points are valid;
- the candle is not the first candle after a real data gap;
- current RSI is strictly greater than previous RSI;
- current ADR is strictly greater than previous ADR;
- the signal close belongs to the discovery window.

For the live entry contract, these are necessary conditions independent of the candidate
thresholds. A candidate then tests only:

```text
previous.rsi < rsi_entry < current.rsi
adx_min < current.adx < adx_max
```

against the indexed events. Strict inequalities are preserved exactly.

## Execution is still the truth replay

Only raw signal filtering is indexed. Each candidate still calls the existing
`replay_signals()` over its complete warm-up/evaluation candle slice, so this layer does
not duplicate or approximate:

- pending next-open fills;
- one-position occupancy;
- candle-gap pending cancellation;
- TP and completed-candle RSI exits;
- taker fees and slippage;
- funding semantics;
- MAE/MFE and holding duration;
- censored open-at-end positions.

## Runtime parity chain

Before bulk evaluation:

1. prepared exact checks a deterministic sample against truth replay;
2. crossing-index checks a stratified sample against prepared exact.

The second sample takes at least one candidate from every `(RSI period, exit mode)` pair
present in the coarse grid, up to the hard parity-sample bound. A mismatch aborts before
bulk search.

## Search compatibility

Candidate generation, coarse/refined stages, safety cap, gates, aggregate metrics, Pareto
objectives and deterministic frontier ordering all come from the existing search module.
The output remains:

```text
kind: parameter_extract.discovery_search
phase_used: discovery
validation_accessed: false
holdout_accessed: false
```

so a parity-passing result remains compatible with `pextract freeze-candidates`.

## Telemetry

The result reports:

- event counts per RSI period;
- the number of candle checks the prepared signal filter would perform;
- the number of crossing-event checks used by this layer;
- the percentage reduction in those raw signal-filter checks.

This telemetry is **not** total runtime speedup. `replay_signals()` still walks every
candidate's full candle timeline, which is intentionally retained for correctness.

## Next acceleration boundary

The next major gain should not come from weakening replay fidelity. It should reduce the
number of candidate replays that need to be considered at all, for example by inverting
RSI-entry grid membership and aggregating ADX-band membership over the event index.

Any such implementation must reproduce `crossing_index_exact_v1` candidate results and
Pareto frontier exactly on parity fixtures and a runtime sample before its candidate cap is
raised substantially.
