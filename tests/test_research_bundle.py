import json
from pathlib import Path

import pytest

import parameter_extract.research_bundle as bundle_module
from parameter_extract.manifest import manifest_fingerprint, sha256_file
from parameter_extract.research_bundle import (
    load_research_bundle_json,
    research_bundle_fingerprint,
    run_bundle_calibration,
    run_bundle_discovery,
    verify_research_bundle,
)
from parameter_extract.scale_calibration import (
    load_scale_calibration_json,
    scale_calibration_fingerprint,
    scale_calibration_result_fingerprint,
)
from parameter_extract.search import load_search_json, search_fingerprint
from parameter_extract.study import load_study_json, study_fingerprint


def _write_bundle(tmp_path: Path, *, discovery_cap: int = 100, calibration_cap: int = 100):
    candles = tmp_path / "candles.csv"
    candles.write_text(
        "open_time,open,high,low,close,volume,close_time\n"
        "1000000,100,101,99,100.5,1,1059999\n"
        "1060000,100.5,102,100,101.5,1,1119999\n",
        encoding="utf-8",
    )
    candle_sha = sha256_file(candles)
    manifest = {
        "schema_version": 1,
        "kind": "parameter_extract.data_manifest",
        "source": "unit-test",
        "files": {
            "candles": {
                "path": "candles.csv",
                "size_bytes": candles.stat().st_size,
                "sha256": candle_sha,
                "expected_sha256": None,
                "checksum_verified": None,
            },
            "funding": None,
        },
        "candles": {
            "rows": 2,
            "start_open_time_ms": 1000000,
            "end_open_time_ms": 1060000,
            "duplicate_open_times": 0,
            "backward_or_unsorted_steps": 0,
            "gap_count": 0,
            "missing_minutes": 0,
            "integrity_ok": True,
        },
    }
    manifest["dataset_fingerprint_sha256"] = manifest_fingerprint(manifest)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    study_payload = {
        "schema_version": 1,
        "name": "bundle fixture",
        "symbol": "BTCUSDT",
        "dataset_manifest": "manifest.json",
        "dataset_fingerprint_sha256": manifest["dataset_fingerprint_sha256"],
        "execution": {
            "name": "expected_live",
            "entry_timing": "next_open",
            "taker_fee_bps": 4.0,
            "buy_slippage_bps": 2.0,
            "sell_slippage_bps": 2.0,
        },
        "windows": {
            "discovery": [{"name": "d", "start_ms": 2_000_000, "end_ms": 3_000_000}],
            "validation": [{"name": "v", "start_ms": 3_000_000, "end_ms": 4_000_000}],
            "holdout": [{"name": "h", "start_ms": 4_000_000, "end_ms": 5_000_000}],
        },
        "warmup_candles": 300,
        "min_trades": 1,
    }
    study_path = tmp_path / "study.json"
    study_path.write_text(json.dumps(study_payload), encoding="utf-8")
    study_fp = study_fingerprint(load_study_json(study_path))

    search_payload = {
        "schema_version": 1,
        "name": "bundle discovery",
        "exit_modes": ["tp"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14],
            "rsi_entry": {"start": 40.0, "stop": 40.0, "step": 1.0},
            "adx_min": {"start": 10.0, "stop": 10.0, "step": 1.0},
            "adx_max": {"start": 30.0, "stop": 30.0, "step": 1.0},
            "tp_price_pct": {"start": 0.2, "stop": 0.2, "step": 0.1},
            "rsi_exit": None,
        },
        "gates": {"min_total_trades": 1, "min_positive_window_fraction": 0.0},
        "refinement": {
            "enabled": True,
            "step_divisor": 2,
            "radius_steps": 1,
            "max_seeds": 1,
            "max_candidates": discovery_cap,
        },
    }
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(search_payload), encoding="utf-8")
    search_fp = search_fingerprint(load_search_json(search_path))

    if calibration_cap == discovery_cap:
        stage_search_file = "search.json"
        stage_search_fp = search_fp
    else:
        calibration_search_payload = dict(search_payload)
        calibration_search_payload["name"] = "bundle calibration"
        calibration_search_payload["refinement"] = dict(search_payload["refinement"])
        calibration_search_payload["refinement"]["max_candidates"] = calibration_cap
        calibration_search_path = tmp_path / "calibration-search.json"
        calibration_search_path.write_text(
            json.dumps(calibration_search_payload), encoding="utf-8"
        )
        stage_search_file = "calibration-search.json"
        stage_search_fp = search_fingerprint(load_search_json(calibration_search_path))

    calibration_payload = {
        "schema_version": 1,
        "name": "bundle scale",
        "study_file": "study.json",
        "study_fingerprint_sha256": study_fp,
        "dataset_fingerprint_sha256": manifest["dataset_fingerprint_sha256"],
        "stages": [
            {
                "name": "stage-1",
                "search_file": stage_search_file,
                "search_fingerprint_sha256": stage_search_fp,
                "expected_max_candidates": calibration_cap,
                "min_evaluated_candidates": 1,
                "max_elapsed_seconds": 60.0,
                "max_peak_python_heap_mb": 512.0,
            }
        ],
    }
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(calibration_payload), encoding="utf-8")
    calibration_fp = scale_calibration_fingerprint(load_scale_calibration_json(calibration_path))

    bundle_payload = {
        "schema_version": 1,
        "name": "real-run fixture",
        "manifest_file": "manifest.json",
        "dataset_fingerprint_sha256": manifest["dataset_fingerprint_sha256"],
        "study_file": "study.json",
        "study_fingerprint_sha256": study_fp,
        "discovery_search_file": "search.json",
        "discovery_search_fingerprint_sha256": search_fp,
        "calibration_file": "calibration.json",
        "calibration_fingerprint_sha256": calibration_fp,
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle_payload), encoding="utf-8")
    return bundle_path, bundle_payload


