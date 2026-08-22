# Research run bundle

`pextract-bundle` is the real-data entry point for the accelerated discovery engine.
It binds byte-level dataset identity, the study contract, the intended discovery grid and the
machine-specific scale calibration ladder into one immutable lineage.

The bundle exists to prevent a common research failure: calibrating one grid, dataset or
machine and then silently running a different discovery search elsewhere.

## Directory model

Keep contract files together under one bundle directory. Historical candle/funding bytes may
live elsewhere and are supplied through `--data-directory`.

```text
btc-run-2026/
  data-manifest.json
  study.json
  discovery-search.json
  scale-calibration.json
  calibration-10k.json
  calibration-50k.json
  bundle.json              # created last by pextract-bundle seal

/data/binance/BTCUSDT/
  BTCUSDT-1m-history.csv
  BTCUSDT-funding.csv
```

All contract paths stored inside `bundle.json`, `study.json` and `scale-calibration.json` must
resolve inside the bundle directory. `../` escapes and absolute stored paths are rejected. The
data files themselves are located through the manifest and `--data-directory`, then re-hashed.

## Bundle contract

The final `bundle.json` looks like this:

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

Do not hand-copy those bundle-level fingerprints. `pextract-bundle seal` computes them from
the authored contracts and refuses to create the final file unless the complete lineage
verifies.

## Construction order

Build the inputs in this order so every downstream contract can pin the upstream identity:

1. Create the candle/funding manifest with `pextract manifest` and record its
   `dataset_fingerprint_sha256`.
2. Create `study.json` with that dataset fingerprint and the final discovery, validation and
   sealed-holdout windows.
3. Create the intended `discovery-search.json`.
4. Create optional smaller calibration search contracts with increasing `max_candidates`
   budgets for earlier ladder stages.
5. Include the **exact `discovery-search.json` contract itself** as a calibration stage at its
   declared `refinement.max_candidates` budget.
6. Create `scale-calibration.json`, pinning the exact study/dataset plus every stage search
   fingerprint.
7. Seal the final bundle from those files.

The study/calibration contracts still need the upstream semantic fingerprints they explicitly
pin, but `bundle.json` itself is generated rather than manually assembled.

## Step 0: seal the bundle

Run the sealer with contract paths that are inside the output bundle directory. Relative paths
are interpreted relative to the output directory; absolute input paths are allowed only when
they still resolve inside that directory.

```bash
pextract-bundle seal \
  --name "BTCUSDT representative run 2026-08" \
  --manifest btc-run-2026/data-manifest.json \
  --study btc-run-2026/study.json \
  --search btc-run-2026/discovery-search.json \
  --calibration btc-run-2026/scale-calibration.json \
  --data-directory /data/binance/BTCUSDT \
  --output btc-run-2026/bundle.json
```

The sealer:

- computes manifest/study/search/calibration fingerprints from the actual files;
- writes a temporary strict-JSON bundle;
- runs the full static `verify_research_bundle()` gate against the real data bytes;
- atomically renames the temporary file to `bundle.json` only after verification succeeds;
- refuses to overwrite an existing `bundle.json`;
- removes the temporary file if verification fails;
- never edits or copies the upstream contract files.

If a lineage input changes intentionally, create a new bundle output rather than silently
replacing the old sealed bundle.

The calibration ladder must reach the intended discovery budget, and at least one stage must
have both the exact discovery-search fingerprint and that exact candidate budget. A different
grid with the same candidate count is not accepted as proof that the intended discovery grid
has the same cost.

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
- presence of an exact-discovery calibration stage;
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

The exact discovery grid is therefore executed once during calibration as a resource/parity
proof. Its full frontier is not retained by the scale artifact, so the later discovery command
runs it again to produce the canonical discovery-search result.

The result records only the last passing stage as `safe_max_candidates`. The bundle command
adds bundle lineage metadata and recomputes the scale-result fingerprint, so the stored result
still passes `pextract-scale verify`.

Calibration also records the Python/platform/CPU environment. That machine metadata is part
of the discovery gate. Copying a successful calibration result to a different runtime or
machine does not authorize discovery there; recalibrate on that environment.

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
- its machine metadata exactly matches the current Python/platform/CPU environment;
- `safe_max_candidates` is at least the bundled discovery search budget;
- the exact discovery-search calibration stage itself has PASSed.

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
calibrated machine, calibrated safe cap, exact discovery-calibration stage names and required
search cap.

## What this does not do

The bundle layer does **not**:

- invent a safe 50k/100k/1M cap without real data and the target machine;
- assume equal candidate counts imply equal runtime across different grids;
- reuse a calibration from a different machine/runtime;
- alter search ranges or candidate caps;
- use validation or holdout during calibration/discovery;
- automatically continue to validation, robustness, portfolio selection or holdout;
- convert a failed calibration into a smaller hidden search;
- overwrite an existing sealed bundle;
- mutate the live trading bot.

After a successful bundled discovery, the existing freeze -> validation -> robustness ->
families -> portfolio -> selection -> sealed holdout -> risk -> exchange-risk -> deployment
chain remains unchanged.
