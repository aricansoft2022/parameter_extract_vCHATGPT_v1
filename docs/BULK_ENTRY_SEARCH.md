# Bulk entry-membership discovery search

`bulk_entry_membership_exact_v1` accelerates raw entry-signal construction while keeping
`exit_query_exact_v1` as the candidate evaluator.

## Why this layer exists

The entry-signal cache key is:

```text
(rsi_period, rsi_entry, adx_min, adx_max)
```

Exit mode and exit threshold are not part of that key. Earlier engines still built each
new entry key by scanning the same crossing-event list independently. This layer reverses
that relationship: each crossing event is visited once per RSI period, and all matching
entry keys are filled from that event.

## Exact strict membership

For a crossing event with previous/current RSI values, qualifying entry thresholds are
selected with the exact live rule:

```text
previous.rsi < rsi_entry < current.rsi
```

Sorted candidate thresholds allow those strict bounds to be found by bisect. ADX remains:

```text
adx_min < current.adx < adx_max
```

No inclusive boundary is introduced.

## Coarse and refinement phases

Before coarse evaluation, every unique coarse entry key is bulk-primed. Candidate exit
variants then reuse those exact signal tuples.

After the coarse Pareto frontier chooses deterministic refinement seeds, refined strategies
are generated in the same order as the reference search. Only entry keys not already in
the cache are bulk-primed in a second pass.

The full-search test requires zero fallback cache misses. A missing bulk key is therefore a
correctness failure rather than a hidden performance regression.

## Runtime parity chain

Every bulk search still runs the prior oracle chain:

1. prepared exact vs truth replay;
2. crossing-index vs prepared exact;
3. entry-signal cache vs crossing-index;
4. exit-query vs entry-signal-cache exact;
5. bulk-primed signal tuples vs ordinary keywise entry-signal cache.

Only then does bulk evaluation begin.

## Usage

```bash
pextract-bulk-search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-bulk.json
```

The result remains `kind: parameter_extract.discovery_search`, so an approved result can
continue into the existing `freeze-candidates` pipeline.

## Work telemetry

The output records:

- `bulk_entry_installed_keys`;
- `bulk_entry_event_visits`;
- `bulk_entry_band_membership_checks`;
- `keywise_event_scan_upper_bound`;
- `event_scan_reduction_fraction`;
- entry-signal cache hits/misses.

The event-scan reduction is a work counter, not wall-clock speed. Use
`tools/benchmark_search_engines.py` for machine/dataset/grid-specific timing. The benchmark
verifies exact frontier/count/fingerprint parity before reporting any timing ratio.

## Safety boundary

This optimization changes only how exact raw signal membership is materialized. It does not
change:

- indicators;
- entry or exit inequalities;
- execution/slippage/fee/funding assumptions;
- gap handling;
- MAE/MFE;
- occupancy;
- gates;
- Pareto objectives;
- validation or holdout access.

Candidate caps are intentionally unchanged in this PR. Cap increases should happen only
after representative real-data benchmarks show both exact parity and useful scaling.
