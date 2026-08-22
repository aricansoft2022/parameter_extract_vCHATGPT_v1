# Research run bundle

`pextract-bundle` is the real-data entry point for the accelerated discovery engine.
It binds byte-level dataset identity, the study contract, the intended discovery grid and the
machine-specific scale calibration ladder into one immutable lineage.

The bundle exists to prevent a common research failure: calibrating one grid or dataset and
then silently running a different, larger discovery search.

## Directory model

Keep contract files together under one bundle directory. Historical candle/funding bytes may
live elsewhere and are supplied through `--data-directory`.

```text
btc-run-2026/
  bundle.json
  data-manifest.json
  study.json
  discovery-search.json
  scale-calibration.json
  calibration-10k.json
  calibration-50k.json

/data/binance/BTCUSDT/
  BTCUSDT-1m-history.csv
  BTCUSDT-funding.csv
```

All contract paths stored inside `bundle.json`, `study.json` and `scale-calibration.json` must
resolve inside the bundle directory. `../` escapes and absolute paths are rejected. The data
files themselves are located through the manifest and `--data-directory`, then re-hashed.

## Bundle contract

```json
{
  "schema_version": 1,
  "name": "BTCUSDT representative run 2026-08",
  "manifest_file": "data-manifest.json",
  "dataset_fingerprint_sha256": "<64 hex>",
  "study_file": "study.json",
  "study_fingerprint_sha256": "<64 hex>",
  "discovery_search_file": "discovery-search.json",
  "discovery_search_fingerprint_sha256": "<64 hex>",
  "calibration_file": "scale-calibration.json",
  "calibration_fingerprint_sha256": "<64 hex>"
}
```

The fingerprints are semantic contract fingerprints, not merely filenames. Changing a search
step, window, execution assumption, dataset manifest or calibration limit requires a new
fingerprint and therefore a new bundle lineage.

## Construction order

Build the inputs in this order so every downstream contract can pin the upstream identity:

1. Create the candle/funding manifest with `pextract manifest` and record its
   `dataset_fingerprint_sha256`.
2. Create `study.json` with that dataset fingerprint and the final discovery, validation and
   sealed-holdout windows.
3. Compute/pin the study fingerprint.
4. Create the intended `discovery-search.json` and compute its search fingerprint.
5. Create one or more calibration search contracts with increasing `max_candidates` budgets.
6. Create `scale-calibration.json`, pinning the exact study/dataset plus every stage search
   fingerprint.
7. Create `bundle.json`, pinning all four identities above.

The calibration ladder must reach at least the intended discovery search
`refinement.max_candidates`; otherwise bundle verification fails before any research run.

## Step 1: static verification

```bash
pextract-bundle verify \
  --bundle btc-run-2026/bundle.json \
  --data-directory /data/binance/BTCUSDT
```

This performs no candidate evaluation. It verifies:

- manifest self-fingerprint;
- candle integrity gate and optional external checksums;
- exact candle/funding file SHA-256 and size;
- study fingerprint and exact manifest reference;
- discovery-search fingerprint;
- scale-calibration fingerprint;
- every calibration-stage search fingerprint and candidate budget;
- path containment inside the bundle directory.

The verification result explicitly reports `discovery_accessed: false`,
`validation_accessed: false` and `holdout_accessed: false`.

## Step 2: calibrate this machine

```bash
pextract-bundle calibrate \
  --bundle btc-run-2026/bundle.json \
  --data-directory /data/binance/BTCUSDT \
  --output btc-run-2026/scale-calibration-result.json
```

The existing fail-closed scale ladder runs with `bulk_entry_membership_exact_v1`. Every stage
must satisfy its predeclared minimum exercised-candidate count, wall-clock ceiling and Python
heap ceiling while keeping all runtime parity gates green.

The result records only the last passing stage as `safe_max_candidates`. The bundle command
adds bundle lineage metadata and recomputes the scale-result fingerprint, so the stored result
still passes `pextract-scale verify`.

A calibration FAIL is not permission to loosen the limit after seeing it and call the same
artifact calibrated. Change the calibration contract intentionally and create a new lineage.

## Step 3: calibrated discovery

```bash
pextract-bundle discovery \
  --bundle btc-run-2026/bundle.json \
  --calibration-result btc-run-2026/scale-calibration-result.json \
  --data-directory /data/binance/BTCUSDT \
  --output btc-run-2026/discovery-search.json
```

Discovery is blocked before engine invocation unless:

- the calibration result self-verifies;
- it belongs to the exact calibration/study/dataset lineage in the bundle;
- `safe_max_candidates` is at least the bundled discovery search budget.

The run must then use `bulk_entry_membership_exact_v1`, pass runtime parity, remain discovery
only and reproduce the exact bundled study/dataset/search fingerprints.

The normal `parameter_extract.discovery_search` schema is preserved so the output can go
straight into:

```bash
pextract freeze-candidates \
  --search-result btc-run-2026/discovery-search.json \
  --output btc-run-2026/frozen-candidates.json
```

The discovery result also contains a `research_bundle` lineage block with the bundle
fingerprint, calibration-result file SHA-256, calibration-result semantic fingerprint,
calibrated safe cap and required search cap.

## What this does not do

The bundle layer does **not**:

- invent a safe 50k/100k/1M cap without real data and the target machine;
- alter search ranges or candidate caps;
- use validation or holdout during calibration/discovery;
- automatically continue to validation, robustness, portfolio selection or holdout;
- convert a failed calibration into a smaller hidden search;
- mutate the live trading bot.

After a successful bundled discovery, the existing freeze -> validation -> robustness ->
families -> portfolio -> selection -> sealed holdout -> risk -> exchange-risk -> deployment
chain remains unchanged.
