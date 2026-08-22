import json
from pathlib import Path

import pytest

from parameter_extract.study import load_study_json, study_fingerprint


def _payload():
    return {
        "schema_version": 1,
        "name": "BTC walk forward",
        "symbol": "BTCUSDT",
        "dataset_manifest": "data-manifest.json",
        "dataset_fingerprint_sha256": "a" * 64,
        "execution": {
            "name": "expected_live",
            "entry_timing": "next_open",
            "taker_fee_bps": 4.0,
            "buy_slippage_bps": 2.0,
            "sell_slippage_bps": 2.0
        },
        "windows": {
            "discovery": [{"name": "d1", "start_ms": 1000000, "end_ms": 2000000}],
            "validation": [{"name": "v1", "start_ms": 2000000, "end_ms": 3000000}],
            "holdout": [{"name": "h1", "start_ms": 3000000, "end_ms": 4000000}]
        },
        "warmup_candles": 300,
        "min_trades": 30
    }


def test_study_contract_loads_and_fingerprint_is_stable(tmp_path: Path):
    path = tmp_path / "study.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    first = load_study_json(path)
    second = load_study_json(path)
    assert first.warmup_candles == 300
    assert study_fingerprint(first) == study_fingerprint(second)
    assert len(study_fingerprint(first)) == 64


def test_study_rejects_overlapping_windows(tmp_path: Path):
    payload = _payload()
    payload["windows"]["validation"][0]["start_ms"] = 1500000
    path = tmp_path / "study.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="overlap"):
        load_study_json(path)


def test_study_requires_explicit_execution_numbers(tmp_path: Path):
    payload = _payload()
    del payload["execution"]["taker_fee_bps"]
    path = tmp_path / "study.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TypeError):
        load_study_json(path)
