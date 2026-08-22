# Accepted paramderive market-data migration

`pextract-migrate-paramderive` converts the already-accepted Binance monthly store from
`backtest_vCHATGPT_v5.0` into the CSV + manifest format consumed by `parameter_extract`.

This is a migration path, not a second market-data downloader. The source store remains
read-only and its verification lineage is preserved in normalized `source-provenance.json`.

## Source contract

The reader is intentionally pinned to the accepted-store format audited from:

```text
repository: aricansoft2022/backtest_vCHATGPT_v5.0
commit:     6ac4f2d03ffa7c583956869e5210139fb83ff5cb
```

That source system accepted BTCUSDT 1m months only after Binance-source validation. For
public archive months the normal path verifies official ZIP CHECKSUM data and compares the
complete month against Futures REST; the source code also records its fallback/dual-pass
verification path. Funding months are SHA-pinned accepted NPZ files sourced from official
Binance funding archives or the documented pre-archive REST fallback.

The migration does not trust filenames alone. Every requested month is rechecked against its
ACCEPTED source manifest and exact stored data SHA before decoding.

## Archived 2026-08-07 reference lineage

The Git-friendly final archive in `backtest_vCHATGPT_v5.0` records:

```text
candle range:          2019-12 .. 2026-07
candle months:         80
candle rows:           3,506,400
funding required from: 2020-01
funding range:         2020-01 .. 2026-07
funding months:        79
legacy data fingerprint:
19e566d197f1266094faed171c6ee4936b822b3d5f061e8b405604b8aff5021c
```

The December 2019 candle month is indicator warm-up. It is intentionally earlier than the
funding-required boundary. The migration therefore treats candle start and funding start as
separate contract fields.

## Typical source directories

The old project documented paths like:

```text
../past_BNN_data/data/BTCUSDT/1m/
  aralık/2019.bin
  aralık/2019.json
  ocak/2020.bin
  ocak/2020.json
  ...

../backtest_vCHATGPT_v5.0/market-data/funding/BTCUSDT/
  ocak/2020.npz
  ocak/2020.json
  ...
```

The funding root may live elsewhere on the machine; pass the actual path.

## Step 1: source preflight

Before writing a multi-gigabyte migrated CSV, validate the local ACCEPTED store and compute
the exact legacy fingerprint:

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

Preflight performs no output migration. It validates each required source manifest/hash and
reproduces the exact legacy `prepare.dataset_fingerprint()` algorithm from the pinned reader:

```text
"funding-required-from:YYYY-MM\n"
+ raw BTC1 monthly manifest bytes
+ BTC1 binary SHA-256 strings
+ raw funding monthly manifest bytes at/after funding_required_from
```

The report contains both the computed legacy fingerprint and
`legacy_fingerprint_matches_reference`.

A mismatch is important evidence, but it does not automatically mean the accepted market
bytes are bad. The old algorithm hashes raw JSON bytes, including operational fields such as
`accepted_at_utc`; recreating otherwise identical ACCEPTED manifests on another day can
therefore change the old fingerprint. A mismatch means only that the byte-exact old data
lineage has not been reproduced and must not be claimed as such.

If byte-exact recreation is specifically required, add:

```text
--require-legacy-fingerprint-match
```

when running migration. That option fails before creating output unless the supplied legacy
reference matches exactly.

## Step 2: migration

For the archived complete-month range:

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

`--funding-start` defaults to `--start`, so existing same-range uses remain compatible.
Months before the funding boundary contribute candles only and require no funding NPZ.

The destination must not already exist. The command builds a sibling temporary directory and
only renames it into place after every source month and the generated manifest pass
verification. A failure leaves no partially published output directory.

## Output

```text
btc-run-2026/data/
  candles.csv
  funding.csv
  source-provenance.json
  legacy-preflight.json
  data-manifest.json
```

