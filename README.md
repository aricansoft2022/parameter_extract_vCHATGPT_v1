# parameter_extract_vCHATGPT_v1

Research engine for deriving robust parameter teams for
[`cryptobot_vCLUADE_v5`](https://github.com/aricansoft2022/cryptobot_vCLUADE_v5).

The project is intentionally built as a chain of research gates rather than a profit-sorting
optimizer. A fast search is useful only after the reference replay, data identity and phase
boundaries are trustworthy.

## Implemented pipeline

1. **Truth replay** — ccbot-compatible Wilder RSI, ADX(14), ADR(14), strict entry rules,
   one-position replay, TP/RSI exits, fees, slippage, funding, MAE/MFE and censored open trades.
2. **Data identity** — SHA-256 file identities, optional Binance `.CHECKSUM` verification,
   candle continuity audit and stable dataset fingerprints.
3. **Live parity** — frozen indicator/signal fixture pinned to an exact live-bot commit.
4. **Study contract** — explicit discovery, validation and sealed holdout windows with literal
   execution assumptions and 300-candle warm-up.
5. **Discovery search** — correctness-first coarse-to-fine search on discovery only, with
   hard candidate caps and a risk/return Pareto frontier rather than a top-profit leaderboard.
6. **Freeze + validation** — discovery candidates are fingerprinted and validated without
   parameter retuning; every candidate receives PASS/REJECT plus machine-readable reasons.
7. **Neighborhood robustness** — PASS centers are diagnosed with non-promotable one-axis
   neighbors across discovery + validation; holdout remains untouched.

Still intentionally deferred:

- candidate-family clustering and signal-overlap deduplication;
- portfolio/slot replay and priority assignment;
- sealed holdout promotion workflow;
- ccbot `teams.csv` export;
- factorized/high-throughput search for very large grids.

## Core research rules

- **Live fidelity before speed.** Any future fast path must reproduce the truth replay.
- **Named data before results.** Every study is pinned to byte-level input identities.
- **Discovery searches; validation rejects.** Validation never retunes a frozen center.
- **Neighbors diagnose; they do not replace.** Robustness cannot promote a nearby variant.
- **Holdout stays sealed until the research decision is frozen.**
- **Robust region before best point.** A local plateau matters more than a single spike.
- **No-loss is metadata, not a crown.** Zero historical losers can be an overfit symptom.
- **Leverage is a risk layer, not an alpha parameter.** Search remains leverage-free.
- **Open-at-end is censored data.** The engine never fabricates an exit at a calendar or
  study-window boundary.

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

Real minute gaps are recorded, not forward-filled. Duplicate or backward timestamps fail the
integrity gate. The manifest fingerprint itself is recomputed during verification, so stale
metadata with an old fingerprint is detected.

## Live-bot parity gate

```bash
pextract parity --fixture tests/fixtures/live_bot_parity_v1.json
```

When the live bot intentionally changes its indicator/signal contract, regenerate the fixture
from a checkout of that exact live-bot commit without modifying the live repository:

```bash
python tools/refresh_parity_fixture.py \
  --ccbot-root ../cryptobot_vCLUADE_v5 \
  --fixture tests/fixtures/live_bot_parity_v1.json
```

## Study contract

A `study.json` pins one dataset fingerprint, literal execution assumptions and non-overlapping
research windows. Each evaluation window begins flat; pre-window candles are used only to
warm indicators. Open trades at the window end remain censored.

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

The discovery search contract and Pareto semantics are documented in
[`docs/SEARCH.md`](docs/SEARCH.md).

```bash
pextract search \
  --study study.json \
  --search search.json \
  --data-directory . \
  --output discovery-search.json
```

This correctness-first path deliberately refuses grids above its configured candidate cap.
Large-scale factorization comes later, after the research semantics are stable.

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

The validation result carries the exact frozen strategy fingerprints and records
`parameters_retuned: false` and `holdout_accessed: false`.

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

Only validation `PASS` centers are diagnosed. V1 perturbs one parameter axis at a time.
Neighbors are explicitly `diagnostic_only` and `neighbor_strategies_promotable: false`;
even a better-performing neighbor cannot replace the frozen center.

## Funding CSV

```text
timestamp_ms,rate,mark_price
1722470400000,0.0001,64832.5
```

`mark_price` may be blank; in that case the enclosing one-minute candle close is used as an
explicit approximation.

## Next milestone

Group robust centers into genuine strategy/signal families instead of treating nearby or
highly synchronized candidates as independent teams. The next layer should quantify signal
and trade overlap, choose representative frozen centers without retuning them, and prepare a
small diverse set for portfolio/slot replay. Holdout remains sealed during that work.
