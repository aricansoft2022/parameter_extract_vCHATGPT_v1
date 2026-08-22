# parameter_extract_vCHATGPT_v1

Research engine for deriving robust parameter teams for
[`cryptobot_vCLUADE_v5`](https://github.com/aricansoft2022/cryptobot_vCLUADE_v5).

This repository deliberately starts with a **truth engine, not an optimizer**. The first
milestones answer one question correctly and reproducibly:

> Given one symbol, one strategy candidate and an identified historical dataset, what
> trades would a conservative live-like execution model have produced, and does the signal
> maths still match the live bot contract?

Only after this path is trusted should millions of candidates be accelerated or ranked.

## Current scope

Implemented now:

- ccbot-compatible Wilder RSI, ADX(14), ADR(14) and strict entry boundaries;
- raw signal generation separated from execution;
- recursive indicators reset across real one-minute data gaps;
- one-position-per-team replay on a continuous timeline;
- explicit frictionless / expected-live / stress execution assumptions;
- next-open entry modelling, adverse slippage, taker fees and long funding;
- TP and completed-candle RSI exits;
- no forced close at dataset or study-window boundaries;
- MAE, MFE, holding time, drawdown, exposure and sample-size metrics;
- SHA-256 identities for candle/funding files and optional Binance `.CHECKSUM` verification;
- candle audits for duplicates, ordering errors and minute gaps;
- self-verifying stable dataset fingerprints;
- a frozen indicator/signal parity fixture pinned to a full live-bot commit SHA;
- a read-only fixture refresh tool that imports an actual checkout of the live bot;
- a `study.json` contract with discovery, validation and holdout windows;
- 300-candle pre-window warm-up, matching the live bot's current REST seed limit;
- holdout results withheld unless `--reveal-holdout` is explicitly requested;
- study results pinned to both a dataset fingerprint and a study fingerprint.

Not implemented yet, by design:

- coarse-to-fine candidate generation;
- promotion rules from discovery to validation;
- parameter-neighborhood robustness;
- candidate clustering and signal-overlap deduplication;
- portfolio/slot replay and priority assignment;
- ccbot `teams.csv` export.

## Why signal and execution are separate

A closed one-minute candle can create a signal, but that closing print is not necessarily
where a market order fills. The expected-live model therefore enters at the next candle
open plus adverse slippage. The frictionless model exists as a research control, not as
the ranking truth.

TP is an application-side trigger: once a candle reaches the target, a sell fill is modeled
at target minus adverse slippage. Because OHLC cannot reveal intrabar ordering, ambiguous
TP/funding cases are resolved against optimistic backtest performance.

## Continuous time, gaps and study windows

Calendar boundaries do not fabricate exits. A normal replay can carry an August 31 trade
into September.

Research windows are different: discovery, validation and holdout are intentionally
independent experiments. Each window begins flat, receives up to 300 pre-window candles
only for indicator warm-up, opens no warm-up trades, and leaves any trade still open at the
window end censored. This prevents validation PnL or position state leaking into discovery.

A missing one-minute candle remains a real state break. RSI/ADX/ADR restart on the
contiguous segment after the gap; nothing is forward-filled. Gaps are recorded in the data
manifest. Duplicate or backward open times fail the integrity gate.

## Setup and single-candidate replay

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest

pextract replay \
  --candles BTCUSDT-1m-2026-07.csv \
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

## Identify the data before studying it

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

The manifest fingerprint is recomputed during verification. Editing manifest metadata or
file identities while keeping an old fingerprint is therefore detected.

## Live-bot parity gate

```bash
pextract parity --fixture tests/fixtures/live_bot_parity_v1.json
```

When the live bot intentionally changes its indicator/signal contract, regenerate the
fixture from a checkout of that exact live-bot commit without modifying that repository:

```bash
python tools/refresh_parity_fixture.py \
  --ccbot-root ../cryptobot_vCLUADE_v5 \
  --fixture tests/fixtures/live_bot_parity_v1.json
```

## Study contract

A study references the fingerprint emitted by its data manifest and stores execution
assumptions as literal numbers, so a future change to defaults cannot silently rewrite an
old experiment.

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

Discovery and validation only:

```bash
pextract study \
  --study study.json \
  --team team.json \
  --data-directory . \
  --output study-result.json
```

Reveal holdout only when the research decision is already frozen:

```bash
pextract study \
  --study study.json \
  --team team.json \
  --data-directory . \
  --reveal-holdout \
  --output final-holdout-result.json
```

The CLI cannot make statistical discipline cryptographically impossible to violate, but it
makes accidental holdout leakage non-default and records whether holdout was revealed.

Funding CSV schema:

```text
timestamp_ms,rate,mark_price
1722470400000,0.0001,64832.5
```

`mark_price` may be blank; in that case the enclosing one-minute candle close is used as
an explicit approximation.

## Research rules

1. **Live fidelity before speed.** Any future fast path must reproduce the truth replay.
2. **Named data before results.** Every study carries byte-level input identities and audits.
3. **Independent windows before one giant backtest.** Validation begins flat and cannot borrow discovery state.
4. **Robust region before best point.** Search should prefer stable parameter plateaus.
5. **Out-of-sample survival before historical profit.** Discovery results are hypotheses.
6. **No-loss is metadata, not a crown.** Zero historical losers can be an overfit symptom.
7. **Leverage is a risk layer, not an alpha parameter.** Search remains leverage-free.
8. **Open-at-end is censored data.** The engine never fabricates a study-window exit.

## Next milestone

Add coarse-to-fine candidate generation that is permitted to inspect **discovery windows
only**. Candidates then move through validation without parameter retuning. Holdout remains
outside the search path. After that comes neighborhood robustness and candidate clustering,
not a naive top-profit leaderboard.
