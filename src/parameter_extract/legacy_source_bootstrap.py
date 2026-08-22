from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .legacy_paramderive import (
    INTERVAL,
    LEGACY_SOURCE_COMMIT,
    LEGACY_SOURCE_REPOSITORY,
    MINUTE_MS,
    MONTH_NAMES_TR,
    NON_SEMANTIC_SOURCE_FIELDS,
    SYMBOL,
    _canonical_sha256,
    _expected_month_rows,
    _load_json,
    _semantic_source_manifest,
    _validate_digest,
    _validate_funding_source_manifest,
    _validate_kline_source_manifest,
    _write_btc1_month,
    _write_funding_month,
    iter_months,
    parse_month,
)
from .manifest import identify_file, manifest_fingerprint, sha256_file, verify_manifest

CANONICAL_2026_08_07_CANDLE_START = "2019-12"
CANONICAL_2026_08_07_FUNDING_START = "2020-01"
CANONICAL_2026_08_07_END = "2026-07"
CANONICAL_2026_08_07_CANDLE_ROWS = 3_506_400
CANONICAL_2026_08_07_LEGACY_DATA_FINGERPRINT = (
    "19e566d197f1266094faed171c6ee4936b822b3d5f061e8b405604b8aff5021c"
)


def preflight_paramderive_source(
    *,
    btc1_root: str | Path,
    funding_root: str | Path,
    start: str,
    funding_start: str,
    end: str,
    legacy_fingerprint_reference: str | None = None,
) -> dict[str, Any]:
    """Validate an ACCEPTED legacy source store without writing migrated data.

    The returned legacy fingerprint reproduces the exact `prepare.dataset_fingerprint()`
    algorithm from the pinned legacy reader commit: raw BTC1 monthly manifest bytes,
    BTC1 binary SHA-256 strings, and raw funding manifest bytes starting at the explicit
    funding boundary.
    """
    start_month = parse_month(start)
    funding_start_month = parse_month(funding_start)
    end_month = parse_month(end)
    if funding_start_month < start_month or funding_start_month > end_month:
        raise ValueError("funding_start must fall inside the selected candle range")

    btc1_base = Path(btc1_root).expanduser().resolve()
    funding_base = Path(funding_root).expanduser().resolve()
    if not btc1_base.is_dir():
        raise ValueError(f"BTC1 root does not exist: {btc1_base}")
    if not funding_base.is_dir():
        raise ValueError(f"funding root does not exist: {funding_base}")

    reference = None
    if legacy_fingerprint_reference is not None:
        reference = legacy_fingerprint_reference.strip().lower()
        _validate_digest(reference, label="legacy fingerprint reference")

    digest = hashlib.sha256()
    digest.update(f"funding-required-from:{funding_start_month.text}\n".encode())

    candle_months = 0
    funding_months = 0
    expected_candle_rows = 0
    source_rows: list[dict[str, Any]] = []
    for month in iter_months(start_month, end_month):
        month_name = MONTH_NAMES_TR[month.month - 1]
        btc1_path = btc1_base / month_name / f"{month.year}.bin"
        btc1_manifest_path = btc1_base / month_name / f"{month.year}.json"
        kline_manifest = _load_json(btc1_manifest_path, label=f"BTC1 {month.text}")
        _validate_kline_source_manifest(kline_manifest, btc1_path=btc1_path, month=month)

        btc1_sha = sha256_file(btc1_path)
        digest.update(btc1_manifest_path.read_bytes())
        digest.update(btc1_sha.encode())
        candle_months += 1
        rows = _expected_month_rows(month)
        expected_candle_rows += rows

        row: dict[str, Any] = {
            "month": month.text,
            "candle_rows": rows,
            "kline_binary_sha256": btc1_sha,
            "kline_manifest_semantic_sha256": _canonical_sha256(
                _semantic_source_manifest(kline_manifest)
            ),
            "kline_source_kind": str(kline_manifest["source_kind"]),
            "funding_required": month >= funding_start_month,
        }

        if month >= funding_start_month:
            funding_data_path = funding_base / month_name / f"{month.year}.npz"
            funding_manifest_path = funding_base / month_name / f"{month.year}.json"
            funding_manifest = _load_json(
                funding_manifest_path, label=f"funding {month.text}"
            )
            _validate_funding_source_manifest(
                funding_manifest, data_path=funding_data_path, month=month
            )
            digest.update(funding_manifest_path.read_bytes())
            funding_months += 1
            row.update(
                {
                    "funding_rows": int(funding_manifest["rows"]),
                    "funding_data_sha256": sha256_file(funding_data_path),
                    "funding_manifest_semantic_sha256": _canonical_sha256(
                        _semantic_source_manifest(funding_manifest)
                    ),
                    "funding_source_kind": str(funding_manifest["source_kind"]),
                }
            )
        else:
            row.update(
                {
                    "funding_rows": 0,
                    "funding_data_sha256": None,
                    "funding_manifest_semantic_sha256": None,
                    "funding_source_kind": None,
                }
            )
        source_rows.append(row)

    legacy_fp = digest.hexdigest()
    matches_reference = None if reference is None else legacy_fp == reference
    return {
        "schema_version": 1,
        "kind": "parameter_extract.paramderive_source_preflight",
        "source_repository": LEGACY_SOURCE_REPOSITORY,
        "source_reader_commit": LEGACY_SOURCE_COMMIT,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "start_month": start_month.text,
        "funding_required_from": funding_start_month.text,
        "end_month": end_month.text,
        "candle_months": candle_months,
        "funding_months": funding_months,
        "expected_candle_rows": expected_candle_rows,
        "legacy_dataset_fingerprint_sha256": legacy_fp,
        "legacy_fingerprint_reference_sha256": reference,
        "legacy_fingerprint_matches_reference": matches_reference,
        "source_validation_passed": True,
        "months": source_rows,
    }


