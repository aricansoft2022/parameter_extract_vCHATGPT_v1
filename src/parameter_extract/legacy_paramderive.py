from __future__ import annotations

import ast
import calendar
import csv
import hashlib
import json
import math
import shutil
import struct
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .manifest import identify_file, manifest_fingerprint, sha256_file, verify_manifest

SYMBOL = "BTCUSDT"
INTERVAL = "1m"
MINUTE_MS = 60_000
BTC1_MAGIC = b"BTC1"
BTC1_HEADER = struct.Struct("<4sI")
LEGACY_SOURCE_REPOSITORY = "aricansoft2022/backtest_vCHATGPT_v5.0"
LEGACY_SOURCE_COMMIT = "6ac4f2d03ffa7c583956869e5210139fb83ff5cb"
MONTH_NAMES_TR = (
    "ocak",
    "şubat",
    "mart",
    "nisan",
    "mayıs",
    "haziran",
    "temmuz",
    "ağustos",
    "eylül",
    "ekim",
    "kasım",
    "aralık",
)
NON_SEMANTIC_SOURCE_FIELDS = frozenset({"accepted_at_utc", "sync_status"})


@dataclass(frozen=True, order=True, slots=True)
class MonthKey:
    year: int
    month: int

    def __post_init__(self) -> None:
        if self.year < 1970 or not 1 <= self.month <= 12:
            raise ValueError("month must be YYYY-MM with year >= 1970")

    @property
    def text(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class ImportedMonth:
    month: str
    kline_binary_sha256: str
    kline_manifest_semantic_sha256: str
    kline_source_kind: str
    funding_data_sha256: str
    funding_manifest_semantic_sha256: str
    funding_source_kind: str
    candle_rows: int
    funding_rows: int
    kline_manifest: dict[str, Any]
    funding_manifest: dict[str, Any]


def parse_month(text: str) -> MonthKey:
    parts = text.strip().split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError("month must use YYYY-MM")
    try:
        return MonthKey(int(parts[0]), int(parts[1]))
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM") from exc


def iter_months(start: MonthKey, end: MonthKey) -> Iterable[MonthKey]:
    if end < start:
        raise ValueError("end month must not precede start month")
    current = start
    while current <= end:
        yield current
        if current.month == 12:
            current = MonthKey(current.year + 1, 1)
        else:
            current = MonthKey(current.year, current.month + 1)


def migrate_paramderive_market_data(
    *,
    btc1_root: str | Path,
    funding_root: str | Path,
    start: str,
    end: str,
    output_directory: str | Path,
) -> dict[str, Any]:
    """Migrate an already-ACCEPTED paramderive monthly store into bundle-ready CSV data.

    Source data is read-only. The complete output directory is built beside the destination
    and atomically renamed only after the generated data manifest verifies all pinned files.
    """
    start_month = parse_month(start)
    end_month = parse_month(end)
    months = tuple(iter_months(start_month, end_month))
    if not months:
        raise ValueError("migration requires at least one month")

    btc1_base = Path(btc1_root).expanduser().resolve()
    funding_base = Path(funding_root).expanduser().resolve()
    if not btc1_base.is_dir():
        raise ValueError(f"BTC1 root does not exist: {btc1_base}")
    if not funding_base.is_dir():
        raise ValueError(f"funding root does not exist: {funding_base}")

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

    imported: list[ImportedMonth] = []
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
                funding_data_path = funding_base / month_name / f"{month.year}.npz"
                funding_manifest_path = funding_base / month_name / f"{month.year}.json"

                kline_manifest = _load_json(btc1_manifest_path, label=f"BTC1 {month.text}")
                funding_manifest = _load_json(
                    funding_manifest_path, label=f"funding {month.text}"
                )
                _validate_kline_source_manifest(
                    kline_manifest, btc1_path=btc1_path, month=month
                )
                _validate_funding_source_manifest(
                    funding_manifest, data_path=funding_data_path, month=month
                )

                candle_rows, month_first, month_last = _write_btc1_month(
                    candle_writer, btc1_path, month
                )
                funding_rows, previous_funding_ms = _write_funding_month(
                    funding_writer,
                    funding_data_path,
                    month,
                    previous_global_timestamp=previous_funding_ms,
                )

                expected_rows = _expected_month_rows(month)
                if candle_rows != expected_rows:
                    raise ValueError(
                        f"BTC1 row count drift after decoding {month.text}: "
                        f"expected={expected_rows}, found={candle_rows}"
                    )
                if int(kline_manifest["rows"]) != candle_rows:
                    raise ValueError(f"BTC1 manifest row count drift: {month.text}")
                if int(funding_manifest["rows"]) != funding_rows:
                    raise ValueError(f"funding manifest row count drift: {month.text}")

                if first_candle_ms is None:
                    first_candle_ms = month_first
                if last_candle_ms is not None and month_first != last_candle_ms + MINUTE_MS:
                    raise ValueError(
                        f"selected BTC1 months are not contiguous at {month.text}"
                    )
                last_candle_ms = month_last
                total_candles += candle_rows
                total_funding += funding_rows

                normalized_kline = _semantic_source_manifest(kline_manifest)
                normalized_funding = _semantic_source_manifest(funding_manifest)
                imported.append(
                    ImportedMonth(
                        month=month.text,
                        kline_binary_sha256=sha256_file(btc1_path),
                        kline_manifest_semantic_sha256=_canonical_sha256(normalized_kline),
                        kline_source_kind=str(kline_manifest["source_kind"]),
                        funding_data_sha256=sha256_file(funding_data_path),
                        funding_manifest_semantic_sha256=_canonical_sha256(normalized_funding),
                        funding_source_kind=str(funding_manifest["source_kind"]),
                        candle_rows=candle_rows,
                        funding_rows=funding_rows,
                        kline_manifest=normalized_kline,
                        funding_manifest=normalized_funding,
                    )
                )

        provenance: dict[str, Any] = {
            "schema_version": 1,
            "kind": "parameter_extract.paramderive_source_provenance",
            "source_repository": LEGACY_SOURCE_REPOSITORY,
            "source_reader_commit": LEGACY_SOURCE_COMMIT,
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "start_month": start_month.text,
            "end_month": end_month.text,
            "source_manifest_normalization": {
                "excluded_fields": sorted(NON_SEMANTIC_SOURCE_FIELDS),
                "reason": (
                    "accepted_at_utc and sync_status are operational metadata; the semantic "
                    "source identity is pinned by accepted verification metadata plus data hashes"
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
            },
            "months": [asdict(row) for row in imported],
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

        if first_candle_ms is None or last_candle_ms is None:
            raise RuntimeError("migration wrote no candles")
        expected_total = sum(_expected_month_rows(month) for month in months)
        if total_candles != expected_total:
            raise RuntimeError(
                f"migrated candle total differs: expected={expected_total}, found={total_candles}"
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
            raise RuntimeError("generated migration manifest failed verification: " + "; ".join(problems))

        temporary.replace(output)
        return {
            "schema_version": 1,
            "kind": "parameter_extract.paramderive_migration_result",
            "output_directory": str(output),
            "start_month": start_month.text,
            "end_month": end_month.text,
            "candle_rows": total_candles,
            "funding_rows": total_funding,
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


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} manifest is not an object")
    return payload


def _validate_kline_source_manifest(
    manifest: dict[str, Any], *, btc1_path: Path, month: MonthKey
) -> None:
    required = {
        "status",
        "symbol",
        "interval",
        "year",
        "month",
        "rows",
        "first_open_time_ms",
        "last_open_time_ms",
        "source_kind",
        "rest_verification",
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
    source_kind = manifest["source_kind"]
    if not isinstance(source_kind, str) or not source_kind.startswith("BINANCE_"):
        raise ValueError(f"BTC1 source is not Binance: {month.text}")
    if not isinstance(manifest["rest_verification"], dict):
        raise ValueError(f"BTC1 REST verification metadata missing: {month.text}")
    if not btc1_path.is_file():
        raise ValueError(f"BTC1 binary not found: {btc1_path}")
    expected_sha = str(manifest["btc1_sha256"]).lower()
    _validate_digest(expected_sha, label=f"BTC1 sha {month.text}")
    if sha256_file(btc1_path) != expected_sha:
        raise ValueError(f"BTC1 SHA-256 mismatch: {month.text}")
    archives = manifest.get("archives", [])
    if not isinstance(archives, list):
        raise ValueError(f"BTC1 archives metadata invalid: {month.text}")
    for artifact in archives:
        if not isinstance(artifact, dict):
            raise ValueError(f"BTC1 archive metadata invalid: {month.text}")
        official = str(artifact.get("official_sha256", "")).lower()
        actual = str(artifact.get("actual_sha256", "")).lower()
        _validate_digest(official, label=f"BTC1 archive checksum {month.text}")
        _validate_digest(actual, label=f"BTC1 archive actual sha {month.text}")
        if official != actual:
            raise ValueError(f"BTC1 archive checksum evidence disagrees: {month.text}")


def _validate_funding_source_manifest(
    manifest: dict[str, Any], *, data_path: Path, month: MonthKey
) -> None:
    required = {
        "status",
        "symbol",
        "year",
        "month",
        "rows",
        "first_timestamp_ms",
        "last_timestamp_ms",
        "source_kind",
        "data_sha256",
    }
    missing = required - manifest.keys()
    if missing:
        raise ValueError(f"funding manifest missing {sorted(missing)}: {month.text}")
    if manifest["status"] != "ACCEPTED" or manifest["symbol"] != SYMBOL:
        raise ValueError(f"funding month is not accepted BTCUSDT data: {month.text}")
    if int(manifest["year"]) != month.year or int(manifest["month"]) != month.month:
        raise ValueError(f"funding manifest month mismatch: {month.text}")
    source_kind = manifest["source_kind"]
    if not isinstance(source_kind, str) or not source_kind.startswith("BINANCE_"):
        raise ValueError(f"funding source is not Binance: {month.text}")
    if not data_path.is_file():
        raise ValueError(f"funding NPZ not found: {data_path}")
    expected_sha = str(manifest["data_sha256"]).lower()
    _validate_digest(expected_sha, label=f"funding sha {month.text}")
    if sha256_file(data_path) != expected_sha:
        raise ValueError(f"funding SHA-256 mismatch: {month.text}")
    official = manifest.get("official_sha256")
    actual = manifest.get("actual_sha256")
    if official is not None or actual is not None:
        if not isinstance(official, str) or not isinstance(actual, str):
            raise ValueError(f"funding archive checksum metadata incomplete: {month.text}")
        official = official.lower()
        actual = actual.lower()
        _validate_digest(official, label=f"funding archive checksum {month.text}")
        _validate_digest(actual, label=f"funding archive actual sha {month.text}")
        if official != actual:
            raise ValueError(f"funding archive checksum evidence disagrees: {month.text}")
    elif not isinstance(manifest.get("rest_verification"), dict):
        raise ValueError(f"funding source lacks archive or REST verification evidence: {month.text}")


def _write_btc1_month(writer: Any, path: Path, month: MonthKey) -> tuple[int, int, int]:
    payload = path.read_bytes()
    if len(payload) < BTC1_HEADER.size:
        raise ValueError(f"truncated BTC1 file: {month.text}")
    magic, count = BTC1_HEADER.unpack_from(payload, 0)
    if magic != BTC1_MAGIC:
        raise ValueError(f"invalid BTC1 magic: {month.text}")
    expected_rows = _expected_month_rows(month)
    if count != expected_rows:
        raise ValueError(
            f"BTC1 binary is not a complete UTC month {month.text}: "
            f"expected={expected_rows}, found={count}"
        )
    expected_size = BTC1_HEADER.size + count * 40
    if len(payload) != expected_size:
        raise ValueError(
            f"BTC1 size mismatch {month.text}: expected={expected_size}, found={len(payload)}"
        )

    timestamp_offset = BTC1_HEADER.size
    open_offset = timestamp_offset + count * 8
    high_offset = open_offset + count * 8
    low_offset = high_offset + count * 8
    close_offset = low_offset + count * 8
    start_ms, _ = _month_bounds_ms(month)

    for index in range(count):
        timestamp = struct.unpack_from("<Q", payload, timestamp_offset + index * 8)[0]
        expected_timestamp = start_ms + index * MINUTE_MS
        if timestamp != expected_timestamp:
            raise ValueError(
                f"BTC1 timestamp drift {month.text} row={index}: "
                f"expected={expected_timestamp}, found={timestamp}"
            )
        open_value = struct.unpack_from("<d", payload, open_offset + index * 8)[0]
        high_value = struct.unpack_from("<d", payload, high_offset + index * 8)[0]
        low_value = struct.unpack_from("<d", payload, low_offset + index * 8)[0]
        close_value = struct.unpack_from("<d", payload, close_offset + index * 8)[0]
        if not _valid_ohlc(open_value, high_value, low_value, close_value):
            raise ValueError(f"BTC1 invalid OHLC {month.text} row={index}")
        writer.writerow(
            [
                timestamp,
                repr(open_value),
                repr(high_value),
                repr(low_value),
                repr(close_value),
                "0",
                timestamp + MINUTE_MS - 1,
            ]
        )
    return count, start_ms, start_ms + (count - 1) * MINUTE_MS


def _write_funding_month(
    writer: Any,
    path: Path,
    month: MonthKey,
    *,
    previous_global_timestamp: int | None,
) -> tuple[int, int | None]:
    timestamps, rates = _read_funding_npz(path)
    if len(timestamps) != len(rates):
        raise ValueError(f"funding arrays differ in length: {month.text}")
    start_ms, end_ms = _month_bounds_ms(month)
    previous = previous_global_timestamp
    for index, (timestamp, rate) in enumerate(zip(timestamps, rates, strict=True)):
        if not start_ms <= timestamp < end_ms:
            raise ValueError(f"funding timestamp outside month {month.text} row={index}")
        if previous is not None and timestamp <= previous:
            raise ValueError(f"funding timestamps are not globally increasing: {month.text}")
        if not math.isfinite(rate):
            raise ValueError(f"funding contains non-finite rate: {month.text} row={index}")
        writer.writerow([timestamp, repr(rate), ""])
        previous = timestamp
    return len(timestamps), previous


def _read_funding_npz(path: Path) -> tuple[list[int], list[float]]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if "timestamps_ms.npy" not in names or "rates.npy" not in names:
                raise ValueError(f"funding NPZ missing expected arrays: {path}")
            timestamps = _read_npy_1d(archive.read("timestamps_ms.npy"), expected_kind="i")
            rates = _read_npy_1d(archive.read("rates.npy"), expected_kind="f")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid funding NPZ: {path}") from exc
    return [int(value) for value in timestamps], [float(value) for value in rates]


def _read_npy_1d(payload: bytes, *, expected_kind: str) -> list[int | float]:
    if len(payload) < 10 or payload[:6] != b"\x93NUMPY":
        raise ValueError("invalid NPY magic")
    major = payload[6]
    if major == 1:
        header_length_size = 2
        header_length = struct.unpack_from("<H", payload, 8)[0]
        header_start = 10
    elif major in {2, 3}:
        header_length_size = 4
        header_length = struct.unpack_from("<I", payload, 8)[0]
        header_start = 12
    else:
        raise ValueError(f"unsupported NPY version: {major}.{payload[7]}")
    if header_start != 8 + header_length_size:
        raise AssertionError("NPY header offset invariant failed")
    header_end = header_start + header_length
    if header_end > len(payload):
        raise ValueError("truncated NPY header")
    try:
        header = ast.literal_eval(payload[header_start:header_end].decode("latin1").strip())
    except (SyntaxError, ValueError) as exc:
        raise ValueError("invalid NPY header") from exc
    if not isinstance(header, dict):
        raise ValueError("NPY header is not a dict")
    if header.get("fortran_order") is not False:
        raise ValueError("Fortran-order funding arrays are unsupported")
    shape = header.get("shape")
    if not isinstance(shape, tuple) or len(shape) != 1 or not isinstance(shape[0], int):
        raise ValueError("funding NPY must be one-dimensional")
    count = shape[0]
    descr = header.get("descr")
    allowed = {f"<{expected_kind}8", f"={expected_kind}8", f"|{expected_kind}8"}
    if descr not in allowed:
        raise ValueError(f"unexpected funding NPY dtype: {descr}")
    body = payload[header_end:]
    if len(body) != count * 8:
        raise ValueError("funding NPY byte length does not match shape")
    fmt = "<q" if expected_kind == "i" else "<d"
    return [item[0] for item in struct.iter_unpack(fmt, body)]


def _semantic_source_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in NON_SEMANTIC_SOURCE_FIELDS
    }


def _canonical_sha256(payload: dict[str, Any]) -> str:
    stable = dict(payload)
    stable.pop("source_provenance_fingerprint_sha256", None)
    encoded = json.dumps(
        stable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _month_bounds_ms(month: MonthKey) -> tuple[int, int]:
    start = datetime(month.year, month.month, 1, tzinfo=timezone.utc)
    if month.month == 12:
        end = datetime(month.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(month.year, month.month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _expected_month_rows(month: MonthKey) -> int:
    return calendar.monthrange(month.year, month.month)[1] * 24 * 60


def _valid_ohlc(open_value: float, high: float, low: float, close: float) -> bool:
    return (
        all(math.isfinite(value) for value in (open_value, high, low, close))
        and low > 0.0
        and high >= low
        and low <= open_value <= high
        and low <= close <= high
    )


def _validate_digest(value: str, *, label: str) -> None:
    if len(value) != 64:
        raise ValueError(f"{label} must be a 64-character SHA-256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be hexadecimal") from exc
