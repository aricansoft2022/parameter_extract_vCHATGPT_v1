# parameter_extract_vCHATGPT_v1

Research engine for deriving robust parameter teams for
[`cryptobot_vCLUADE_v5`](https://github.com/aricansoft2022/cryptobot_vCLUADE_v5).

This repository deliberately starts with a **truth engine, not an optimizer**. The first
milestones answer one question correctly and reproducibly:

> Given one symbol, one strategy candidate and an identified continuous stream of
> historical data, what trades would a conservative live-like execution model have
> produced, and can we prove the signal maths still matches the live bot contract?

Only after this path is trusted should millions of candidates be accelerated or ranked.

## Current scope: truth, accounting, integrity and parity

Implemented now:

- ccbot-compatible Wilder RSI, ADX(14), ADR(14) and strict entry boundaries;
- raw signal generation separated from execution;
- recursive indicators reset across real one-minute data gaps;
- one-position-per-team replay on a continuous timeline;
- three execution regimes: `frictionless`, `expected_live`, `stress`;
- signal-close vs next-open entry timing;
- adverse buy/sell slippage and two-sided taker fees;
- funding payments for long positions;
- TP and completed-candle RSI exits;
- no forced close at dataset/month boundaries: an open trade remains censored/open;
- MAE, MFE and holding-time tracking;
- compounded return, profit factor, closed-equity drawdown, exposure and sample-size metrics;
- SHA-256 identities for candle/funding files and optional Binance `.CHECKSUM` verification;
- candle audits for duplicates, ordering errors and minute gaps;
- stable dataset fingerprints that do not depend on generation time;
- a frozen indicator/signal parity fixture pinned to a full live-bot commit SHA;
- a read-only fixture refresh tool that imports an actual local checkout of the live bot;
- CLI commands for replay, manifest creation/verification and parity checks.

Not implemented yet, by design:

- study-window orchestration;
- coarse-to-fine parameter search;
- walk-forward / sealed holdout evaluation;
- parameter-neighborhood robustness;
- candidate clustering and signal-overlap deduplication;
- portfolio/slot replay and priority assignment;
- ccbot `teams.csv` export.

## Why signal and execution are separate

A closed one-minute candle can create a signal, but that closing print is not necessarily
where a market order fills. The expected-live model therefore enters at the next candle
open plus adverse slippage. The frictionless model exists as a research control, not as
the ranking truth.

TP is also treated as an application-side trigger: once the candle reaches the target, a
sell fill is modeled at target minus adverse slippage. Because OHLC cannot reveal the
intrabar path, MAE on a TP exit candle uses the full candle low conservatively while MFE
is capped at the TP target so post-exit price movement cannot inflate it. If a funding
timestamp falls in that same ambiguous TP candle, positive funding is charged but negative
funding is not credited; uncertainty is resolved against optimistic backtest performance.

## Continuous time and data gaps

Calendar months are reporting/study windows, not trading-state boundaries. A position
opened on August 31 may remain open into September. No month-end close is invented.

A missing one-minute candle *is* a state boundary for recursive indicators. RSI/ADX/ADR
restart on the contiguous segment after the gap. A pending `next_open` entry whose very
next candle is missing is cancelled rather than filled across an unknown interval.

Gaps are recorded in the data manifest rather than silently filled. Duplicate or backward
open times fail the manifest integrity gate.

## Usage

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

Create a reproducible data identity before a study:

```bash
pextract manifest \
  --candles BTCUSDT-1m-2026-07.csv \
  --candles-checksum BTCUSDT-1m-2026-07.zip.CHECKSUM \
  --funding BTCUSDT-funding.csv \
  --source "Binance futures UM archive" \
  --output data-manifest.json

pextract verify-manifest \
  --manifest data-manifest.json \
  --directory .
```

Run the pinned live-contract parity gate:

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

Funding CSV schema:

```text
timestamp_ms,rate,mark_price
1722470400000,0.0001,64832.5
```

`mark_price` may be blank; in that case the enclosing one-minute candle close is used as
an explicit approximation.

## Research rules we intend to preserve

1. **Live fidelity before speed.** Any future fast path must be checked against this replay.
2. **Named data before results.** A study must carry byte-level input identities and audits.
3. **Robust region before best point.** Search should prefer stable parameter plateaus.
4. **Out-of-sample survival before historical profit.** Discovery results are hypotheses.
5. **No-loss is metadata, not a crown.** Zero historical losers can be an overfit symptom.
6. **Leverage is a risk layer, not an alpha parameter.** Search returns underlying price-edge behavior.
7. **Open-at-end is censored data.** The engine never fabricates a month-end exit.

## Next milestone

Build a `study.json` contract and study runner that explicitly names discovery, validation
and sealed holdout windows, requires a verified dataset fingerprint, and records every
assumption. Only then add coarse-to-fine candidate generation against the truth replay.
