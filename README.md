# parameter_extract_vCHATGPT_v1

Research engine for deriving robust parameter teams for
[`cryptobot_vCLUADE_v5`](https://github.com/aricansoft2022/cryptobot_vCLUADE_v5).

This repository deliberately starts with a **truth engine, not an optimizer**.  The first
milestone answers one question correctly and reproducibly:

> Given one symbol, one strategy candidate and a continuous stream of historical data,
> what trades would a conservative live-like execution model have produced?

Only after this path is trusted should millions of candidates be accelerated or ranked.

## Current scope: Phase 1 + Phase 2

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
- a small CLI for replaying one JSON strategy over Binance kline CSV data.

Not implemented yet, by design:

- parameter grid search;
- coarse-to-fine refinement;
- walk-forward / sealed holdout orchestration;
- parameter-neighborhood robustness;
- candidate clustering and signal-overlap deduplication;
- portfolio/slot replay and priority assignment;
- ccbot `teams.csv` export.

Those belong after the truth path has parity tests and real historical fixtures.

## Why signal and execution are separate

A closed one-minute candle can create a signal, but that closing print is not necessarily
where a market order fills.  The expected-live model therefore enters at the next candle
open plus adverse slippage.  The frictionless model exists as a research control, not as
the ranking truth.

TP is also treated as an application-side trigger: once the candle reaches the target, a
sell fill is modeled at target minus adverse slippage.  Because OHLC cannot reveal the
intrabar path, MAE on a TP exit candle uses the full candle low conservatively while MFE
is capped at the TP target so post-exit price movement cannot inflate it.  If a funding
timestamp falls in that same ambiguous TP candle, positive funding is charged but negative
funding is not credited; uncertainty is resolved against optimistic backtest performance.

## Continuous time and data gaps

Calendar months are reporting/study windows, not trading-state boundaries.  A position
opened on August 31 may remain open into September.  No month-end close is invented.

A missing one-minute candle *is* a state boundary for recursive indicators.  RSI/ADX/ADR
restart on the contiguous segment after the gap.  A pending `next_open` entry whose very
next candle is missing is cancelled rather than filled across an unknown interval.

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
2. **Robust region before best point.** Search should prefer stable parameter plateaus.
3. **Out-of-sample survival before historical profit.** Discovery results are hypotheses.
4. **No-loss is metadata, not a crown.** Zero historical losers can be an overfit symptom.
5. **Leverage is a risk layer, not an alpha parameter.** Search returns underlying price-edge behavior.
6. **Open-at-end is censored data.** The engine never fabricates a month-end exit.

## Next milestone

Before exhaustive search, add historical-data manifests/checksums and a parity harness that
replays fixed fixtures through both this research engine and the live bot's indicator/signal
logic.  Once those agree, build study windows and coarse-to-fine candidate generation.
