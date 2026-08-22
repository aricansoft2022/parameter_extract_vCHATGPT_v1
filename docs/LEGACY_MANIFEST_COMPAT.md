# Historical BTC1 manifest compatibility

The archived `past_BNN_data` store can contain BTC1 month manifests that were accepted by
`backtest_vCHATGPT_v5.0` before the later repair pipeline began writing enriched provenance
fields such as `source_kind`, `rest_verification`, and `archives`.

That is valid legacy input. The pinned legacy reader in `src/paramderive/btc1.py` accepted an
existing month from the following fail-closed core contract:

- `status == ACCEPTED`;
- exact `BTCUSDT` / `1m` identity;
- path year/month equals manifest year/month;
- complete UTC month row count;
- exact first/last one-minute timestamps;
- exact `btc1_sha256` match against the stored binary.

`pextract-migrate-paramderive` therefore uses the same compatibility boundary for historical
already-accepted BTC1 manifests. Missing enriched provenance does **not** cause rejection.
Instead, normalized migration provenance records:

```text
source_kind: LEGACY_ACCEPTED_BTC1
parameter_extract_source_evidence: legacy_accepted_contract
```

This annotation exists only in the parsed/normalized migration provenance. The original
source JSON file is never modified, and the exact legacy fingerprint continues to hash the
original raw manifest bytes.

When enriched fields are present, the migration does not ignore them. A present
`source_kind` must be a Binance source, REST metadata must be structurally valid, and present
archive checksum pairs must agree. Core date/row/hash checks remain mandatory in every case.

Funding validation is unchanged. Newly regenerated funding months keep the modern Binance
archive checksum provenance produced by the audited legacy funding-sync pipeline.

## Canonical preflight

After updating and reinstalling the project, the archived 2026-08-07 source check is:

```bash
pextract-migrate-paramderive \
  --btc1-root /Users/turanarican/Desktop/past_BNN_data/data/BTCUSDT/1m \
  --funding-root /Users/turanarican/Desktop/backtest_vCHATGPT_v5.0/market-data/funding/BTCUSDT \
  --start 2019-12 \
  --funding-start 2020-01 \
  --end 2026-07 \
  --legacy-fingerprint-reference 19e566d197f1266094faed171c6ee4936b822b3d5f061e8b405604b8aff5021c \
  --preflight-only
```

Because the legacy exact fingerprint hashes raw funding manifest bytes, rebuilding the
funding store can legitimately make `legacy_fingerprint_matches_reference` false even when
all regenerated funding bytes are independently checksum-verified. The new normalized
`parameter_extract` dataset identity is intentionally separate from that operational raw
manifest fingerprint.
