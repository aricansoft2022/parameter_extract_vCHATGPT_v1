# Shared-slot portfolio replay

The portfolio stage asks a different question from individual backtests:

> What actually happens when the frozen family representatives compete for the same finite
> live-bot slots?

It does not search new strategy parameters, does not optimize priority permutations, does
not apply leverage, and does not inspect holdout.

## Inputs are already frozen

The source is a `families-result.json`. Before replay, the engine rechecks:

- the exact source-file SHA-256 pinned by `portfolio.json`;
- family contract and fingerprint;
- representative and member strategy fingerprints;
- family/member counts and pairwise evidence counts;
- complete-link within-family pair evidence;
- the deterministic `robustness_stability_v1` representative policy;
- study, dataset, symbol and execution assumptions.

Every family representative entering portfolio replay is therefore an already-existing
ROBUST frozen center.

## Portfolio contract

```json
{
  "schema_version": 1,
  "name": "btc four-slot portfolio",
  "source_families_result_sha256": "SHA256_OF_FAMILIES_RESULT_JSON",
  "slot_count": 4,
  "priority_family_ids": ["F0001", "F0002", "F0003", "F0004"]
}
```

`priority_family_ids` must contain every representative family exactly once. The order is
literal priority. V1 deliberately does **not** search permutations because that would create
a new and particularly easy-to-overfit optimization dimension.

## Slot semantics

The replay starts each study window flat, as the study contract requires.

For each closed candle:

1. a previously accepted `PENDING_ENTRY` fills at the next open;
2. open positions are updated and may exit;
3. same-close new signals are considered in declared family priority order;
4. a signal with no free slot is lost and counted `blocked_no_slot`;
5. if a slot exists, `next_open` execution reserves that slot immediately as
   `PENDING_ENTRY`.

That last point matters. The live bot's pending entry is already an active slot reservation.
The research engine therefore does not let a lower-priority signal queue behind it and enter
later just because the slot eventually becomes free. A blocked team needs a **new signal**.

A team with an open or pending position cannot reserve another slot.

## Execution truth reuse

Once a portfolio position exists, path accounting reuses the same truth-engine primitives as
single-team replay for:

- entry fill model;
- TP/RSI exit behavior;
- adverse slippage;
- fees;
- funding;
- MAE/MFE path updates;
- gap handling for pending entries.

The portfolio layer adds coordination; it does not invent a second trade-accounting model.

## Returns without leverage

Search deliberately kept leverage out of alpha discovery, so portfolio replay remains
unleveraged too.

Every slot is treated as an equal fixed baseline slice. If there are `S` slots, each closed
trade's unleveraged net return contributes `trade_return / S` to portfolio baseline return.
The baseline is fixed rather than compounded, matching the live bot's persistent capital
baseline idea more closely than silently reinvesting every historical profit.

This is **not** a liquidation model and is not a recommendation for live leverage. Leverage
belongs to a later risk layer after portfolio behavior is frozen.

## Slot utilization assumption

OHLC data does not reveal the exact second of an intraminute TP. For utilization only, a
position that is present at candle open is counted as occupying its slot for that full minute.
This is slightly conservative when a TP happens early in the candle and is explicitly recorded
as `occupied_at_candle_open_counts_full_minute` in the result.

## Run

```bash
pextract portfolio \
  --study study.json \
  --families-result families-result.json \
  --portfolio portfolio.json \
  --data-directory . \
  --output portfolio-result.json
```

The result includes per-window and aggregate:

- raw signals;
- accepted entries;
- signals blocked by slot contention;
- signals skipped because the team was already active;
- gap-cancelled pending entries;
- closed trades and fixed-baseline return;
- closed-equity drawdown on the fixed baseline;
- slot utilization;
- censored open/pending positions at window end;
- per-family attribution.

It separately reports discovery and validation aggregates and records:

- `strategy_parameters_retuned: false`;
- `priority_optimized: false`;
- `leverage_applied: false`;
- `holdout_accessed: false`.

## What comes next

Portfolio replay is evidence, not yet a final selection rule. A later portfolio-selection
contract may use predeclared marginal-contribution / contention rules to freeze a smaller
representative set, but it must not tune strategy parameters or priority after seeing sealed
holdout. Only after the portfolio policy itself is frozen should the final holdout be revealed.
