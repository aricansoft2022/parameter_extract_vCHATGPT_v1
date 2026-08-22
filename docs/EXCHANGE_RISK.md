# Binance USD-M exchange-risk validation

This is the final risk gate before ccbot team export can be enabled. It sits after a
`RISK_BUDGET_PASS` and validates the provisional leverage against an identified Binance USD-M
**production** leverage-bracket snapshot plus real Binance-reported liquidation-price parity
fixtures.

It still does not optimize alpha, membership, priority or leverage.

## Why a separate exchange gate exists

The MAE risk budget answers a policy question: how much adverse price movement should the
selected strategy portfolio be budgeted to survive?

That is not the same as the exchange question:

> At the proposed isolated leverage and intended capital range, where would Binance's
> maintenance-margin mechanics put liquidation?

Binance USD-M notional/leverage brackets currently expose, per bracket:

- `initialLeverage`;
- `notionalFloor`;
- `notionalCap`;
- `maintMarginRatio`;
- `cum` (the API's quick-calculation value).

The account endpoint is `GET /fapi/v1/leverageBracket`. The current position-risk endpoint is
`GET /fapi/v3/positionRisk`, whose isolated-position data includes fields such as entry price,
position amount, `isolatedWallet` and Binance's reported `liquidationPrice`.

The exact liquidation equation is not treated as an undocumented Binance contract. V1 derives
a piecewise isolated-long equation from equity = maintenance margin and **requires parity
against real Binance-reported liquidation prices** before it can pass.

## Production snapshot

Create a JSON snapshot from the same Binance USD-M production account that will run the bot.
Do not use a testnet bracket snapshot to approve a production deployment.

Example shape (numbers below are illustrative only):

```json
{
  "schema_version": 1,
  "captured_at_utc": "2026-08-22T14:45:00Z",
  "source": "Binance production GET /fapi/v1/leverageBracket + /fapi/v3/positionRisk",
  "symbol": "BTCUSDT",
  "margin_asset": "USDT",
  "margin_type": "ISOLATED",
  "position_mode": "ONE_WAY",
  "auto_add_margin": false,
  "notional_coef": 1.0,
  "brackets": [
    {
      "bracket": 1,
      "initialLeverage": 20,
      "notionalFloor": 0,
      "notionalCap": 1000,
      "maintMarginRatio": 0.005,
      "cum": 0
    },
    {
      "bracket": 2,
      "initialLeverage": 10,
      "notionalFloor": 1000,
      "notionalCap": 5000,
      "maintMarginRatio": 0.01,
      "cum": 5
    }
  ],
  "liquidation_parity_cases": [
    {
      "name": "isolated-long-case-1",
      "entry_price": 100.0,
      "position_amt": 10.0,
      "isolated_wallet": 100.0,
      "reported_liquidation_price": 90.45
    }
  ]
}
```

The example bracket numbers above are **not BTC production limits** and must never be copied
into a real study.

`notional_coef` is retained as account-specific provenance. The engine treats the bracket
floor/cap values stored in the snapshot as the effective values returned for that account; it
does not multiply them by `notional_coef` a second time.

V1 requires:

- USDT margin;
- isolated margin;
- one-way mode;
- auto-add-margin disabled;
- long positions only.

That matches the current live-bot operating assumptions. Hedge/cross/multi-asset liquidation is
a different model and is intentionally rejected.

## Real parity fixtures

A unit test can prove that our algebra is internally consistent. It cannot prove that Binance
uses the same operational calculation.

For that reason `liquidation_parity_cases` must come from real isolated long positions reported
by Binance `positionRisk`. At minimum capture:

- `entryPrice` -> `entry_price`;
- positive `positionAmt` -> `position_amt`;
- `isolatedWallet` -> `isolated_wallet`;
- `liquidationPrice` -> `reported_liquidation_price`.

Prefer multiple cases and, where practical, different notionals/brackets. The exchange-risk
contract declares both `min_parity_cases` and the maximum tolerated model error in basis points.
If parity is missing or too inaccurate, deployment is `BLOCK` regardless of historical PnL.

## Piecewise maintenance margin

For a bracket, V1 evaluates:

```text
maintenance_margin(mark_notional)
  = mark_notional * maintMarginRatio - cum
```

The snapshot loader checks that adjacent bracket floors/caps are contiguous and that `cum`
keeps maintenance margin continuous at each boundary. A malformed ladder is rejected.

For an isolated long with quantity `q`, entry price `E`, isolated wallet `W`, mark price `P`,
and a candidate maintenance bracket `(r, cum)`, V1 solves the maintenance condition:

```text
W + q * (P - E) = q * P * r - cum
```

which gives the candidate:

```text
P = (q*E - W - cum) / (q * (1-r))
```

The candidate is accepted only when `q*P` actually lies inside that bracket. The solver requires
exactly one bracket-consistent positive solution.

This derivation is why the real Binance parity gate is mandatory: the code does not claim the
algebra itself is an officially published liquidation formula.

## Exchange-risk contract

Example `exchange-risk.json`:

```json
{
  "schema_version": 1,
  "name": "BTC production isolated liquidation gate",
  "source_risk_result_sha256": "COPY_SHA256_OF_RISK_RESULT",
  "source_exchange_snapshot_sha256": "COPY_SHA256_OF_EXCHANGE_SNAPSHOT",
  "baseline_capital_min_usdt": 20.0,
  "baseline_capital_max_usdt": 500.0,
  "isolated_wallet_haircut_pct": 1.0,
  "min_liquidation_headroom_over_required_budget_pct": 2.0,
  "min_parity_cases": 2,
  "max_parity_error_bps": 2.0
}
```

Run:

```bash
pextract exchange-risk \
  --risk-result risk-result.json \
  --exchange-snapshot exchange-snapshot.json \
  --exchange-risk exchange-risk.json \
  --output exchange-risk-result.json
```

## Capital envelope

Bracket selection depends on position notional, so approving one wallet size is not enough.
The contract declares a supported baseline-capital interval.

For each tested baseline:

```text
slot_margin
  = baseline_capital
  * allocation_pct
  * (1 - reserve_pct)

planned_notional
  = slot_margin * provisional_leverage
```

V1 tests both capital-range endpoints and points immediately around every entry-notional bracket
boundary that falls inside the range. It then takes the scenario with the smallest liquidation
distance as the deployment worst case.

A live baseline outside the approved interval invalidates this exchange-risk result and requires
a new risk evaluation; the exporter should carry that allowed interval into deployment metadata.

## Wallet haircut

`isolated_wallet_haircut_pct` deliberately reduces the margin used in the liquidation model. It
is a predeclared conservative buffer for effects not represented by the simple initial-margin
identity. Increasing the haircut moves modeled liquidation closer to entry.

It is a risk-policy assumption, not a fitted parameter.

## Headroom gate

The preceding MAE risk stage already produced:

```text
required_adverse_budget_pct
  = stressed_adverse_move_pct + required_headroom_pct
```

Exchange risk computes the worst modeled liquidation distance across the approved capital range
and requires:

```text
liquidation_distance_pct
  - required_adverse_budget_pct
  >= min_liquidation_headroom_over_required_budget_pct
```

Thus the exchange gate adds an extra liquidation headroom layer rather than replacing the MAE
stress budget.

## Initial-leverage bracket gate

At every tested capital point, the proposed leverage must also be no greater than that entry
notional bracket's `initialLeverage`. If any supported capital point violates the bracket cap,
the entire deployment range is blocked.

## PASS semantics

Only an `EXCHANGE_RISK_PASS` may set:

```text
exchange_liquidation_validated: true
teams_export_ready: true
```

A pass requires all of:

- upstream `RISK_BUDGET_PASS`;
- exact risk and snapshot SHA provenance;
- valid bracket ladder;
- enough real liquidation parity fixtures;
- parity error within the predeclared tolerance;
- initial leverage permitted at every tested deployment notional;
- enough liquidation distance beyond the MAE required budget.

The stage records:

```text
alpha_parameters_retuned: false
selected_set_changed: false
priority_reoptimized: false
leverage_optimized: false
```

## Remaining operational caveats

This V1 intentionally does not pretend to cover every Binance account configuration. In
particular it rejects rather than models cross margin, hedge mode, auto-add-margin and
non-USDT margin. Funding/fee effects on the trading strategy are already modeled upstream;
the exchange gate's wallet haircut is the conservative deployment buffer around the static
liquidation calculation.

A production rollout should still run a small testnet/mainnet operational smoke test before
meaningful capital is used. Exchange behavior and API fields can change, so a bracket snapshot
should be refreshed and revalidated when deploying a new research package.
