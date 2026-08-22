# Deterministic search work profile

Search acceleration decisions should be based on reproducible work evidence, not guesses and
not a single noisy wall-clock benchmark. `query_work_profile` therefore records logical work
performed by the exact exit-query evaluator during real coarse/refinement evaluation.

Parity-preflight candidates are deliberately excluded. The profile describes research work,
not the cost of the safety oracle chain.

## Counters

Each query/bulk discovery-search result includes:

- `candidate_evaluations`: strategies actually evaluated in coarse/refinement;
- `candidate_window_replays`: candidate × discovery-window evaluations;
- `accepted_positions`: closed trades plus censored open positions;
- `closed_trades`;
- `open_positions`;
- `exit_lookup_requests`: exact first-TP/first-RSI queries;
- `excursion_range_requests`: exact MAE/MFE range queries;
- `funding_event_checks`: funding-event rows inspected by the current event-exact funding path;
- `closed_trade_signal_bisects`: signal-list bisects used after closed trades.

These counts are deterministic for the same study, dataset, search contract and engine
semantics. They contain no timing values.

## Why funding checks are counted explicitly

Exit discovery is now logarithmic, but funding remains deliberately event-exact. For each
accepted position, the current implementation examines the discovery window's indexed
funding tuple and applies timestamp/range/TP-conservatism filters. On normal 8-hour funding
this tuple is often small, but long windows or denser funding data could make this visible.
The profile tells us whether optimizing it is worth the extra complexity.

## Bulk entry counters

`bulk_entry_membership_exact_v1` also reports:

- `bulk_entry_event_visits`;
- `bulk_entry_band_membership_checks`;
- `keywise_event_scan_upper_bound`;
- `event_scan_reduction_fraction`.

Together with `query_work_profile`, these separate entry-membership work from position/exit
work.

## Wall-clock benchmarking remains separate

`tools/benchmark_search_engines.py` still measures actual elapsed time for cached, query and
bulk engines. Before reporting those timings it requires:

1. research-output parity between engines;
2. deterministic query-work-profile equality between query and bulk.

The benchmark report carries both wall-clock timings and deterministic work counters. A
ratio from one computer, dataset or grid is not a universal speed claim.

## How to choose the next optimization

Use representative real-data runs and look for the dominant remaining work:

- high bulk band checks relative to query work → improve ADX-band membership materialization;
- high accepted positions / exit queries → investigate safe reuse or lower-level query data
  structures, while respecting occupancy path dependence;
- high funding-event checks → consider exact indexed/prefix funding queries;
- low logical work but poor wall-clock time → profile implementation overhead/memory rather
  than changing trading semantics.

Do not infer that TP/RSI threshold families can share one position path merely because their
exit thresholds are ordered. Changing an exit changes occupancy, which changes skipped
signals and later accepted entries.

## Candidate-cap policy

This profiling layer does not increase `max_candidates`.

A later scale-calibration change may raise caps only in staged, explicit steps after:

- exact parity on representative datasets;
- acceptable wall-clock and memory behavior;
- work counters that explain the observed scaling;
- preserving the slow engines as deterministic sample oracles.
