# Post-holdout risk budget

The risk-budget stage is downstream of a **PASS sealed holdout**. It does not search alpha
parameters and it does not choose the leverage that would have produced the highest historical
profit.

Its narrower question is:

> Given the already-selected frozen portfolio and the adverse excursions actually observed in
> development plus sealed holdout, what leverage ceiling is consistent with a predeclared
> conservative adverse-move budget?

This is intentionally only the first risk layer.

## Important limitation

`RISK_BUDGET_PASS` is **not Binance liquidation validation**.

V1 does not model maintenance-margin tiers, leverage brackets, mark-price liquidation details,
fees at liquidation, or other exchange-specific liquidation mechanics. The result therefore
always records:

```text
leverage_optimized: false
exchange_liquidation_validated: false
teams_export_ready: false
```

The MAE-derived leverage ceiling is a conservative budgeting heuristic, not a liquidation-price
formula.

## Contract

Example `risk.json`:

```json
{
  "schema_version": 1,
  "name": "BTC deployment risk budget",
  "source_holdout_result_sha256": "COPY_SHA256_OF_HOLDOUT_RESULT",
  "max_leverage_cap": 10,
  "mae_stress_multiplier": 1.5,
  "extra_adverse_move_pct": 0.5,
  "required_headroom_pct": 2.0,
  "allocation_pct": 25.0,
  "reserve_pct": 4.0,
  "min_total_closed_trades": 50,
  "min_closed_trades_per_family": 3,
  "max_stressed_adverse_move_pct": 15.0
}
```

Run:

```bash
pextract risk-budget \
  --selection-result selection-result.json \
  --holdout-result holdout-result.json \
  --risk risk.json \
  --output risk-result.json
```

## Provenance rules

The stage requires:

- a self-verifying selection result;
- a self-verifying sealed-holdout result;
- holdout status `PASS`;
- the exact holdout-result file SHA pinned by `risk.json`;
- the holdout result to pin the exact supplied selection-result file;
- identical selected-set fingerprints and selected rows;
- identical study, dataset, symbol, execution assumptions and slot count.

The risk artifact itself stores the exact selected rows and source portfolio SHA and recomputes
the selected-set fingerprint. Trade evidence may reference only those frozen family/fingerprint
pairs.

## Evidence scope

Risk sizing deliberately uses the selected portfolio's closed-trade MAE evidence from:

- discovery windows;
- validation windows;
- the sealed holdout windows.

Using holdout observations here is acceptable because this is a **downstream deployment-risk
calculation**, not feedback into alpha selection. The result explicitly records
`holdout_reused_for_alpha_tuning: false`.

The holdout must not be used to drop a weak family, alter an indicator parameter, change
priority, or rerun the selection process.

## Adverse-move budget

For every closed trade:

```text
adverse_move_pct = max(0, -MAE_pct)
```

For a family and for the whole portfolio, V1 takes the worst observed adverse move and applies:

```text
stressed_adverse_move
  = worst_adverse_move * mae_stress_multiplier
  + extra_adverse_move_pct

required_adverse_budget
  = stressed_adverse_move
  + required_headroom_pct

mae_budget_leverage_ceiling
  = floor(100 / required_adverse_budget)
```

The provisional deployment leverage is then:

```text
min(max_leverage_cap, mae_budget_leverage_ceiling)
```

Example only: if worst observed MAE corresponds to a 3% adverse move, multiplier is 1.5,
extra adverse move is 0.5%, and required headroom is 2%, then:

```text
stressed adverse move = 3 * 1.5 + 0.5 = 5.0%
required budget       = 5.0 + 2.0     = 7.0%
MAE budget ceiling    = floor(100/7)  = 14x
```

If the policy cap is 10x, the provisional value is 10x. This still says nothing definitive
about the exchange liquidation price at 10x.

## Why both worst and p95 are reported

The policy ceiling uses the worst observed adverse move in V1. p95 is reported as context, not
as the safety boundary. This avoids silently discarding the tail event that matters most when
there is no strategy stop-loss.

## Sample-size gate

There are two evidence requirements:

- `min_total_closed_trades` for the complete selected portfolio;
- `min_closed_trades_per_family` for every selected family.

If one frozen family lacks enough MAE evidence, the whole risk stage returns `BLOCK`. It does
**not** remove that family after holdout and continue, because doing so would change the sealed
selected set.

## Allocation and slot consistency

`allocation_pct` must imply the same number of slots used during portfolio research:

```text
implied_slots = floor(100 / allocation_pct)
```

If research used four slots, `allocation_pct: 25` is consistent. If research used two slots,
`allocation_pct: 25` is rejected because it would imply four live slots and therefore a
different contention regime.

`reserve_pct` is persisted as a deployment-policy assumption but does not alter alpha or MAE
history in V1.

## Why leverage is not optimized

Testing 1x, 2x, 3x, ... against historical PnL and selecting the most profitable leverage would
turn leverage into another fitted parameter. Instead V1 derives a ceiling from a policy fixed
before the calculation and reports a provisional value bounded by `max_leverage_cap`.

## Next gate before export

A `RISK_BUDGET_PASS` still leaves `teams_export_ready: false`.

Before exporting ccbot teams, the next layer should validate the provisional leverage against
exchange-specific isolated-margin mechanics using an identified Binance leverage-bracket /
maintenance-margin snapshot and a verified liquidation model. Only that layer should be able
to set `exchange_liquidation_validated: true` and unlock deployment export.
