# Accepted paramderive market-data migration

`pextract-migrate-paramderive` converts the already-accepted Binance monthly store from
`backtest_vCHATGPT_v5.0` into the CSV + manifest format consumed by `parameter_extract`.

This is a migration path, not a second market-data downloader. The source store remains
read-only and its verification lineage is preserved in `source-provenance.json`.

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

## Typical source directories

The old project documented paths like:

```text
../past_BNN_data/data/BTCUSDT/1m/
  ocak/2020.bin
  ocak/2020.json
  şubat/2020.bin
  şubat/2020.json
  ...

../backtest_vCHATGPT_v5.0/market-data/funding/BTCUSDT/
  ocak/2020.npz
  ocak/2020.json
  ...
```

The funding root may of course live elsewhere on the machine; pass the actual path.

## Migration

Choose complete UTC months only. For a research lineage beginning in January 2020, keeping
2019-12 in the dataset is useful because the study engine may need pre-window indicator
warm-up candles. Do not include an unfinished current month merely to make the dataset look
more recent.

Example:

```bash
pextract-migrate-paramderive \
  --btc1-root ../past_BNN_data/data/BTCUSDT/1m \
  --funding-root ../backtest_vCHATGPT_v5.0/market-data/funding/BTCUSDT \
  --start 2019-12 \
  --end 2026-07 \
  --output-directory btc-run-2026/data
```

The destination must not already exist. The command builds a sibling temporary directory and
only renames it into place after every source month and the generated manifest pass
verification. A failure leaves no partially published output directory.

## Output

```text
btc-run-2026/data/
  candles.csv
  funding.csv
  source-provenance.json
  data-manifest.json
```

`data-manifest.json` pins all three other files, including the provenance document. The
standard `pextract verify-manifest`/research-bundle verifier therefore detects later edits to
source provenance as well as edits to candles/funding.

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

Legacy funding NPZ stores raw `timestamps_ms` and raw `rates`. The migration decodes those
arrays without applying the old research engine's execution policy.

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

The migration excludes only these operational fields from each source-manifest semantic
fingerprint:

```text
accepted_at_utc
sync_status
```

It still pins the actual BTC1/NPZ SHA-256 values, source kind, official archive checksum
evidence and REST-verification metadata. Therefore changing an acceptance timestamp alone
does not create a fake new dataset fingerprint, while changing the verified market bytes or
the substantive verification lineage does.

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
