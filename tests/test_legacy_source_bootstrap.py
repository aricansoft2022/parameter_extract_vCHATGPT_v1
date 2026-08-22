import csv
import hashlib
import json
import struct
import zipfile
from pathlib import Path

import pytest

import parameter_extract.legacy_paramderive as legacy
import parameter_extract.legacy_source_bootstrap as bootstrap
from parameter_extract.manifest import sha256_file, verify_manifest


def _patch_two_tiny_months(monkeypatch):
    def rows(_month):
        return 3

    def bounds(month):
        if (month.year, month.month) == (2019, 12):
            return 0, 180_000
        if (month.year, month.month) == (2020, 1):
            return 180_000, 360_000
        raise AssertionError(month)

    monkeypatch.setattr(legacy, "_expected_month_rows", rows)
    monkeypatch.setattr(legacy, "_month_bounds_ms", bounds)
    monkeypatch.setattr(bootstrap, "_expected_month_rows", rows)


def _write_btc1(path: Path, start_ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamps = (start_ms, start_ms + 60_000, start_ms + 120_000)
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
    header_text = repr({"descr": descr, "fortran_order": False, "shape": (len(values),)})
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
            _npy((210_000, 300_000), descr="<i8", fmt="q"),
        )
        archive.writestr(
            "rates.npy",
            _npy((-0.001, 0.002), descr="<f8", fmt="d"),
        )


