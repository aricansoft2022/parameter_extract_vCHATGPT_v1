import json
from pathlib import Path

import pytest

import parameter_extract.scale_calibration as calibration_module
from parameter_extract.models import ExecutionModel
from parameter_extract.search import load_search_json, search_fingerprint
from parameter_extract.scale_calibration import (
    load_scale_calibration_json,
    run_scale_calibration,
)
from parameter_extract.study import StudySpec, WindowSpec, study_fingerprint


def _study_payload() -> dict:
    return {
        "schema_version": 1,
        "name": "scale fixture",
        "symbol": "BTCUSDT",
        "dataset_manifest": "unused.json",
        "dataset_fingerprint_sha256": "a" * 64,
        "execution": {
            "name": "expected_live",
            "entry_timing": "next_open",
            "taker_fee_bps": 4.0,
            "buy_slippage_bps": 2.0,
            "sell_slippage_bps": 2.0,
        },
        "windows": {
            "discovery": [{"name": "d", "start_ms": 1000, "end_ms": 2000}],
            "validation": [{"name": "v", "start_ms": 2000, "end_ms": 3000}],
            "holdout": [{"name": "h", "start_ms": 3000, "end_ms": 4000}],
        },
        "warmup_candles": 300,
        "min_trades": 1,
    }


def _search_payload(max_candidates: int) -> dict:
    return {
        "schema_version": 1,
        "name": f"scale-{max_candidates}",
        "exit_modes": ["tp"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14],
            "rsi_entry": {"start": 30.0, "stop": 45.0, "step": 15.0},
            "adx_min": {"start": 0.0, "stop": 0.0, "step": 1.0},
            "adx_max": {"start": 100.0, "stop": 100.0, "step": 1.0},
            "tp_price_pct": {"start": 0.2, "stop": 0.4, "step": 0.2},
            "rsi_exit": None,
        },
        "gates": {
            "min_total_trades": 1,
            "min_positive_window_fraction": 0.0,
        },
        "refinement": {
            "enabled": False,
            "step_divisor": 2,
            "radius_steps": 1,
            "max_seeds": 1,
            "max_candidates": max_candidates,
        },
    }


def _write_contract(tmp_path: Path, budgets=(100, 200, 400)) -> Path:
    study_path = tmp_path / "study.json"
    study_path.write_text(json.dumps(_study_payload()), encoding="utf-8")
    study_spec = calibration_module.load_study_json(study_path)

    stages = []
    for budget in budgets:
        search_path = tmp_path / f"search-{budget}.json"
        search_path.write_text(json.dumps(_search_payload(budget)), encoding="utf-8")
        search_spec = load_search_json(search_path)
        stages.append(
            {
                "name": f"stage-{budget}",
                "search_file": search_path.name,
                "search_fingerprint_sha256": search_fingerprint(search_spec),
                "expected_max_candidates": budget,
                "min_evaluated_candidates": budget // 2,
                "max_elapsed_seconds": 10.0,
                "max_peak_python_heap_mb": 256.0,
            }
        )

    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "scale ladder",
                "study_file": study_path.name,
                "study_fingerprint_sha256": study_fingerprint(study_spec),
                "dataset_fingerprint_sha256": study_spec.dataset_fingerprint_sha256,
                "stages": stages,
            }
        ),
        encoding="utf-8",
    )
    return path


def _fake_result(search_path: Path, evaluated: int) -> dict:
    search_spec = load_search_json(search_path)
    study = StudySpec(
        name="scale fixture",
        symbol="BTCUSDT",
        dataset_manifest="unused.json",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
        discovery=(WindowSpec("d", 1000, 2000),),
        validation=(WindowSpec("v", 2000, 3000),),
        holdout=(WindowSpec("h", 3000, 4000),),
        warmup_candles=300,
        min_trades=1,
    )
    return {
        "schema_version": 1,
        "kind": "parameter_extract.discovery_search",
        "search_engine": "bulk_entry_membership_exact_v1",
        "runtime_parity_passed": True,
        "validation_accessed": False,
        "holdout_accessed": False,
        "study_fingerprint_sha256": study_fingerprint(study),
        "dataset_fingerprint_sha256": "a" * 64,
        "search_fingerprint_sha256": search_fingerprint(search_spec),
        "entry_signal_cache_misses": 0,
        "evaluated_candidates": evaluated,
        "coarse_candidates": evaluated,
        "refined_candidates": 0,
        "pareto_candidates": 3,
        "event_scan_reduction_fraction": 0.8,
        "bulk_entry_event_visits": 10,
        "bulk_entry_band_membership_checks": 20,
        "query_work_profile": {
            "candidate_evaluations": evaluated,
            "candidate_window_replays": evaluated,
            "accepted_positions": evaluated * 2,
            "closed_trades": evaluated * 2,
            "open_positions": 0,
            "exit_lookup_requests": evaluated * 2,
            "excursion_range_requests": evaluated * 2,
            "funding_event_checks": evaluated * 2,
            "closed_trade_signal_bisects": evaluated * 2,
        },
    }


