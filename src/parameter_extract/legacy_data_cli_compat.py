from __future__ import annotations

from pathlib import Path
from typing import Any

from . import legacy_source_bootstrap as bootstrap
from .legacy_data_cli import main as _delegate_main
from .legacy_paramderive import (
    INTERVAL,
    MINUTE_MS,
    SYMBOL,
    MonthKey,
    _expected_month_rows,
    _month_bounds_ms,
    _validate_digest,
)
from .manifest import sha256_file

LEGACY_ACCEPTED_SOURCE_KIND = "LEGACY_ACCEPTED_BTC1"
LEGACY_ACCEPTED_EVIDENCE = "legacy_accepted_contract"
EXTENDED_BINANCE_EVIDENCE = "extended_binance_provenance"


def validate_kline_source_manifest_compat(
    manifest: dict[str, Any], *, btc1_path: Path, month: MonthKey
) -> None:
    """Validate both historical accepted BTC1 manifests and newer enriched manifests.

    `backtest_vCHATGPT_v5.0`'s canonical BTC1 reader accepted a month from the core
    ACCEPTED/date/row/boundary/hash contract. `source_kind`, `rest_verification` and archive
    checksum metadata were added by the later repair pipeline but were never required by the
    reader for already-accepted months. Preserve that exact compatibility boundary here.

    Missing optional provenance is made explicit in the in-memory normalized provenance only;
    the source JSON file is never modified, so the exact legacy raw-manifest fingerprint is
    unchanged.
    """
    required = {
        "status",
        "symbol",
        "interval",
        "year",
        "month",
        "rows",
        "first_open_time_ms",
        "last_open_time_ms",
        "btc1_sha256",
    }
    missing = required - manifest.keys()
    if missing:
        raise ValueError(f"BTC1 manifest missing {sorted(missing)}: {month.text}")
    if manifest["status"] != "ACCEPTED":
        raise ValueError(f"BTC1 month is not ACCEPTED: {month.text}")
    if manifest["symbol"] != SYMBOL or manifest["interval"] != INTERVAL:
        raise ValueError(f"BTC1 symbol/interval mismatch: {month.text}")
    if int(manifest["year"]) != month.year or int(manifest["month"]) != month.month:
        raise ValueError(f"BTC1 manifest month mismatch: {month.text}")

    expected_rows = _expected_month_rows(month)
    start_ms, end_ms = _month_bounds_ms(month)
    if int(manifest["rows"]) != expected_rows:
        raise ValueError(f"BTC1 manifest is not a complete UTC month: {month.text}")
    if int(manifest["first_open_time_ms"]) != start_ms:
        raise ValueError(f"BTC1 first timestamp mismatch: {month.text}")
    if int(manifest["last_open_time_ms"]) != end_ms - MINUTE_MS:
        raise ValueError(f"BTC1 last timestamp mismatch: {month.text}")

    if not btc1_path.is_file():
        raise ValueError(f"BTC1 binary not found: {btc1_path}")
    expected_sha = str(manifest["btc1_sha256"]).lower()
    _validate_digest(expected_sha, label=f"BTC1 sha {month.text}")
    if sha256_file(btc1_path) != expected_sha:
        raise ValueError(f"BTC1 SHA-256 mismatch: {month.text}")

    source_kind = manifest.get("source_kind")
    rest_verification = manifest.get("rest_verification")
    archives = manifest.get("archives")

    if source_kind is None:
        evidence_level = LEGACY_ACCEPTED_EVIDENCE
        # Add only to the parsed object used for normalized provenance. Raw source bytes remain
        # untouched and continue to feed the exact legacy fingerprint separately.
        manifest["source_kind"] = LEGACY_ACCEPTED_SOURCE_KIND
    else:
        if not isinstance(source_kind, str) or not source_kind.startswith("BINANCE_"):
            raise ValueError(f"BTC1 source_kind is invalid when present: {month.text}")
        evidence_level = EXTENDED_BINANCE_EVIDENCE

    if rest_verification is not None and not isinstance(rest_verification, dict):
        raise ValueError(f"BTC1 REST verification metadata invalid: {month.text}")

    if archives is not None:
        if not isinstance(archives, list):
            raise ValueError(f"BTC1 archives metadata invalid: {month.text}")
        for artifact in archives:
            if not isinstance(artifact, dict):
                raise ValueError(f"BTC1 archive metadata invalid: {month.text}")
            official_value = artifact.get("official_sha256")
            actual_value = artifact.get("actual_sha256")
            if official_value is None and actual_value is None:
                continue
            if not isinstance(official_value, str) or not isinstance(actual_value, str):
                raise ValueError(f"BTC1 archive checksum metadata incomplete: {month.text}")
            official = official_value.lower()
            actual = actual_value.lower()
            _validate_digest(official, label=f"BTC1 archive checksum {month.text}")
            _validate_digest(actual, label=f"BTC1 archive actual sha {month.text}")
            if official != actual:
                raise ValueError(f"BTC1 archive checksum evidence disagrees: {month.text}")

    if source_kind is not None and rest_verification is None:
        # The historical reader did not require this field. Do not downgrade an otherwise
        # valid accepted month, but record that the enriched provenance is incomplete.
        evidence_level = LEGACY_ACCEPTED_EVIDENCE

    manifest["parameter_extract_source_evidence"] = evidence_level


def _install_compatibility_adapter() -> None:
    bootstrap._validate_kline_source_manifest = validate_kline_source_manifest_compat


def main(argv: list[str] | None = None) -> int:
    _install_compatibility_adapter()
    return _delegate_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
