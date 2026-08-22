from __future__ import annotations

from pathlib import Path

import pytest

import parameter_extract.legacy_data_cli_compat as compat
import parameter_extract.legacy_source_bootstrap as bootstrap
from parameter_extract.legacy_paramderive import MonthKey
from parameter_extract.manifest import sha256_file


def _manifest(path: Path) -> dict[str, object]:
    return {
        "status": "ACCEPTED",
        "symbol": "BTCUSDT",
        "interval": "1m",
        "year": 2020,
        "month": 1,
        "rows": 3,
        "first_open_time_ms": 0,
        "last_open_time_ms": 120_000,
        "btc1_sha256": sha256_file(path),
    }


def _patch_tiny_month(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat, "_expected_month_rows", lambda month: 3)
    monkeypatch.setattr(compat, "_month_bounds_ms", lambda month: (0, 180_000))


def test_historical_accepted_manifest_without_enriched_provenance_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tiny_month(monkeypatch)
    btc1 = tmp_path / "2020.bin"
    btc1.write_bytes(b"historical accepted BTC1 bytes")
    manifest = _manifest(btc1)

    compat.validate_kline_source_manifest_compat(
        manifest, btc1_path=btc1, month=MonthKey(2020, 1)
    )

    assert manifest["source_kind"] == compat.LEGACY_ACCEPTED_SOURCE_KIND
    assert manifest["parameter_extract_source_evidence"] == compat.LEGACY_ACCEPTED_EVIDENCE
    assert "rest_verification" not in manifest


def test_modern_enriched_manifest_keeps_extended_binance_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tiny_month(monkeypatch)
    btc1 = tmp_path / "2020.bin"
    btc1.write_bytes(b"modern accepted BTC1 bytes")
    manifest = _manifest(btc1)
    manifest.update(
        {
            "source_kind": "BINANCE_MONTHLY_ARCHIVE",
            "rest_verification": {"mode": "full_archive_comparison"},
            "archives": [
                {
                    "official_sha256": "a" * 64,
                    "actual_sha256": "a" * 64,
                }
            ],
        }
    )

    compat.validate_kline_source_manifest_compat(
        manifest, btc1_path=btc1, month=MonthKey(2020, 1)
    )

    assert manifest["source_kind"] == "BINANCE_MONTHLY_ARCHIVE"
    assert (
        manifest["parameter_extract_source_evidence"]
        == compat.EXTENDED_BINANCE_EVIDENCE
    )


def test_present_but_invalid_source_kind_is_not_silently_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tiny_month(monkeypatch)
    btc1 = tmp_path / "2020.bin"
    btc1.write_bytes(b"accepted BTC1 bytes")
    manifest = _manifest(btc1)
    manifest["source_kind"] = "UNKNOWN_SOURCE"

    with pytest.raises(ValueError, match="source_kind is invalid"):
        compat.validate_kline_source_manifest_compat(
            manifest, btc1_path=btc1, month=MonthKey(2020, 1)
        )


def test_present_archive_checksum_mismatch_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tiny_month(monkeypatch)
    btc1 = tmp_path / "2020.bin"
    btc1.write_bytes(b"accepted BTC1 bytes")
    manifest = _manifest(btc1)
    manifest.update(
        {
            "source_kind": "BINANCE_MONTHLY_ARCHIVE",
            "rest_verification": {"mode": "full_archive_comparison"},
            "archives": [
                {
                    "official_sha256": "a" * 64,
                    "actual_sha256": "b" * 64,
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="checksum evidence disagrees"):
        compat.validate_kline_source_manifest_compat(
            manifest, btc1_path=btc1, month=MonthKey(2020, 1)
        )


def test_binary_sha_mismatch_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tiny_month(monkeypatch)
    btc1 = tmp_path / "2020.bin"
    btc1.write_bytes(b"accepted BTC1 bytes")
    manifest = _manifest(btc1)
    manifest["btc1_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="BTC1 SHA-256 mismatch"):
        compat.validate_kline_source_manifest_compat(
            manifest, btc1_path=btc1, month=MonthKey(2020, 1)
        )


def test_cli_installer_replaces_bootstrap_validator() -> None:
    original = bootstrap._validate_kline_source_manifest
    try:
        compat._install_compatibility_adapter()
        assert bootstrap._validate_kline_source_manifest is compat.validate_kline_source_manifest_compat
    finally:
        bootstrap._validate_kline_source_manifest = original
