from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .models import Candle
from .signals import ONE_MINUTE_MS

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    size_bytes: int
    sha256: str
    expected_sha256: str | None = None
    checksum_verified: bool | None = None


@dataclass(frozen=True, slots=True)
class CandleAudit:
    rows: int
    start_open_time_ms: int | None
    end_open_time_ms: int | None
    duplicate_open_times: int
    backward_or_unsorted_steps: int
    gap_count: int
    missing_minutes: int
    integrity_ok: bool


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def identify_file(path: str | Path, *, expected_sha256: str | None = None) -> FileIdentity:
    file_path = Path(path)
    actual = sha256_file(file_path)
    expected = None if expected_sha256 is None else expected_sha256.strip().lower()
    if expected is not None and len(expected) != 64:
        raise ValueError("expected SHA-256 must contain exactly 64 hex characters")
    if expected is not None:
        try:
            int(expected, 16)
        except ValueError as exc:
            raise ValueError("expected SHA-256 is not hexadecimal") from exc
    return FileIdentity(
        path=file_path.name,
        size_bytes=file_path.stat().st_size,
        sha256=actual,
        expected_sha256=expected,
        checksum_verified=None if expected is None else actual == expected,
    )


def audit_candles(candles: Iterable[Candle]) -> CandleAudit:
    rows = list(candles)
    duplicates = 0
    backwards = 0
    gaps = 0
    missing = 0
    for previous, current in zip(rows, rows[1:]):
        delta = current.open_time_ms - previous.open_time_ms
        if delta == 0:
            duplicates += 1
        elif delta < 0:
            backwards += 1
        elif delta > ONE_MINUTE_MS:
            gaps += 1
            missing += max(0, (delta // ONE_MINUTE_MS) - 1)
        elif delta < ONE_MINUTE_MS:
            backwards += 1
    return CandleAudit(
        rows=len(rows),
        start_open_time_ms=None if not rows else rows[0].open_time_ms,
        end_open_time_ms=None if not rows else rows[-1].open_time_ms,
        duplicate_open_times=duplicates,
        backward_or_unsorted_steps=backwards,
        gap_count=gaps,
        missing_minutes=missing,
        integrity_ok=(duplicates == 0 and backwards == 0),
    )


def build_manifest(
    *,
    candle_path: str | Path,
    candles: Iterable[Candle],
    funding_path: str | Path | None = None,
    candle_expected_sha256: str | None = None,
    funding_expected_sha256: str | None = None,
    source: str | None = None,
) -> dict[str, object]:
    files = {
        "candles": asdict(identify_file(candle_path, expected_sha256=candle_expected_sha256)),
        "funding": None,
    }
    if funding_path is not None:
        files["funding"] = asdict(
            identify_file(funding_path, expected_sha256=funding_expected_sha256)
        )
    audit = asdict(audit_candles(candles))
    stable = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": "parameter_extract.data_manifest",
        "source": source,
        "files": files,
        "candles": audit,
    }
    fingerprint_payload = json.dumps(
        stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {
        **stable,
        "dataset_fingerprint_sha256": hashlib.sha256(fingerprint_payload).hexdigest(),
    }


def verify_manifest(manifest: dict[str, object], *, directory: str | Path) -> list[str]:
    root = Path(directory)
    problems: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, dict):
        return ["manifest files section is missing or invalid"]
    for label in ("candles", "funding"):
        record = files.get(label)
        if record is None:
            continue
        if not isinstance(record, dict):
            problems.append(f"{label}: invalid file record")
            continue
        name = record.get("path")
        expected = record.get("sha256")
        if not isinstance(name, str) or not isinstance(expected, str):
            problems.append(f"{label}: missing path or sha256")
            continue
        path = root / name
        if not path.exists():
            problems.append(f"{label}: file not found: {name}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            problems.append(f"{label}: sha256 mismatch: expected {expected}, got {actual}")
        if path.stat().st_size != record.get("size_bytes"):
            problems.append(f"{label}: size mismatch")
    return problems


def read_checksum_file(path: str | Path) -> str:
    """Read the SHA-256 token from Binance-style `.CHECKSUM` text."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("checksum file is empty")
    token = text.split()[0].lower()
    if len(token) != 64:
        raise ValueError("checksum file does not start with a SHA-256 digest")
    try:
        int(token, 16)
    except ValueError as exc:
        raise ValueError("checksum file digest is not hexadecimal") from exc
    return token
