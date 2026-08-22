from dataclasses import asdict
from pathlib import Path

import pytest

from parameter_extract.scale_calibration import (
    CALIBRATION_ENGINE,
    CalibrationStage,
    ScaleCalibrationSpec,
    _resolve_contract_path,
    scale_calibration_fingerprint,
    scale_calibration_result_fingerprint,
    verify_scale_calibration_result,
)


def _valid_result() -> dict:
    stage = CalibrationStage(
        name="stage-100",
        search_file="search-100.json",
        search_fingerprint_sha256="b" * 64,
        expected_max_candidates=100,
        min_evaluated_candidates=50,
        max_elapsed_seconds=10.0,
        max_peak_python_heap_mb=256.0,
    )
    spec = ScaleCalibrationSpec(
        name="integrity",
        study_file="study.json",
        study_fingerprint_sha256="a" * 64,
        dataset_fingerprint_sha256="c" * 64,
        stages=(stage,),
    )
    row = {
        "name": stage.name,
        "status": "PASS",
        "failure_reasons": [],
        "search_file": stage.search_file,
        "search_fingerprint_sha256": stage.search_fingerprint_sha256,
        "expected_max_candidates": 100,
        "min_evaluated_candidates": 50,
        "result_kind": "parameter_extract.discovery_search",
        "engine": CALIBRATION_ENGINE,
        "validation_accessed": False,
        "holdout_accessed": False,
        "study_fingerprint_sha256": "a" * 64,
        "dataset_fingerprint_sha256": "c" * 64,
        "evaluated_candidates": 75,
        "coarse_candidates": 75,
        "refined_candidates": 0,
        "pareto_candidates": 3,
        "elapsed_seconds": 4.0,
        "peak_python_heap_mb": 32.0,
        "max_elapsed_seconds": 10.0,
        "max_peak_python_heap_mb": 256.0,
        "runtime_parity_passed": True,
        "entry_signal_cache_misses": 0,
        "event_scan_reduction_fraction": 0.8,
        "bulk_entry_event_visits": 10,
        "bulk_entry_band_membership_checks": 20,
        "query_work_profile": {"candidate_evaluations": 75},
    }
    payload = {
        "schema_version": 1,
        "kind": "parameter_extract.scale_calibration_result",
        "calibration": spec.name,
        "calibration_fingerprint_sha256": scale_calibration_fingerprint(spec),
        "calibration_spec": asdict(spec),
        "study_fingerprint_sha256": "a" * 64,
        "dataset_fingerprint_sha256": "c" * 64,
        "engine": CALIBRATION_ENGINE,
        "machine": {},
        "resource_measurement": {},
        "fail_closed": True,
        "auto_raises_candidate_cap": False,
        "all_stages_passed": True,
        "safe_max_candidates": 100,
        "stopped_after_stage": None,
        "stage_results": [row],
    }
    payload["scale_calibration_result_fingerprint_sha256"] = (
        scale_calibration_result_fingerprint(payload)
    )
    return payload


def test_scale_calibration_result_self_verifies():
    assert verify_scale_calibration_result(_valid_result()) == []


def test_scale_calibration_result_detects_safe_cap_mutation():
    payload = _valid_result()
    payload["safe_max_candidates"] = 200
    problems = verify_scale_calibration_result(payload)
    assert any("safe_max_candidates" in problem for problem in problems)
    assert any("fingerprint mismatch" in problem for problem in problems)


def test_scale_calibration_result_detects_stage_evidence_mutation():
    payload = _valid_result()
    payload["stage_results"][0]["elapsed_seconds"] = 20.0
    problems = verify_scale_calibration_result(payload)
    assert any("failure reasons" in problem for problem in problems)
    assert any("status" in problem for problem in problems)
    assert any("fingerprint mismatch" in problem for problem in problems)


def test_calibration_contract_paths_cannot_escape_root(tmp_path: Path):
    inside = _resolve_contract_path(tmp_path, "nested/search.json", label="search")
    assert inside == (tmp_path / "nested/search.json").resolve()
    with pytest.raises(ValueError, match="escapes"):
        _resolve_contract_path(tmp_path, "../outside.json", label="search")
    with pytest.raises(ValueError, match="relative"):
        _resolve_contract_path(tmp_path, "/tmp/outside.json", label="search")