`data-manifest.json` pins `candles.csv`, `funding.csv` and normalized
`source-provenance.json`. The standard `pextract verify-manifest`/research-bundle verifier
therefore detects later edits to semantic source provenance as well as candles/funding.

`legacy-preflight.json` is deliberately an operational audit sidecar rather than part of the
new dataset fingerprint. It records the exact old raw-manifest fingerprint comparison. This
separation is necessary because the old fingerprint includes nondeterministic acceptance
metadata, whereas the new dataset identity intentionally does not.

The migration command also prints the SHA-256 of `legacy-preflight.json`, so a run log can
retain exact evidence of the preflight report.

### Candle conversion

Legacy BTC1 stores exact minute open timestamps plus float64 OHLC columns. The migration:

- validates BTC1 magic/size/count;
- requires every timestamp to equal the exact UTC minute expected for that full month;
- validates finite, positive and internally consistent OHLC;
- writes float values with Python's round-trip representation;
- sets `close_time = open_time + 59999`;
- writes `volume=0` because BTC1 never stored volume.

Volume is explicitly documented as an unavailable-source placeholder. It is safe for this
engine because volume is not part of the strategy signal, execution, funding or risk model.

### Funding conversion

Legacy funding NPZ stores raw `timestamps_ms` and raw `rates`. Only months at or after
`--funding-start` are exported. The migration decodes those arrays without applying the old
research engine's execution policy.

In particular it does **not**:

- suppress negative funding;
- multiply positive funding by `1.10`;
- carry forward any old leverage/liquidation assumptions.

The new `funding.csv` contains the raw stored rate. `mark_price` is blank because the legacy
NPZ did not retain it; `parameter_extract` therefore uses its already-documented enclosing
candle-close approximation when a mark price is unavailable.

## Stable provenance identity

Old ACCEPTED month manifests contain operational fields such as `accepted_at_utc` and
`sync_status`. Recreating the same verified month on another day can legitimately change
those fields without changing the market bytes or verification evidence.

The normalized source provenance excludes only these operational fields from each
source-manifest semantic fingerprint:

```text
accepted_at_utc
sync_status
```

It still pins the actual BTC1/NPZ SHA-256 values, source kind, official archive checksum
evidence and REST-verification metadata. Therefore changing an acceptance timestamp alone
does not create a fake new `parameter_extract` dataset fingerprint, while changing the
verified market bytes or substantive verification lineage does.

The exact old raw fingerprint and the normalized new dataset fingerprint are intentionally
different concepts and are both reported.

## Wiring into a research bundle

After migration, author the study inside the bundle directory and point it at the migrated
manifest. For example, if `study.json` is `btc-run-2026/study.json`:

```json
{
  "dataset_manifest": "data/data-manifest.json",
  "dataset_fingerprint_sha256": "COPY_FROM_MIGRATED_DATA_MANIFEST"
}
```

Then author the exact discovery-search and scale-calibration contracts and seal the bundle:

```bash
pextract-bundle seal \
  --name "BTCUSDT representative run 2026" \
  --manifest btc-run-2026/data/data-manifest.json \
  --study btc-run-2026/study.json \
  --search btc-run-2026/discovery-search.json \
  --calibration btc-run-2026/scale-calibration.json \
  --data-directory btc-run-2026/data \
  --output btc-run-2026/bundle.json
```

From there use the normal bundle sequence:

```text
seal -> verify -> calibrate -> discovery -> freeze -> validation -> ... -> sealed holdout
```

## Deliberate limitations

V1 is intentionally narrow:

- BTCUSDT only, because the source BTC1 contract is BTCUSDT-specific;
- complete UTC months only;
- accepted source manifests are mandatory;
- no source repair/download is performed;
- no partial-month stitching;
- no synthetic OHLC or forward-fill;
- volume cannot be recovered from BTC1;
- funding mark price cannot be recovered from the legacy NPZ.

If the accepted monthly store is missing a required month, repair/sync that source with its
original audited Binance-only pipeline first rather than silently mixing a second data source
inside the migration.