def _machine() -> dict:
    return bundle_module._current_machine_metadata()


def test_bundle_preflight_verifies_static_lineage_without_touching_phases(tmp_path: Path):
    bundle_path, payload = _write_bundle(tmp_path)
    result = verify_research_bundle(bundle_path, data_directory=tmp_path)

    spec = load_research_bundle_json(bundle_path)
    assert result["bundle_fingerprint_sha256"] == research_bundle_fingerprint(spec)
    assert result["dataset_fingerprint_sha256"] == payload["dataset_fingerprint_sha256"]
    assert result["required_safe_max_candidates"] == 100
    assert result["calibration_stage_budgets"] == [100]
    assert result["exact_discovery_calibration_stages"] == ["stage-1"]
    assert result["manifest_verified"] is True
    assert result["contract_lineage_verified"] is True
    assert result["ready_for_calibration"] is True
    assert result["ready_for_discovery"] is False
    assert result["discovery_accessed"] is False
    assert result["validation_accessed"] is False
    assert result["holdout_accessed"] is False


def test_bundle_rejects_contract_path_escape(tmp_path: Path):
    bundle_path, payload = _write_bundle(tmp_path)
    outside = tmp_path.parent / "outside-study.json"
    outside.write_text("{}", encoding="utf-8")
    payload["study_file"] = "../outside-study.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="escapes the research bundle directory"):
        verify_research_bundle(bundle_path, data_directory=tmp_path)


def test_bundle_rejects_calibration_ladder_that_never_reaches_discovery_budget(tmp_path: Path):
    bundle_path, _ = _write_bundle(tmp_path, discovery_cap=100, calibration_cap=50)
    with pytest.raises(ValueError, match="never reaches the discovery search candidate budget"):
        verify_research_bundle(bundle_path, data_directory=tmp_path)


