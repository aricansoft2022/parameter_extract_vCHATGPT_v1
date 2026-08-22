import json
import math
from pathlib import Path

import pytest

from parameter_extract.manifest import build_manifest
from parameter_extract.models import Candle
from parameter_extract.study import load_study_json, run_study, study_fingerprint


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


def test_study_runner_withholds_holdout_until_explicitly_revealed(tmp_path: Path):
    base = 1_700_000_000_000
    candles: list[Candle] = []
    rows: list[str] = []
    for index in range(540):
        close = 100.0 + 4.0 * math.sin(index / 4.2) + 0.015 * index
        open_value = 100.0 + 4.0 * math.sin((index - 0.6) / 4.2) + 0.015 * (index - 0.6)
        spread = 0.35 + 0.003 * index
        candle = Candle(
            open_time_ms=base + index * 60_000,
            close_time_ms=base + index * 60_000 + 59_999,
            open=open_value,
            high=max(open_value, close) + spread,
            low=min(open_value, close) - spread,
            close=close,
            volume=100.0 + index,
        )
        candles.append(candle)
        rows.append(
            ",".join(
                str(value)
                for value in (
                    candle.open_time_ms,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.close_time_ms,
                )
            )
        )

    candle_path = tmp_path / "BTCUSDT.csv"
    candle_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    manifest = build_manifest(candle_path=candle_path, candles=candles, source="test fixture")
    manifest_path = tmp_path / "data-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    study = {
        "schema_version": 1,
        "name": "holdout gate",
        "symbol": "BTCUSDT",
        "dataset_manifest": manifest_path.name,
        "dataset_fingerprint_sha256": manifest["dataset_fingerprint_sha256"],
        "execution": {
            "name": "expected_live",
            "entry_timing": "next_open",
            "taker_fee_bps": 4.0,
            "buy_slippage_bps": 2.0,
            "sell_slippage_bps": 2.0
        },
        "windows": {
            "discovery": [{"name": "d", "start_ms": base + 300 * 60_000, "end_ms": base + 380 * 60_000}],
            "validation": [{"name": "v", "start_ms": base + 380 * 60_000, "end_ms": base + 460 * 60_000}],
            "holdout": [{"name": "h", "start_ms": base + 460 * 60_000, "end_ms": base + 530 * 60_000}]
        },
        "warmup_candles": 300,
        "min_trades": 1
    }
    study_path = tmp_path / "study.json"
    study_path.write_text(json.dumps(study), encoding="utf-8")
    team_path = tmp_path / "team.json"
    team_path.write_text(
        json.dumps(
            {
                "symbol": "BTCUSDT",
                "rsi_period": 14,
                "rsi_entry": 30.0,
                "adx_min": 0.0,
                "adx_max": 100.0,
                "exit_mode": "tp",
                "tp_price_pct": 1.0
            }
        ),
        encoding="utf-8",
    )

    hidden = run_study(study_path, team_path, data_directory=tmp_path)
    assert hidden["holdout_revealed"] is False
    assert hidden["withheld_holdout_windows"] == 1
    assert [row["phase"] for row in hidden["windows"]] == ["discovery", "validation"]

    revealed = run_study(
        study_path,
        team_path,
        data_directory=tmp_path,
        reveal_holdout=True,
    )
    assert revealed["holdout_revealed"] is True
    assert [row["phase"] for row in revealed["windows"]] == [
        "discovery",
        "validation",
        "holdout",
    ]
    assert all(0.0 <= row["metrics"]["exposure_pct"] <= 100.0 for row in revealed["windows"])