def test_scale_calibration_passes_increasing_ladder_and_reports_last_safe_cap(
    tmp_path: Path,
    monkeypatch,
):
    path = _write_contract(tmp_path)

    def fake_measure(_study, search, *, data_directory):
        budget = load_search_json(search).refinement.max_candidates
        return _fake_result(search, budget), budget / 100.0, budget / 10.0

    monkeypatch.setattr(calibration_module, "_measure_stage", fake_measure)
    result = run_scale_calibration(path, data_directory=tmp_path)

    assert result["all_stages_passed"] is True
    assert result["safe_max_candidates"] == 400
    assert result["stopped_after_stage"] is None
    assert [row["status"] for row in result["stage_results"]] == ["PASS", "PASS", "PASS"]
    assert result["auto_raises_candidate_cap"] is False


def test_scale_calibration_stops_at_first_resource_failure(tmp_path: Path, monkeypatch):
    path = _write_contract(tmp_path)
    calls = []

    def fake_measure(_study, search, *, data_directory):
        budget = load_search_json(search).refinement.max_candidates
        calls.append(budget)
        elapsed = 20.0 if budget == 200 else 1.0
        return _fake_result(search, budget), elapsed, 32.0

    monkeypatch.setattr(calibration_module, "_measure_stage", fake_measure)
    result = run_scale_calibration(path, data_directory=tmp_path)

    assert calls == [100, 200]
    assert result["safe_max_candidates"] == 100
    assert result["stopped_after_stage"] == "stage-200"
    assert result["all_stages_passed"] is False
    assert result["stage_results"][1]["failure_reasons"] == ["ELAPSED_TIME"]


def test_scale_calibration_requires_stage_to_exercise_declared_scale(
    tmp_path: Path,
    monkeypatch,
):
    path = _write_contract(tmp_path, budgets=(100,))

    def fake_measure(_study, search, *, data_directory):
        return _fake_result(search, 10), 1.0, 16.0

    monkeypatch.setattr(calibration_module, "_measure_stage", fake_measure)
    result = run_scale_calibration(path, data_directory=tmp_path)
    assert result["safe_max_candidates"] is None
    assert result["stage_results"][0]["failure_reasons"] == [
        "INSUFFICIENT_SCALE_EXERCISE"
    ]


def test_scale_calibration_records_engine_error_and_stops(tmp_path: Path, monkeypatch):
    path = _write_contract(tmp_path)

    def fake_measure(_study, search, *, data_directory):
        raise RuntimeError("parity exploded")

    monkeypatch.setattr(calibration_module, "_measure_stage", fake_measure)
    result = run_scale_calibration(path, data_directory=tmp_path)
    assert len(result["stage_results"]) == 1
    assert result["stage_results"][0]["status"] == "FAIL"
    assert result["stage_results"][0]["failure_reasons"] == ["ENGINE_ERROR"]
    assert result["safe_max_candidates"] is None


def test_scale_contract_rejects_non_increasing_candidate_budgets(tmp_path: Path):
    path = _write_contract(tmp_path, budgets=(100, 200))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stages"][1]["expected_max_candidates"] = 100
    payload["stages"][1]["min_evaluated_candidates"] = 50
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="strictly increase"):
        load_scale_calibration_json(bad)


def test_scale_calibration_rejects_search_fingerprint_drift(tmp_path: Path):
    path = _write_contract(tmp_path, budgets=(100,))
    search_path = tmp_path / "search-100.json"
    payload = json.loads(search_path.read_text(encoding="utf-8"))
    payload["ranges"]["rsi_entry"]["start"] = 31.0
    search_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="different search contract"):
        run_scale_calibration(path, data_directory=tmp_path)