def test_bundle_rejects_same_budget_calibration_with_different_grid(tmp_path: Path):
    bundle_path, bundle_payload = _write_bundle(tmp_path)
    search_payload = json.loads((tmp_path / "search.json").read_text(encoding="utf-8"))
    search_payload["name"] = "different calibration grid"
    other_path = tmp_path / "other-search.json"
    other_path.write_text(json.dumps(search_payload), encoding="utf-8")
    other_fp = search_fingerprint(load_search_json(other_path))

    calibration_payload = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    calibration_payload["stages"][0]["search_file"] = "other-search.json"
    calibration_payload["stages"][0]["search_fingerprint_sha256"] = other_fp
    (tmp_path / "calibration.json").write_text(json.dumps(calibration_payload), encoding="utf-8")
    bundle_payload["calibration_fingerprint_sha256"] = scale_calibration_fingerprint(
        load_scale_calibration_json(tmp_path / "calibration.json")
    )
    bundle_path.write_text(json.dumps(bundle_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="must contain the exact discovery search contract"):
        verify_research_bundle(bundle_path, data_directory=tmp_path)


def test_bundle_calibration_recomputes_scale_result_fingerprint_after_metadata(
    tmp_path: Path, monkeypatch
):
    bundle_path, payload = _write_bundle(tmp_path)
    calibration_result = {
        "schema_version": 1,
        "kind": "parameter_extract.scale_calibration_result",
        "calibration_fingerprint_sha256": payload["calibration_fingerprint_sha256"],
        "study_fingerprint_sha256": payload["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": payload["dataset_fingerprint_sha256"],
        "machine": _machine(),
        "safe_max_candidates": 100,
    }
    calibration_result["scale_calibration_result_fingerprint_sha256"] = (
        scale_calibration_result_fingerprint(calibration_result)
    )

    monkeypatch.setattr(
        bundle_module,
        "run_scale_calibration",
        lambda *args, **kwargs: dict(calibration_result),
    )
    monkeypatch.setattr(bundle_module, "verify_scale_calibration_result", lambda payload: [])

    result = run_bundle_calibration(bundle_path, data_directory=tmp_path)
    assert result["research_bundle"]["required_safe_max_candidates"] == 100
    assert result["research_bundle"]["exact_discovery_calibration_stages"] == ["stage-1"]
    assert result["scale_calibration_result_fingerprint_sha256"] == (
        scale_calibration_result_fingerprint(result)
    )


def test_bundle_discovery_blocks_when_safe_cap_is_too_small(tmp_path: Path, monkeypatch):
    bundle_path, payload = _write_bundle(tmp_path)
    calibration_result = {
        "calibration_fingerprint_sha256": payload["calibration_fingerprint_sha256"],
        "study_fingerprint_sha256": payload["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": payload["dataset_fingerprint_sha256"],
        "machine": _machine(),
        "safe_max_candidates": 50,
        "stage_results": [],
    }
    result_path = tmp_path / "calibration-result.json"
    result_path.write_text(json.dumps(calibration_result), encoding="utf-8")
    monkeypatch.setattr(bundle_module, "verify_scale_calibration_result", lambda payload: [])
    called = False

    def fake_search(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(bundle_module, "run_bulk_search", fake_search)
    with pytest.raises(RuntimeError, match="calibration safe_max_candidates"):
        run_bundle_discovery(bundle_path, result_path, data_directory=tmp_path)
    assert called is False


def test_bundle_discovery_blocks_calibration_from_different_machine(tmp_path: Path, monkeypatch):
    bundle_path, payload = _write_bundle(tmp_path)
    wrong_machine = dict(_machine())
    wrong_machine["cpu_count"] = (wrong_machine.get("cpu_count") or 1) + 1
    calibration_result = {
        "calibration_fingerprint_sha256": payload["calibration_fingerprint_sha256"],
        "study_fingerprint_sha256": payload["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": payload["dataset_fingerprint_sha256"],
        "machine": wrong_machine,
        "safe_max_candidates": 100,
        "stage_results": [{"name": "stage-1", "status": "PASS"}],
    }
    result_path = tmp_path / "calibration-result.json"
    result_path.write_text(json.dumps(calibration_result), encoding="utf-8")
    monkeypatch.setattr(bundle_module, "verify_scale_calibration_result", lambda payload: [])
    called = False

    def fake_search(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(bundle_module, "run_bulk_search", fake_search)
    with pytest.raises(RuntimeError, match="different machine/runtime"):
        run_bundle_discovery(bundle_path, result_path, data_directory=tmp_path)
    assert called is False


def test_bundle_discovery_runs_only_after_exact_grid_calibration_and_adds_lineage(
    tmp_path: Path, monkeypatch
):
    bundle_path, payload = _write_bundle(tmp_path)
    calibration_result = {
        "calibration_fingerprint_sha256": payload["calibration_fingerprint_sha256"],
        "study_fingerprint_sha256": payload["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": payload["dataset_fingerprint_sha256"],
        "machine": _machine(),
        "safe_max_candidates": 100,
        "stage_results": [{"name": "stage-1", "status": "PASS"}],
        "scale_calibration_result_fingerprint_sha256": "f" * 64,
    }
    result_path = tmp_path / "calibration-result.json"
    result_path.write_text(json.dumps(calibration_result), encoding="utf-8")
    monkeypatch.setattr(bundle_module, "verify_scale_calibration_result", lambda payload: [])

    search_result = {
        "kind": "parameter_extract.discovery_search",
        "search_engine": "bulk_entry_membership_exact_v1",
        "runtime_parity_passed": True,
        "validation_accessed": False,
        "holdout_accessed": False,
        "study_fingerprint_sha256": payload["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": payload["dataset_fingerprint_sha256"],
        "search_fingerprint_sha256": payload["discovery_search_fingerprint_sha256"],
        "evaluated_candidates": 80,
    }
    monkeypatch.setattr(
        bundle_module,
        "run_bulk_search",
        lambda *args, **kwargs: dict(search_result),
    )

    result = run_bundle_discovery(bundle_path, result_path, data_directory=tmp_path)
    lineage = result["research_bundle"]
    assert lineage["calibration_gate_passed"] is True
    assert lineage["calibrated_safe_max_candidates"] == 100
    assert lineage["required_safe_max_candidates"] == 100
    assert lineage["exact_discovery_calibration_stages"] == ["stage-1"]
    assert lineage["calibrated_machine"] == _machine()
    assert lineage["calibration_result_sha256"] == sha256_file(result_path)
