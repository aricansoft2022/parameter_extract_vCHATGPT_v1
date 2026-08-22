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

Still intentionally deferred:

- sealed holdout evaluation/promotion;
- risk/leverage policy;
- ccbot `teams.csv` export;
- factorized/high-throughput search for very large grids.

## Core research rules

- **Live fidelity before speed.** Any future fast path must reproduce the truth replay.
- **Named data before results.** Every study is pinned to byte-level input identities.
- **Discovery searches; validation rejects.** Validation never retunes a frozen center.
- **Neighbors diagnose; they do not replace.** Robustness cannot promote a nearby variant.
- **Families deduplicate; they do not invent.** A family representative is an existing ROBUST frozen center, never a synthesized midpoint.
- **Portfolio priority is declared, not optimized.** Shared-slot replay uses an explicit family order.
- **Selection is one-pass, not a subset optimizer.** DROP decisions do not trigger repeated leave-one-out retesting.
- **Holdout stays sealed until the selected-set fingerprint is frozen.**
- **Robust region before best point.** A local plateau matters more than a single spike.
- **No-loss is metadata, not a crown.** Zero historical losers can be an overfit symptom.
- **Leverage is a risk layer, not an alpha parameter.** Search and selection remain leverage-free.
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

Holdout is omitted unless `--reveal-holdout` is explicitly supplied.

## Discovery search

See [`docs/SEARCH.md`](docs/SEARCH.md).

```bash
pextract search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-search.json
```

This correctness-first path deliberately refuses grids above its configured candidate cap. Large-scale factorization comes later, after the research semantics are stable.

## Freeze and validate without retuning

See [`docs/PROMOTION.md`](docs/PROMOTION.md).

```bash
pextract freeze-candidates \
  --search-result discovery-search.json \
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

## Funding CSV

```text
timestamp_ms,rate,mark_price
1722470400000,0.0001,64832.5
```

`mark_price` may be blank; in that case the enclosing one-minute candle close is used as an explicit approximation.

## Next milestone

Freeze the selection result and evaluate that exact selected set on the sealed holdout without changing gates, strategies, slot count or relative priority. Only after that boundary should a separate risk/leverage layer and ccbot-compatible `teams.csv` export be considered.
