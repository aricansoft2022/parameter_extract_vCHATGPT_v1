import csv
import json
import struct
import zipfile
from pathlib import Path

import pytest

import parameter_extract.legacy_paramderive as legacy
from parameter_extract.manifest import sha256_file, verify_manifest


def _patch_tiny_month(monkeypatch):
    monkeypatch.setattr(legacy, "_expected_month_rows", lambda month: 3)
    monkeypatch.setattr(legacy, "_month_bounds_ms", lambda month: (0, 180_000))


def _write_btc1(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamps = (0, 60_000, 120_000)
    opens = (100.0, 101.0, 102.0)
    highs = (101.0, 102.0, 103.0)
    lows = (99.0, 100.0, 101.0)
    closes = (100.5, 101.5, 102.5)
    payload = bytearray(legacy.BTC1_HEADER.pack(legacy.BTC1_MAGIC, 3))
    payload.extend(struct.pack("<3Q", *timestamps))
    for values in (opens, highs, lows, closes):
        payload.extend(struct.pack("<3d", *values))
    path.write_bytes(bytes(payload))


def _npy(values, *, descr: str, fmt: str) -> bytes:
    header_text = repr(
        {"descr": descr, "fortran_order": False, "shape": (len(values),)}
    )
    header = header_text.encode("latin1") + b"\n"
    return (
        b"\x93NUMPY"
        + bytes((1, 0))
        + struct.pack("<H", len(header))
        + header
        + struct.pack(f"<{len(values)}{fmt}", *values)
    )


def _write_funding_npz(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "timestamps_ms.npy",
            _npy((30_000, 90_000), descr="<i8", fmt="q"),
        )
        archive.writestr(
            "rates.npy",
            _npy((-0.001, 0.002), descr="<f8", fmt="d"),
        )


def _source_store(tmp_path: Path, *, accepted_at: str = "2026-08-01T00:00:00Z"):
    btc1_root = tmp_path / "btc1"
    funding_root = tmp_path / "funding"
    btc1_path = btc1_root / "ocak" / "2020.bin"
    funding_path = funding_root / "ocak" / "2020.npz"
    _write_btc1(btc1_path)
    _write_funding_npz(funding_path)

    btc1_manifest = {
        "schema": "paramderive-binance-month/v1",
        "status": "ACCEPTED",
        "accepted_at_utc": accepted_at,
        "symbol": "BTCUSDT",
        "interval": "1m",
        "year": 2020,
        "month": 1,
        "month_directory": "ocak",
        "rows": 3,
        "first_open_time_ms": 0,
        "last_open_time_ms": 120_000,
        "source_kind": "BINANCE_MONTHLY_ARCHIVE",
        "monthly_attempt_error": None,
        "daily_attempt_error": None,
        "archives": [
            {
                "url": "https://data.binance.vision/example.zip",
                "checksum_url": "https://data.binance.vision/example.zip.CHECKSUM",
                "official_sha256": "a" * 64,
                "actual_sha256": "a" * 64,
            }
        ],
        "rest_verification": {
            "mode": "full_archive_comparison",
            "requests": 1,
            "compared_rows": 3,
            "selected_source": "BINANCE_MONTHLY_ARCHIVE",
            "url": "https://fapi.binance.com/fapi/v1/klines",
        },
        "btc1_sha256": sha256_file(btc1_path),
        "sync_status": "ACCEPTED",
    }
    (btc1_root / "ocak" / "2020.json").write_text(
        json.dumps(btc1_manifest), encoding="utf-8"
    )

    funding_manifest = {
        "schema": "paramderive-funding-month/v1",
        "status": "ACCEPTED",
        "accepted_at_utc": accepted_at,
        "symbol": "BTCUSDT",
        "year": 2020,
        "month": 1,
        "rows": 2,
        "first_timestamp_ms": 30_000,
        "last_timestamp_ms": 90_000,
        "source_kind": "BINANCE_MONTHLY_FUNDING_ARCHIVE",
        "archive_url": "https://data.binance.vision/funding.zip",
        "checksum_url": "https://data.binance.vision/funding.zip.CHECKSUM",
        "official_sha256": "b" * 64,
        "actual_sha256": "b" * 64,
        "rest_verification": None,
        "data_sha256": sha256_file(funding_path),
        "negative_funding_policy": "ignored",
        "positive_multiplier": 1.10,
        "sync_status": "ACCEPTED",
    }
    (funding_root / "ocak" / "2020.json").write_text(
        json.dumps(funding_manifest), encoding="utf-8"
    )
    return btc1_root, funding_root


def test_migration_decodes_btc1_preserves_raw_funding_and_pins_provenance(
    tmp_path: Path, monkeypatch
):
    _patch_tiny_month(monkeypatch)
    btc1_root, funding_root = _source_store(tmp_path)
    output = tmp_path / "bundle-data"

    result = legacy.migrate_paramderive_market_data(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2020-01",
        end="2020-01",
        output_directory=output,
    )

    assert result["candle_rows"] == 3
    assert result["funding_rows"] == 2
    assert result["source_reader_commit"] == legacy.LEGACY_SOURCE_COMMIT
    assert output.is_dir()
    assert not (tmp_path / ".bundle-data.importing").exists()

    with (output / "candles.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["open_time"] == "0"
    assert rows[0]["volume"] == "0"
    assert rows[0]["close_time"] == "59999"
    assert float(rows[-1]["close"]) == 102.5

    with (output / "funding.csv").open(newline="", encoding="utf-8") as handle:
        funding_rows = list(csv.DictReader(handle))
    assert [float(row["rate"]) for row in funding_rows] == [-0.001, 0.002]
    assert [row["mark_price"] for row in funding_rows] == ["", ""]

    provenance = json.loads((output / "source-provenance.json").read_text(encoding="utf-8"))
    assert provenance["source_reader_commit"] == legacy.LEGACY_SOURCE_COMMIT
    assert provenance["migration_policies"]["funding_rate"].startswith("raw stored Binance")
    assert provenance["months"][0]["kline_manifest"].get("accepted_at_utc") is None
    assert provenance["months"][0]["funding_manifest"].get("sync_status") is None

    manifest = json.loads((output / "data-manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"candles", "funding", "source_provenance"}
    assert manifest["candles"]["integrity_ok"] is True
    assert verify_manifest(manifest, directory=output) == []
    assert result["dataset_fingerprint_sha256"] == manifest["dataset_fingerprint_sha256"]


def test_operational_acceptance_timestamp_does_not_change_migrated_dataset_identity(
    tmp_path: Path, monkeypatch
):
    _patch_tiny_month(monkeypatch)
    btc1_root, funding_root = _source_store(
        tmp_path, accepted_at="2026-08-01T00:00:00Z"
    )
    first = legacy.migrate_paramderive_market_data(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2020-01",
        end="2020-01",
        output_directory=tmp_path / "first",
    )

    for path in (
        btc1_root / "ocak" / "2020.json",
        funding_root / "ocak" / "2020.json",
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["accepted_at_utc"] = "2026-08-22T12:34:56Z"
        payload["sync_status"] = "SKIPPED"
        path.write_text(json.dumps(payload), encoding="utf-8")

    second = legacy.migrate_paramderive_market_data(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2020-01",
        end="2020-01",
        output_directory=tmp_path / "second",
    )

    assert first["source_provenance_fingerprint_sha256"] == second[
        "source_provenance_fingerprint_sha256"
    ]
    assert first["dataset_fingerprint_sha256"] == second["dataset_fingerprint_sha256"]
    assert (tmp_path / "first" / "candles.csv").read_bytes() == (
        tmp_path / "second" / "candles.csv"
    ).read_bytes()
    assert (tmp_path / "first" / "funding.csv").read_bytes() == (
        tmp_path / "second" / "funding.csv"
    ).read_bytes()


def test_tampering_a_pinned_provenance_file_breaks_manifest_verification(
    tmp_path: Path, monkeypatch
):
    _patch_tiny_month(monkeypatch)
    btc1_root, funding_root = _source_store(tmp_path)
    output = tmp_path / "bundle-data"
    legacy.migrate_paramderive_market_data(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2020-01",
        end="2020-01",
        output_directory=output,
    )
    manifest = json.loads((output / "data-manifest.json").read_text(encoding="utf-8"))
    (output / "source-provenance.json").write_text("{}\n", encoding="utf-8")
    problems = verify_manifest(manifest, directory=output)
    assert any("source_provenance: sha256 mismatch" in problem for problem in problems)


def test_source_hash_failure_leaves_no_partial_output(tmp_path: Path, monkeypatch):
    _patch_tiny_month(monkeypatch)
    btc1_root, funding_root = _source_store(tmp_path)
    with (btc1_root / "ocak" / "2020.bin").open("ab") as handle:
        handle.write(b"tamper")
    output = tmp_path / "bundle-data"

    with pytest.raises(ValueError, match="BTC1 SHA-256 mismatch"):
        legacy.migrate_paramderive_market_data(
            btc1_root=btc1_root,
            funding_root=funding_root,
            start="2020-01",
            end="2020-01",
            output_directory=output,
        )
    assert not output.exists()
    assert not (tmp_path / ".bundle-data.importing").exists()


def test_parse_month_and_calendar_row_count():
    assert legacy.parse_month("2020-02") == legacy.MonthKey(2020, 2)
    assert legacy._expected_month_rows(legacy.MonthKey(2020, 2)) == 41_760
    with pytest.raises(ValueError):
        legacy.parse_month("2020-2")
