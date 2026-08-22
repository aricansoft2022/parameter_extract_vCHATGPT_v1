from pathlib import Path

from parameter_extract.manifest import audit_candles, build_manifest, sha256_file, verify_manifest
from parameter_extract.models import Candle


def _candle(open_time_ms: int) -> Candle:
    return Candle(open_time_ms, open_time_ms + 59_999, 100.0, 101.0, 99.0, 100.5, 1.0)


def test_sha256_and_manifest_verification_detect_mutation(tmp_path: Path):
    candle_file = tmp_path / "BTCUSDT.csv"
    candle_file.write_text("hello\n", encoding="utf-8")
    candles = [_candle(0), _candle(60_000)]
    manifest = build_manifest(candle_path=candle_file, candles=candles, source="fixture")
    assert manifest["files"]["candles"]["sha256"] == sha256_file(candle_file)
    assert verify_manifest(manifest, directory=tmp_path) == []
    candle_file.write_text("changed\n", encoding="utf-8")
    problems = verify_manifest(manifest, directory=tmp_path)
    assert any("sha256 mismatch" in problem for problem in problems)


def test_audit_records_gaps_but_marks_duplicates_as_integrity_failure():
    healthy_with_gap = audit_candles([_candle(0), _candle(60_000), _candle(180_000)])
    assert healthy_with_gap.gap_count == 1
    assert healthy_with_gap.missing_minutes == 1
    assert healthy_with_gap.integrity_ok is True

    duplicate = audit_candles([_candle(0), _candle(0)])
    assert duplicate.duplicate_open_times == 1
    assert duplicate.integrity_ok is False


def test_expected_checksum_is_recorded_and_verified(tmp_path: Path):
    candle_file = tmp_path / "candles.csv"
    candle_file.write_bytes(b"abc")
    digest = sha256_file(candle_file)
    manifest = build_manifest(
        candle_path=candle_file,
        candles=[],
        candle_expected_sha256=digest,
    )
    assert manifest["files"]["candles"]["checksum_verified"] is True


def test_reads_binance_style_checksum_file(tmp_path: Path):
    from parameter_extract.manifest import read_checksum_file

    digest = "a" * 64
    checksum = tmp_path / "file.CHECKSUM"
    checksum.write_text(f"{digest}  file.zip\n", encoding="utf-8")
    assert read_checksum_file(checksum) == digest
