# Exact exit-query discovery search

`exit_query_exact_v1` is the next correctness-preserving acceleration layer after
`entry_signal_cache_exact_v1`.

It removes the per-candidate full candle loop used only to discover exits and excursions.
It does **not** change the strategy contract, execution assumptions, candidate grid,
refinement, hard gates, Pareto objectives, discovery-only isolation or frozen-candidate
promotion flow.

## What is precomputed

For each prepared discovery window the engine builds:

- a high-price max tree;
- a low-price min tree;
- an RSI max tree for every prepared RSI period;
- funding events mapped to the real candle that owns the event timestamp.

The existing entry-signal cache remains the source of raw signals.

## Exact TP queries

For a filled long position, the TP exit candle is the first candle at or after the legal
exit start whose high satisfies:

```text
high >= entry_price * (1 + tp_price_pct / 100)
```

The same range structure supplies the lowest low and highest high needed for MAE/MFE.
TP MFE remains capped at the target exactly as in truth replay.

## Exact RSI-exit queries

The RSI exit candle is the first legal candle satisfying the existing strict rule:

```text
RSI > rsi_exit
```

For `signal_close`, the signal candle is not eligible for an exit because truth replay
opens the position only after that candle's exit check. For `next_open`, the fill candle
is eligible for an exit at its close.

## Gaps and occupancy

The query replay preserves an important asymmetry from truth replay:

- a pending next-open order is cancelled when the next candle is separated by a one-minute
  data gap;
- an already-open position survives data gaps.

Signals strictly before an exit candle are counted as skipped while open. A signal on the
exit candle itself is processed after the exit and can therefore become the next pending
entry. This behavior is explicitly covered by direct `ReplayResult` parity tests.

## Funding

Funding is intentionally kept event-exact rather than aggressively prefix-aggregated.
Events are mapped to actual candles; events that occur in missing-data gaps are ignored,
matching truth replay.

On a TP exit candle, ambiguous intrabar ordering remains conservative:

- positive funding rates (a long cost) are charged;
- negative funding rates (a long benefit) are withheld.

RSI exits receive all funding that occurred while the position was definitely open.

## Runtime parity chain

Every `pextract-query-search` run performs the existing chain before bulk evaluation:

1. prepared exact vs truth replay;
2. crossing-index vs prepared exact;
3. entry-signal cache vs crossing-index;
4. exit-query vs entry-signal-cache exact.

Any mismatch raises and aborts the search.

## Usage

```bash
pextract-query-search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-query.json
```

The output remains `kind: parameter_extract.discovery_search`, so a parity-approved result
can continue into `freeze-candidates` without a new downstream format.

## Benchmarking

Use `tools/benchmark_search_engines.py` with the exact same study/search/data inputs. The
script runs cached search and query search, verifies the research-significant output fields
are identical, and only then reports elapsed times and the observed speed ratio.

Do not treat a synthetic or tiny-grid benchmark as evidence for production-scale speed.
Do not raise `max_candidates` merely because one benchmark is fast. Candidate-cap increases
should be staged after parity on representative data and should keep the old engines as
oracles for deterministic samples.

## Deliberate limits

This engine still evaluates every strategy candidate and every accepted trade. It reduces
exit discovery from a candle-by-candle scan to logarithmic first-crossing/range queries,
but it is not yet a fully vectorized multi-million-candidate engine.

The next optimization should target repeated exit-query work across candidates that share
an entry signal set and closely related exit thresholds. That layer must prove trade-level
parity against `exit_query_exact_v1` before becoming a search default.