def _write_kline_month(
    btc1_root: Path,
    *,
    year: int,
    month: int,
    month_name: str,
    start_ms: int,
    accepted_at: str,
) -> None:
    binary = btc1_root / month_name / f"{year}.bin"
    _write_btc1(binary, start_ms)
    manifest = {
        "schema": "paramderive-binance-month/v1",
        "status": "ACCEPTED",
        "accepted_at_utc": accepted_at,
        "symbol": "BTCUSDT",
        "interval": "1m",
        "year": year,
        "month": month,
        "month_directory": month_name,
        "rows": 3,
        "first_open_time_ms": start_ms,
        "last_open_time_ms": start_ms + 120_000,
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
        "btc1_sha256": sha256_file(binary),
        "sync_status": "ACCEPTED",
    }
    (btc1_root / month_name / f"{year}.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _write_funding_month(funding_root: Path, *, accepted_at: str) -> None:
    data = funding_root / "ocak" / "2020.npz"
    _write_funding_npz(data)
    manifest = {
        "schema": "paramderive-funding-month/v1",
        "status": "ACCEPTED",
        "accepted_at_utc": accepted_at,
        "symbol": "BTCUSDT",
        "year": 2020,
        "month": 1,
        "rows": 2,
        "first_timestamp_ms": 210_000,
        "last_timestamp_ms": 300_000,
        "source_kind": "BINANCE_MONTHLY_FUNDING_ARCHIVE",
        "archive_url": "https://data.binance.vision/funding.zip",
        "checksum_url": "https://data.binance.vision/funding.zip.CHECKSUM",
        "official_sha256": "b" * 64,
        "actual_sha256": "b" * 64,
        "rest_verification": None,
        "data_sha256": sha256_file(data),
        "negative_funding_policy": "ignored",
        "positive_multiplier": 1.10,
        "sync_status": "ACCEPTED",
    }
    (funding_root / "ocak" / "2020.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _source_store(tmp_path: Path, *, accepted_at: str = "2026-08-01T00:00:00Z"):
    btc1_root = tmp_path / "btc1"
    funding_root = tmp_path / "funding"
    _write_kline_month(
        btc1_root,
        year=2019,
        month=12,
        month_name="aralık",
        start_ms=0,
        accepted_at=accepted_at,
    )
    _write_kline_month(
        btc1_root,
        year=2020,
        month=1,
        month_name="ocak",
        start_ms=180_000,
        accepted_at=accepted_at,
    )
    _write_funding_month(funding_root, accepted_at=accepted_at)
    return btc1_root, funding_root


def _manual_legacy_fingerprint(btc1_root: Path, funding_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"funding-required-from:2020-01\n")
    for year, month_name in ((2019, "aralık"), (2020, "ocak")):
        manifest = btc1_root / month_name / f"{year}.json"
        binary = btc1_root / month_name / f"{year}.bin"
        digest.update(manifest.read_bytes())
        digest.update(sha256_file(binary).encode())
        if year == 2020:
            digest.update((funding_root / "ocak" / "2020.json").read_bytes())
    return digest.hexdigest()


def test_preflight_reproduces_legacy_fingerprint_and_does_not_require_warmup_funding(
    tmp_path: Path, monkeypatch
):
    _patch_two_tiny_months(monkeypatch)
    btc1_root, funding_root = _source_store(tmp_path)
    expected = _manual_legacy_fingerprint(btc1_root, funding_root)

    result = bootstrap.preflight_paramderive_source(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2019-12",
        funding_start="2020-01",
        end="2020-01",
        legacy_fingerprint_reference=expected,
    )

    assert result["candle_months"] == 2
    assert result["funding_months"] == 1
    assert result["expected_candle_rows"] == 6
    assert result["legacy_dataset_fingerprint_sha256"] == expected
    assert result["legacy_fingerprint_matches_reference"] is True
    assert result["months"][0]["funding_required"] is False
    assert result["months"][0]["funding_data_sha256"] is None
    assert result["months"][1]["funding_required"] is True


def test_boundary_migration_exports_warmup_candles_and_only_required_funding(
    tmp_path: Path, monkeypatch
):
    _patch_two_tiny_months(monkeypatch)
    btc1_root, funding_root = _source_store(tmp_path)
    expected = _manual_legacy_fingerprint(btc1_root, funding_root)
    output = tmp_path / "migrated"

    result = bootstrap.migrate_paramderive_market_data_with_funding_boundary(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2019-12",
        funding_start="2020-01",
        end="2020-01",
        output_directory=output,
        legacy_fingerprint_reference=expected,
        require_legacy_fingerprint_match=True,
    )

    assert result["candle_months"] == 2
    assert result["funding_months"] == 1
    assert result["candle_rows"] == 6
    assert result["funding_rows"] == 2
    assert result["legacy_fingerprint_matches_reference"] is True

    with (output / "candles.csv").open(newline="", encoding="utf-8") as handle:
        candles = list(csv.DictReader(handle))
    assert len(candles) == 6
    assert [int(row["open_time"]) for row in candles] == [
        0,
        60_000,
        120_000,
        180_000,
        240_000,
        300_000,
    ]

    with (output / "funding.csv").open(newline="", encoding="utf-8") as handle:
        funding = list(csv.DictReader(handle))
    assert [int(row["timestamp_ms"]) for row in funding] == [210_000, 300_000]
    assert [float(row["rate"]) for row in funding] == [-0.001, 0.002]

    provenance = json.loads((output / "source-provenance.json").read_text(encoding="utf-8"))
    assert provenance["funding_required_from"] == "2020-01"
    assert provenance["months"][0]["funding_manifest"] is None
    assert provenance["legacy_exact_fingerprint_evidence"][
        "included_in_dataset_fingerprint"
    ] is False

    preflight = json.loads((output / "legacy-preflight.json").read_text(encoding="utf-8"))
    assert preflight["legacy_dataset_fingerprint_sha256"] == expected

    manifest = json.loads((output / "data-manifest.json").read_text(encoding="utf-8"))
    assert "legacy_preflight" not in manifest["files"]
    assert verify_manifest(manifest, directory=output) == []


def test_require_exact_legacy_match_blocks_before_output(tmp_path: Path, monkeypatch):
    _patch_two_tiny_months(monkeypatch)
    btc1_root, funding_root = _source_store(tmp_path)
    output = tmp_path / "blocked"

    with pytest.raises(RuntimeError, match="legacy dataset fingerprint does not match"):
        bootstrap.migrate_paramderive_market_data_with_funding_boundary(
            btc1_root=btc1_root,
            funding_root=funding_root,
            start="2019-12",
            funding_start="2020-01",
            end="2020-01",
            output_directory=output,
            legacy_fingerprint_reference="0" * 64,
            require_legacy_fingerprint_match=True,
        )
    assert not output.exists()
    assert not (tmp_path / ".blocked.importing").exists()


def test_raw_legacy_fingerprint_can_change_while_normalized_dataset_identity_stays_stable(
    tmp_path: Path, monkeypatch
):
    _patch_two_tiny_months(monkeypatch)
    btc1_root, funding_root = _source_store(
        tmp_path, accepted_at="2026-08-01T00:00:00Z"
    )
    first = bootstrap.migrate_paramderive_market_data_with_funding_boundary(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2019-12",
        funding_start="2020-01",
        end="2020-01",
        output_directory=tmp_path / "first",
    )

    for path in (
        btc1_root / "aralık" / "2019.json",
        btc1_root / "ocak" / "2020.json",
        funding_root / "ocak" / "2020.json",
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["accepted_at_utc"] = "2026-08-22T20:00:00Z"
        payload["sync_status"] = "SKIPPED"
        path.write_text(json.dumps(payload), encoding="utf-8")

    second = bootstrap.migrate_paramderive_market_data_with_funding_boundary(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start="2019-12",
        funding_start="2020-01",
        end="2020-01",
        output_directory=tmp_path / "second",
    )

    assert first["legacy_dataset_fingerprint_sha256"] != second[
        "legacy_dataset_fingerprint_sha256"
    ]
    assert first["dataset_fingerprint_sha256"] == second["dataset_fingerprint_sha256"]
    assert first["source_provenance_fingerprint_sha256"] == second[
        "source_provenance_fingerprint_sha256"
    ]
    assert first["legacy_preflight_sha256"] != second["legacy_preflight_sha256"]


def test_cli_funding_start_defaults_to_start_and_preflight_needs_no_output():
    parser = __import__(
        "parameter_extract.legacy_data_cli", fromlist=["build_parser"]
    ).build_parser()
    args = parser.parse_args(
        [
            "--btc1-root",
            "/btc1",
            "--funding-root",
            "/funding",
            "--start",
            "2020-01",
            "--end",
            "2020-02",
            "--preflight-only",
        ]
    )
    assert args.funding_start is None
    assert args.output_directory is None
    assert args.preflight_only is True
