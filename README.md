# parameter_extract_vCHATGPT_v1

Research engine for deriving robust parameter teams for
[`cryptobot_vCLUADE_v5`](https://github.com/aricansoft2022/cryptobot_vCLUADE_v5).

The project is intentionally built as a chain of research gates rather than a profit-sorting optimizer. A fast search is useful only after reference replay, data identity and phase boundaries are trustworthy.

## Implemented pipeline

1. **Truth replay** — ccbot-compatible Wilder RSI, ADX(14), ADR(14), strict entry rules, one-position replay, TP/RSI exits, fees, slippage, funding, MAE/MFE and censored open trades.
2. **Data identity** — SHA-256 file identities, optional Binance `.CHECKSUM` verification, candle continuity audit and stable dataset fingerprints.
3. **Live parity** — frozen indicator/signal fixture pinned to an exact live-bot commit.
4. **Study contract** — explicit discovery, validation and sealed holdout windows with literal execution assumptions and 300-candle warm-up.
5. **Discovery search** — correctness-first coarse-to-fine search on discovery only, with hard candidate caps and a risk/return Pareto frontier rather than a top-profit leaderboard.
6. **Freeze + validation** — discovery candidates are fingerprinted and validated without parameter retuning; every candidate receives PASS/REJECT plus machine-readable reasons.
7. **Neighborhood robustness** — PASS centers are diagnosed with non-promotable one-axis neighbors across discovery + validation; holdout remains untouched.
8. **Strategy families** — ROBUST frozen centers are grouped by tolerant raw-signal overlap, accepted-entry overlap, position-exposure overlap and normalized parameter distance using conservative complete-link clustering.
9. **Shared-slot portfolio replay** — family representatives are replayed concurrently from raw signals with finite slots, explicit priority and `PENDING_ENTRY` slot reservation.
10. **One-pass portfolio selection** — each family is removed once from the full portfolio, predeclared marginal gates yield KEEP/DROP, survivors retain original relative priority, and no iterative subset search is allowed.
11. **Sealed holdout evaluation** — the exact selected-set fingerprint, slot count and priority order are replayed on holdout windows only against predeclared PASS/FAIL gates; no repair or retuning path exists inside the evaluator.
12. **Post-holdout MAE risk budget** — a PASS selected portfolio is stress-budgeted from observed adverse excursions without alpha retuning or leverage-profit optimization. Allocation must preserve the researched slot count.
13. **Binance exchange-risk gate** — provisional leverage is checked against a pinned production USD-M leverage-bracket snapshot, a declared capital envelope and real Binance-reported isolated-long liquidation-price parity fixtures.
14. **Audited deployment export** — the exact selection -> risk -> exchange-risk artifact lineage is serialized into the audited live bot's `teams.csv` contract plus a self-verifying deployment manifest. No live bot files, database rows or settings are changed by the exporter.
15. **Exact accelerated discovery** — prepared indicators, crossing-event indexing, entry-signal membership caching, exact exit/range queries and bulk entry-membership inversion remove repeated work while runtime parity remains chained back to truth replay.
16. **Deterministic work profiling** — query/entry work counters are machine-independent and wall-clock benchmarking remains separate, so the next optimization is chosen from measured work rather than intuition.
17. **Fail-closed scale calibration** — increasing candidate budgets are exercised on the target dataset/machine under explicit time/heap limits; `safe_max_candidates` is only the last passing stage and is never auto-applied.
18. **Calibrated research bundle** — manifest, study, exact discovery search and scale-calibration contracts are pinned together; discovery is blocked unless the exact intended grid has passed calibration on the same machine/runtime. The final `bundle.json` can be deterministically sealed from those authored contracts rather than hand-assembling bundle-level fingerprints.
19. **Accepted paramderive source preflight + migration** — the previously verified Binance BTC1/funding monthly store can be validated read-only, compared with the archived legacy fingerprint, and migrated into bundle-ready CSV data while keeping candle warm-up and funding-required boundaries explicit and avoiding nondeterministic acceptance timestamps in the new dataset identity.

No universal large-grid cap is claimed. A 50k/100k/1M budget is considered safe only after the exact bundled discovery grid passes its explicit calibration contract on the machine that will run it.

## Core research rules

- **Live fidelity before speed.** Every accelerated path must reproduce the truth replay through runtime parity gates.
- **Named data before results.** Every study is pinned to byte-level input identities.
- **Discovery searches; validation rejects.** Validation never retunes a frozen center.
- **Neighbors diagnose; they do not replace.** Robustness cannot promote a nearby variant.
- **Families deduplicate; they do not invent.** A family representative is an existing ROBUST frozen center, never a synthesized midpoint.
- **Portfolio priority is declared, not optimized.** Shared-slot replay uses an explicit family order.
- **Selection is one-pass, not a subset optimizer.** DROP decisions do not trigger repeated leave-one-out retesting.
- **Holdout evaluates; it never repairs.** A FAIL cannot be converted into a PASS by loosening gates, changing strategies, dropping families or reordering priority on the same holdout data.
- **Leverage is budgeted, not optimized.** Post-holdout risk policy derives an MAE-based ceiling; it does not search leverage for historical profit.
- **Exchange liquidation must be parity-validated.** A derived formula and synthetic tests cannot unlock export without real Binance-reported isolated liquidation fixtures.
- **Risk cannot silently change the sealed set.** Insufficient evidence for one selected family blocks deployment rather than removing the family after holdout.
- **Export serializes; it never redecides.** IDs and enabled state are explicit handoff settings, while strategy parameters, membership, priority order and leverage remain frozen.
- **The live CSV target is audited by commit.** V1 refuses a different ccbot commit rather than assuming its import contract stayed compatible.
- **Scale is calibrated, not assumed.** Candidate-count budgets are only accepted after explicit resource/parity stages on real data.
- **The exact discovery grid must be calibrated.** A different grid with the same candidate count is not treated as equivalent work.
- **Calibration is machine-specific.** A safe-cap artifact from a different Python/platform/CPU environment cannot authorize discovery.
- **Bundles are sealed, not hand-patched.** Bundle-level fingerprints are computed from authored contracts and the final bundle is published only after static lineage/data verification.
- **Accepted source data is migrated, not reinterpreted.** Raw BTC1 OHLC and raw funding rates are carried forward without applying legacy funding multipliers, leverage or liquidation assumptions; unavailable volume/mark-price fields are explicit and documented.
- **Warm-up candles and funding coverage are separate.** A candle-only warm-up month before `funding_required_from` is valid and must not invent a funding requirement that the archived research contract never had.
- **Operational timestamps are not market identity.** Legacy `accepted_at_utc`/`sync_status` can alter the old raw-manifest fingerprint but do not change normalized source provenance when the verified market bytes and substantive verification evidence are unchanged.
- **Robust region before best point.** A local plateau matters more than a single spike.
- **No-loss is metadata, not a crown.** Zero historical losers can be an overfit symptom.
- **Open-at-end is censored data.** The engine never fabricates an exit at a calendar or study-window boundary.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Single-candidate truth replay

```bash
pextract replay \
  --candles BTCUSDT-1m-history.csv \
  --team team.json \
  --funding BTCUSDT-funding.csv \
  --model expected \
  --trades
```

Example `team.json`:

```json
{
  "symbol": "BTCUSDT",
  "rsi_period": 14,
  "rsi_entry": 30.0,
  "adx_min": 18.0,
  "adx_max": 42.0,
  "exit_mode": "tp",
  "tp_price_pct": 0.65
}
```

## Identify and verify historical data

For direct Binance CSV inputs:

```bash
pextract manifest \
  --candles BTCUSDT-1m-history.csv \
  --candles-checksum BTCUSDT-1m-history.CHECKSUM \
  --funding BTCUSDT-funding.csv \
  --source "Binance futures UM archive" \
  --output data-manifest.json

pextract verify-manifest \
  --manifest data-manifest.json \
  --directory .
```

Real minute gaps are recorded, not forward-filled. Duplicate or backward timestamps fail the integrity gate. The manifest fingerprint itself is recomputed during verification, so stale metadata with an old fingerprint is detected.

### Reuse the accepted paramderive Binance store

If the existing `backtest_vCHATGPT_v5.0` ACCEPTED BTC1/funding monthly store is still available, prefer migrating that verified lineage instead of building a second downloader policy. See [`docs/LEGACY_DATA_MIGRATION.md`](docs/LEGACY_DATA_MIGRATION.md).

For the archived 2026-08-07 source coverage, first run the read-only preflight:

```bash
pextract-migrate-paramderive \
  --btc1-root ../past_BNN_data/data/BTCUSDT/1m \
  --funding-root ../backtest_vCHATGPT_v5.0/market-data/funding/BTCUSDT \
  --start 2019-12 \
  --funding-start 2020-01 \
  --end 2026-07 \
  --legacy-fingerprint-reference 19e566d197f1266094faed171c6ee4936b822b3d5f061e8b405604b8aff5021c \
  --preflight-only
```

Then migrate the accepted source into the new normalized lineage:

```bash
pextract-migrate-paramderive \
  --btc1-root ../past_BNN_data/data/BTCUSDT/1m \
  --funding-root ../backtest_vCHATGPT_v5.0/market-data/funding/BTCUSDT \
  --start 2019-12 \
  --funding-start 2020-01 \
  --end 2026-07 \
  --legacy-fingerprint-reference 19e566d197f1266094faed171c6ee4936b822b3d5f061e8b405604b8aff5021c \
  --output-directory btc-run-2026/data
```

The migration re-verifies each required source month's ACCEPTED manifest and data SHA, decodes BTC1/funding losslessly where those formats retain the field, and atomically publishes `candles.csv`, `funding.csv`, normalized/pinned `source-provenance.json`, operational `legacy-preflight.json` and `data-manifest.json`. The raw legacy fingerprint sidecar is intentionally excluded from the new dataset identity because the old algorithm hashes nondeterministic acceptance metadata. It never modifies the source store.

## Live-bot parity gate

```bash
pextract parity --fixture tests/fixtures/live_bot_parity_v1.json
```

When the live bot intentionally changes its indicator/signal contract, regenerate the fixture from a checkout of that exact live-bot commit without modifying the live repository:

```bash
python tools/refresh_parity_fixture.py \
  --ccbot-root ../cryptobot_vCLUADE_v5 \
  --fixture tests/fixtures/live_bot_parity_v1.json
```

## Study contract

A `study.json` pins one dataset fingerprint, literal execution assumptions and non-overlapping research windows. Each evaluation window begins flat; pre-window candles are used only to warm indicators. Open trades at the window end remain censored.

```json
{
  "schema_version": 1,
  "name": "BTC walk-forward example",
  "symbol": "BTCUSDT",
  "dataset_manifest": "data-manifest.json",
  "dataset_fingerprint_sha256": "COPY_THE_64_HEX_VALUE_FROM_THE_MANIFEST_HERE",
  "execution": {
    "name": "expected_live",
    "entry_timing": "next_open",
    "taker_fee_bps": 4.0,
    "buy_slippage_bps": 2.0,
    "sell_slippage_bps": 2.0
  },
  "windows": {
    "discovery": [
      {"name": "discovery-1", "start_ms": 1735689600000, "end_ms": 1743465600000}
    ],
    "validation": [
      {"name": "validation-1", "start_ms": 1743465600000, "end_ms": 1751328000000}
    ],
    "holdout": [
      {"name": "holdout-1", "start_ms": 1751328000000, "end_ms": 1759276800000}
    ]
  },
  "warmup_candles": 300,
  "min_trades": 30
}
```

A single strategy can be inspected with:

```bash
pextract study \
  --study study.json \
  --team team.json \
  --data-directory . \
  --output study-result.json
```

Holdout is omitted unless `--reveal-holdout` is explicitly supplied. The normal research pipeline does not use that generic reveal path; final portfolio evaluation uses the stricter sealed-holdout contract below.

## Discovery search

See [`docs/SEARCH.md`](docs/SEARCH.md).

```bash
pextract search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-search.json
```

`pextract search` remains the correctness-first reference path. Accelerated engines preserve the same discovery/search semantics and are parity-gated against progressively slower reference layers rather than replacing the truth oracle.

## Accelerated, calibrated real-data discovery

See [`docs/RESEARCH_BUNDLE.md`](docs/RESEARCH_BUNDLE.md), [`docs/WORK_PROFILE.md`](docs/WORK_PROFILE.md) and [`docs/SCALE_CALIBRATION.md`](docs/SCALE_CALIBRATION.md).

A real run should seal one pinned bundle before calibration instead of launching a large grid directly:

```bash
pextract-bundle seal \
  --name "BTCUSDT representative run 2026-08" \
  --manifest btc-run-2026/data/data-manifest.json \
  --study btc-run-2026/study.json \
  --search btc-run-2026/discovery-search.json \
  --calibration btc-run-2026/scale-calibration.json \
  --data-directory btc-run-2026/data \
  --output btc-run-2026/bundle.json

pextract-bundle verify \
  --bundle btc-run-2026/bundle.json \
  --data-directory btc-run-2026/data

pextract-bundle calibrate \
  --bundle btc-run-2026/bundle.json \
  --data-directory btc-run-2026/data \
  --output btc-run-2026/scale-calibration-result.json

pextract-bundle discovery \
  --bundle btc-run-2026/bundle.json \
  --calibration-result btc-run-2026/scale-calibration-result.json \
  --data-directory btc-run-2026/data \
  --output btc-run-2026/discovery-search-result.json
```

`seal` computes the bundle-level fingerprints from the actual contracts, refuses overwrite, verifies the static lineage against the real data bytes and only then atomically publishes `bundle.json`. Static verification touches no research phase. Calibration is discovery-only and must include the exact intended discovery-search contract as one stage. Canonical discovery is then blocked unless that stage passed, `safe_max_candidates` covers the search budget, and the calibration machine metadata matches the current runtime.

## Freeze and validate without retuning

See [`docs/PROMOTION.md`](docs/PROMOTION.md).

```bash
pextract freeze-candidates \
  --search-result discovery-search-result.json \
  --output frozen-candidates.json

pextract validate-candidates \
  --study study.json \
  --candidates frozen-candidates.json \
  --validation validation.json \
  --data-directory . \
  --output validation-result.json
```

## Neighborhood robustness

See [`docs/ROBUSTNESS.md`](docs/ROBUSTNESS.md).

```bash
pextract robustness \
  --study study.json \
  --validation-result validation-result.json \
  --robustness robustness.json \
  --data-directory . \
  --output robustness-result.json
```

Neighbors are diagnostic only; even a better-performing neighbor cannot replace the frozen center.

## Strategy-family deduplication

See [`docs/FAMILIES.md`](docs/FAMILIES.md).

```bash
pextract families \
  --study study.json \
  --robustness-result robustness-result.json \
  --families families.json \
  --data-directory . \
  --output families-result.json
```

The family stage uses discovery + validation behavioral evidence only and complete-link grouping. Every representative is an existing ROBUST center.

## Shared-slot portfolio replay

See [`docs/PORTFOLIO.md`](docs/PORTFOLIO.md).

```bash
pextract portfolio \
  --study study.json \
  --families-result families-result.json \
  --portfolio portfolio.json \
  --data-directory . \
  --output portfolio-result.json
```

`portfolio.json` predeclares `slot_count` and a complete `priority_family_ids` order. The engine regenerates raw signals concurrently; `PENDING_ENTRY` reserves a slot immediately, and a no-slot signal is lost rather than queued. Priority is not searched.

## One-pass portfolio selection

See [`docs/SELECTION.md`](docs/SELECTION.md).

```bash
pextract select-portfolio \
  --study study.json \
  --families-result families-result.json \
  --portfolio-result portfolio-result.json \
  --selection selection.json \
  --data-directory . \
  --output selection-result.json
```

Each representative is removed exactly once from the full portfolio. Predeclared gates inspect discovery/validation marginal return, validation sample size, drawdown worsening and contention added to other families. KEEP/DROP decisions are simultaneous; survivors are replayed once without parameter retuning or priority reoptimization. The result has a self-verifying selected-set fingerprint and still records `holdout_accessed: false`.

## Sealed holdout

See [`docs/HOLDOUT.md`](docs/HOLDOUT.md).

Write `holdout.json` before revealing holdout results. It pins both the exact selection-result file SHA-256 and the exact selected-set fingerprint, plus the final PASS/FAIL gates.

```bash
pextract sealed-holdout \
  --study study.json \
  --selection-result selection-result.json \
  --holdout holdout.json \
  --data-directory . \
  --output holdout-result.json
```

The evaluator replays only the study's holdout windows. Strategies, selection gates, slot count and relative priority are frozen. The output records `holdout_accessed: true` and independently recomputes the selected-set fingerprint from its stored selected rows, original priorities, source portfolio SHA and slot count.

A FAIL is a final result for that research lineage, not a tuning prompt. Once holdout observations influence a change, that period is no longer sealed holdout data for the changed design.

## Post-holdout MAE risk budget

See [`docs/RISK.md`](docs/RISK.md).

```bash
pextract risk-budget \
  --selection-result selection-result.json \
  --holdout-result holdout-result.json \
  --risk risk.json \
  --output risk-result.json
```

It combines closed-trade MAE evidence for the frozen selected portfolio across discovery, validation and sealed holdout, applies predeclared stress/headroom assumptions, and derives an MAE-budget leverage ceiling. It does not search leverage for historical profit and it never changes the selected set.

`allocation_pct` must imply the same number of slots used during portfolio research. If one selected family lacks the required MAE sample, the risk stage returns `BLOCK` rather than silently deleting the family after holdout.

## Binance USD-M exchange-risk gate

See [`docs/EXCHANGE_RISK.md`](docs/EXCHANGE_RISK.md).

The final pre-export risk gate consumes the `RISK_BUDGET_PASS`, an exact production account bracket snapshot and a predeclared exchange-risk contract:

```bash
pextract exchange-risk \
  --risk-result risk-result.json \
  --exchange-snapshot exchange-snapshot.json \
  --exchange-risk exchange-risk.json \
  --output exchange-risk-result.json
```

V1 supports only the live bot's intended simple configuration: USDT margin, isolated margin, one-way mode, auto-add-margin disabled and long positions. It models the maintenance-margin ladder with each bracket's `maintMarginRatio` and `cum`, tests the declared baseline-capital envelope around bracket boundaries, and verifies that the proposed leverage is permitted at every tested notional.

Because Binance does not expose the derived liquidation equation as a stable API contract, export cannot be unlocked by algebra or synthetic unit tests alone. The supplied snapshot must contain real Binance `/fapi/v3/positionRisk` isolated-long liquidation fixtures and the model must reproduce them within the predeclared basis-point tolerance.

Only `EXCHANGE_RISK_PASS` sets:

```text
exchange_liquidation_validated: true
teams_export_ready: true
```

This does not change strategies, family membership, priority or leverage.

## Final ccbot deployment export

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

The final exporter requires the exact selection, risk and exchange-risk artifacts. It checks
their SHA-linked lineage, then serializes the frozen selected set into the exact CSV contract
audited from `cryptobot_vCLUADE_v5` commit
`0ab6aa532cb22f399bc94393280c604cb6756d66`.

```bash
pextract-deploy \
  --selection-result selection-result.json \
  --risk-result risk-result.json \
  --exchange-risk-result exchange-risk-result.json \
  --deployment deployment.json \
  --teams-csv teams.csv \
  --manifest deployment-manifest.json
```

`first_team_id` and `enabled` are explicit deployment settings; they are not research
parameters. Strategy parameters, selected membership, compact priority order and leverage
remain frozen. The exporter does not connect to the live bot, and the manifest explicitly
states that existing live team-ID collisions have not been checked.

Before any write to a live ccbot database, validate the generated file with the live bot's
own dry-run importer:

```bash
ccbot import-teams teams.csv
```

Do not hand-edit the CSV after export; its exact bytes are SHA-256-pinned in the deployment
manifest.

## Funding CSV

```text
timestamp_ms,rate,mark_price
1722470400000,0.0001,64832.5
```

`mark_price` may be blank; in that case the enclosing one-minute candle close is used as an explicit approximation.

## Next milestone

The next operation belongs on the target machine that actually holds the ACCEPTED monthly store: run the read-only canonical source preflight first, then migrate the verified candle/funding ranges into a new normalized `data-manifest.json`. After that, author the study/search/calibration contracts around the resulting dataset fingerprint, seal the bundle, calibrate that same machine, and produce the canonical bundled discovery artifact. Only measured real-data work profiles and calibration results should determine whether another acceleration layer or a larger candidate budget is justified.
