# Entry-signal cache exact discovery search

`entry_signal_cache_exact_v1` is the third acceleration layer.

Run it with:

```bash
pextract-cached-search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-cached.json
```

Its immediate reference engine is `crossing_index_exact_v1`.

## Cache boundary

Raw entry signals depend on:

```text
rsi_period
rsi_entry
adx_min
adx_max
```

They do **not** depend on:

```text
exit_mode
rsi_exit
tp_price_pct
```

The cache therefore stores one exact signal tuple per unique entry key and reuses it for
all TP and RSI-exit variants sharing that key.

This is especially useful when the exit grid is much larger than the entry grid: dozens
of exit candidates no longer repeat the same crossing-index membership work.

## What is not cached

Candidate execution is not cached or approximated. Each full strategy still calls the
same `replay_signals()` truth execution with its own exit parameters. Therefore TP timing,
RSI exits, next-open fills, one-position occupancy, gaps, fees/slippage, funding, MAE/MFE,
holding duration and censored open positions remain exact candidate-specific behavior.

## Runtime parity chain

Before the bulk loop:

1. prepared exact checks truth replay;
2. crossing-index checks prepared exact;
3. entry-signal cache checks crossing-index.

The third parity sample deliberately tries to include candidates that share the same entry
key while using different exit modes, so the actual factorization boundary is exercised.
All cache state/counters are cleared after parity before bulk evaluation begins.

## Telemetry

The discovery result reports:

```text
entry_signal_cache_requests
entry_signal_cache_misses
entry_signal_cache_hits
entry_signal_cache_hit_fraction
unique_entry_signal_keys
```

For a correct bulk run:

- requests equal evaluated candidates;
- misses equal unique entry keys;
- repeated exit variants should produce hits.

This telemetry describes raw-signal reuse only. It is not total runtime speedup because
all candidate replays are still executed.

## Search compatibility and isolation

Candidate generation, refinement, gates, aggregates and Pareto semantics are inherited
unchanged. Output remains `parameter_extract.discovery_search`, so a parity-passing result
can be frozen by the existing promotion pipeline.

Only discovery data is used. Validation and holdout remain inaccessible.

## Next factorization boundary

The remaining large cost is one full replay per candidate. Reaching multi-million grids
requires reducing repeated exit evaluation, not weakening execution fidelity. A safe next
step is to precompute candidate-independent exit-query structures (for example first TP
high crossing or first completed-candle RSI threshold crossing after each possible entry)
and prove their trade-by-trade parity against this cache-backed engine before using them
for bulk search.
