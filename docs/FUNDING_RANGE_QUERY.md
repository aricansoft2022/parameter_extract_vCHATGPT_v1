# Exact funding range queries

Real-data scale calibration exposed a deterministic work-profile bottleneck in the exact exit-query replay: each accepted position iterated the complete funding-event tuple for its discovery window and then discarded events outside the position interval.

The optimized path preserves research semantics and changes only how impossible events are skipped:

- funding events are still sorted and mapped to their enclosing candle exactly once when the exit-query index is built;
- the corresponding non-decreasing candle-index tuple is stored beside the events;
- each accepted position uses `bisect_left` / `bisect_right` to select only funding events whose candle lies inside the position's candidate range;
- selected events are processed in the same chronological order as before;
- `event.timestamp_ms > entry_time_ms` remains mandatory;
- when a TP is reached on a candle that also contains funding, positive funding is still charged and negative funding benefit is still withheld;
- mark-price fallback and floating-point accumulation are unchanged.

This deliberately avoids a prefix-sum rewrite. Replaying the same selected funding events in the same order minimizes numerical-parity risk while removing the full-window Python scan.

## Work profile

`QueryWorkProfile` reports:

- `funding_range_bisects`: actual range-boundary bisect operations performed for positions in windows that contain indexed funding;
- `funding_event_checks`: actual indexed funding events visited after range selection.

Before this change, `funding_event_checks` was a deterministic full-scan upper-bound estimate (`accepted_positions * len(window.funding)`), not actual selected event visits. Results produced before and after this change should therefore not compare that counter as if its meaning were unchanged.

## Evidence boundary

This optimization does not itself establish a wall-clock speedup. Runtime and Python-heap evidence must come from a new fail-closed scale calibration on the target machine. Candidate caps must not be raised merely because the algorithmic scan count decreased.

The existing truth replay and runtime parity checks remain the semantic oracle. Discovery, validation and sealed-holdout isolation are unchanged.