def migrate_paramderive_market_data_with_funding_boundary(
    *,
    btc1_root: str | Path,
    funding_root: str | Path,
    start: str,
    funding_start: str,
    end: str,
    output_directory: str | Path,
    legacy_fingerprint_reference: str | None = None,
    require_legacy_fingerprint_match: bool = False,
) -> dict[str, Any]:
    """Migrate accepted candles plus a later funding-required boundary.

    The source store remains read-only. The output directory is atomically published only
    after the generated data manifest verifies all output files, including pinned source
    provenance.
    """
    preflight = preflight_paramderive_source(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start=start,
        funding_start=funding_start,
        end=end,
        legacy_fingerprint_reference=legacy_fingerprint_reference,
    )
    if require_legacy_fingerprint_match:
        if preflight["legacy_fingerprint_reference_sha256"] is None:
            raise ValueError(
                "require_legacy_fingerprint_match needs legacy_fingerprint_reference"
            )
        if preflight["legacy_fingerprint_matches_reference"] is not True:
            raise RuntimeError(
                "legacy dataset fingerprint does not match the required reference: "
                f"expected={preflight['legacy_fingerprint_reference_sha256']} "
                f"actual={preflight['legacy_dataset_fingerprint_sha256']}"
            )

    start_month = parse_month(start)
    funding_start_month = parse_month(funding_start)
    end_month = parse_month(end)
    months = tuple(iter_months(start_month, end_month))
    btc1_base = Path(btc1_root).expanduser().resolve()
    funding_base = Path(funding_root).expanduser().resolve()

    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"migration output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.importing"
    if temporary.exists():
        raise FileExistsError(f"migration temporary directory already exists: {temporary}")
    temporary.mkdir()

    candle_path = temporary / "candles.csv"
    funding_path = temporary / "funding.csv"
    provenance_path = temporary / "source-provenance.json"
    manifest_path = temporary / "data-manifest.json"

    imported: list[dict[str, Any]] = []
    total_candles = 0
    total_funding = 0
    first_candle_ms: int | None = None
    last_candle_ms: int | None = None
    previous_funding_ms: int | None = None

    try:
        with candle_path.open("w", encoding="utf-8", newline="") as candle_handle, funding_path.open(
            "w", encoding="utf-8", newline=""
        ) as funding_handle:
            candle_writer = csv.writer(candle_handle, lineterminator="\n")
            funding_writer = csv.writer(funding_handle, lineterminator="\n")
            candle_writer.writerow(
                ["open_time", "open", "high", "low", "close", "volume", "close_time"]
            )
            funding_writer.writerow(["timestamp_ms", "rate", "mark_price"])

            for month in months:
                month_name = MONTH_NAMES_TR[month.month - 1]
                btc1_path = btc1_base / month_name / f"{month.year}.bin"
                btc1_manifest_path = btc1_base / month_name / f"{month.year}.json"
                kline_manifest = _load_json(btc1_manifest_path, label=f"BTC1 {month.text}")
                _validate_kline_source_manifest(
                    kline_manifest, btc1_path=btc1_path, month=month
                )
                candle_rows, month_first, month_last = _write_btc1_month(
                    candle_writer, btc1_path, month
                )
                if first_candle_ms is None:
                    first_candle_ms = month_first
                if last_candle_ms is not None and month_first != last_candle_ms + MINUTE_MS:
                    raise ValueError(
                        f"selected BTC1 months are not contiguous at {month.text}"
                    )
                last_candle_ms = month_last
                total_candles += candle_rows

                funding_manifest: dict[str, Any] | None = None
                funding_rows = 0
                funding_data_sha: str | None = None
                funding_semantic_sha: str | None = None
                funding_source_kind: str | None = None
                if month >= funding_start_month:
                    funding_data_path = funding_base / month_name / f"{month.year}.npz"
                    funding_manifest_path = funding_base / month_name / f"{month.year}.json"
                    funding_manifest = _load_json(
                        funding_manifest_path, label=f"funding {month.text}"
                    )
                    _validate_funding_source_manifest(
                        funding_manifest, data_path=funding_data_path, month=month
                    )
                    funding_rows, previous_funding_ms = _write_funding_month(
                        funding_writer,
                        funding_data_path,
                        month,
                        previous_global_timestamp=previous_funding_ms,
                    )
                    if int(funding_manifest["rows"]) != funding_rows:
                        raise ValueError(f"funding manifest row count drift: {month.text}")
                    total_funding += funding_rows
                    funding_data_sha = sha256_file(funding_data_path)
                    normalized_funding = _semantic_source_manifest(funding_manifest)
                    funding_semantic_sha = _canonical_sha256(normalized_funding)
                    funding_source_kind = str(funding_manifest["source_kind"])
                else:
                    normalized_funding = None

                normalized_kline = _semantic_source_manifest(kline_manifest)
                imported.append(
                    {
                        "month": month.text,
                        "funding_required": month >= funding_start_month,
                        "kline_binary_sha256": sha256_file(btc1_path),
                        "kline_manifest_semantic_sha256": _canonical_sha256(normalized_kline),
                        "kline_source_kind": str(kline_manifest["source_kind"]),
                        "funding_data_sha256": funding_data_sha,
                        "funding_manifest_semantic_sha256": funding_semantic_sha,
                        "funding_source_kind": funding_source_kind,
                        "candle_rows": candle_rows,
                        "funding_rows": funding_rows,
                        "kline_manifest": normalized_kline,
                        "funding_manifest": normalized_funding,
                    }
                )

        if first_candle_ms is None or last_candle_ms is None:
            raise RuntimeError("migration wrote no candles")
        if total_candles != preflight["expected_candle_rows"]:
            raise RuntimeError(
                "migrated candle total differs from preflight: "
                f"expected={preflight['expected_candle_rows']}, found={total_candles}"
            )

        provenance: dict[str, Any] = {
            "schema_version": 2,
            "kind": "parameter_extract.paramderive_source_provenance",
            "source_repository": LEGACY_SOURCE_REPOSITORY,
            "source_reader_commit": LEGACY_SOURCE_COMMIT,
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "start_month": start_month.text,
            "funding_required_from": funding_start_month.text,
            "end_month": end_month.text,
            "legacy_dataset_fingerprint_sha256": preflight[
                "legacy_dataset_fingerprint_sha256"
            ],
            "legacy_fingerprint_reference_sha256": preflight[
                "legacy_fingerprint_reference_sha256"
            ],
            "legacy_fingerprint_matches_reference": preflight[
                "legacy_fingerprint_matches_reference"
            ],
            "legacy_fingerprint_algorithm": (
                "backtest_vCHATGPT_v5.0 prepare.dataset_fingerprint: raw monthly BTC1 "
                "manifest bytes + BTC1 SHA strings + funding manifest bytes at/after "
                "funding_required_from"
            ),
            "source_manifest_normalization": {
                "excluded_fields": sorted(NON_SEMANTIC_SOURCE_FIELDS),
                "reason": (
                    "accepted_at_utc and sync_status are operational metadata; the new "
                    "parameter_extract source identity is pinned by accepted verification "
                    "metadata plus source data hashes"
                ),
            },
            "migration_policies": {
                "candle_ohlc": "lossless IEEE-754 float64 values from BTC1",
                "candle_close_time": "open_time_ms + 59999",
                "volume": "0 placeholder; unavailable in BTC1 and unused by parameter_extract",
                "funding_rate": "raw stored Binance funding rate; no legacy multiplier/filter applied",
                "funding_mark_price": (
                    "blank; unavailable in legacy NPZ, so parameter_extract uses its documented "
                    "enclosing-candle-close approximation"
                ),
                "funding_before_required_from": "not exported; candle warm-up may begin earlier",
            },
            "months": imported,
        }
        provenance["source_provenance_fingerprint_sha256"] = _canonical_sha256(provenance)
        provenance_path.write_text(
            json.dumps(
                provenance,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

        files = {
            "candles": asdict(identify_file(candle_path)),
            "funding": asdict(identify_file(funding_path)),
            "source_provenance": asdict(identify_file(provenance_path)),
        }
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "kind": "parameter_extract.data_manifest",
            "source": f"paramderive-accepted-binance@{LEGACY_SOURCE_COMMIT}",
            "files": files,
            "candles": {
                "rows": total_candles,
                "start_open_time_ms": first_candle_ms,
                "end_open_time_ms": last_candle_ms,
                "duplicate_open_times": 0,
                "backward_or_unsorted_steps": 0,
                "gap_count": 0,
                "missing_minutes": 0,
                "integrity_ok": True,
            },
        }
        manifest["dataset_fingerprint_sha256"] = manifest_fingerprint(manifest)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        problems = verify_manifest(manifest, directory=temporary)
        if problems:
            raise RuntimeError(
                "generated migration manifest failed verification: " + "; ".join(problems)
            )

        temporary.replace(output)
        return {
            "schema_version": 2,
            "kind": "parameter_extract.paramderive_migration_result",
            "output_directory": str(output),
            "start_month": start_month.text,
            "funding_required_from": funding_start_month.text,
            "end_month": end_month.text,
            "candle_months": preflight["candle_months"],
            "funding_months": preflight["funding_months"],
            "candle_rows": total_candles,
            "funding_rows": total_funding,
            "legacy_dataset_fingerprint_sha256": preflight[
                "legacy_dataset_fingerprint_sha256"
            ],
            "legacy_fingerprint_reference_sha256": preflight[
                "legacy_fingerprint_reference_sha256"
            ],
            "legacy_fingerprint_matches_reference": preflight[
                "legacy_fingerprint_matches_reference"
            ],
            "dataset_fingerprint_sha256": manifest["dataset_fingerprint_sha256"],
            "source_provenance_fingerprint_sha256": provenance[
                "source_provenance_fingerprint_sha256"
            ],
            "source_reader_commit": LEGACY_SOURCE_COMMIT,
            "files": {
                "candles": "candles.csv",
                "funding": "funding.csv",
                "source_provenance": "source-provenance.json",
                "manifest": "data-manifest.json",
            },
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def canonical_2026_08_07_preflight(
    *, btc1_root: str | Path, funding_root: str | Path
) -> dict[str, Any]:
    result = preflight_paramderive_source(
        btc1_root=btc1_root,
        funding_root=funding_root,
        start=CANONICAL_2026_08_07_CANDLE_START,
        funding_start=CANONICAL_2026_08_07_FUNDING_START,
        end=CANONICAL_2026_08_07_END,
        legacy_fingerprint_reference=CANONICAL_2026_08_07_LEGACY_DATA_FINGERPRINT,
    )
    result["canonical_reference"] = "backtest_vCHATGPT_v5.0 final-2026-08-07"
    result["canonical_expected_candle_rows"] = CANONICAL_2026_08_07_CANDLE_ROWS
    result["canonical_coverage_matches"] = (
        result["candle_months"] == 80
        and result["funding_months"] == 79
        and result["expected_candle_rows"] == CANONICAL_2026_08_07_CANDLE_ROWS
    )
    return result
