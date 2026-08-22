from __future__ import annotations

import gc
import hashlib
import json
import os
import platform
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bulk_search import run_bulk_search
from .search import load_search_json, search_fingerprint
from .study import load_study_json, study_fingerprint

CALIBRATION_SCHEMA_VERSION = 1
CALIBRATION_RESULT_SCHEMA_VERSION = 1
CALIBRATION_ENGINE = "bulk_entry_membership_exact_v1"


@dataclass(frozen=True, slots=True)
class CalibrationStage:
    name: str
    search_file: str
    search_fingerprint_sha256: str
    expected_max_candidates: int
    min_evaluated_candidates: int
    max_elapsed_seconds: float
    max_peak_python_heap_mb: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("calibration stage name cannot be empty")
        if not self.search_file.strip():
            raise ValueError("calibration stage search_file cannot be empty")
        _validate_digest(self.search_fingerprint_sha256)
        if self.expected_max_candidates < 1:
            raise ValueError("expected_max_candidates must be positive")
        if not 1 <= self.min_evaluated_candidates <= self.expected_max_candidates:
            raise ValueError(
                "min_evaluated_candidates must be inside [1, expected_max_candidates]"
            )
        if self.max_elapsed_seconds <= 0.0:
            raise ValueError("max_elapsed_seconds must be positive")
        if self.max_peak_python_heap_mb <= 0.0:
            raise ValueError("max_peak_python_heap_mb must be positive")


@dataclass(frozen=True, slots=True)
class ScaleCalibrationSpec:
    name: str
    study_file: str
    study_fingerprint_sha256: str
    dataset_fingerprint_sha256: str
    stages: tuple[CalibrationStage, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("calibration name cannot be empty")
        if not self.study_file.strip():
            raise ValueError("study_file cannot be empty")
        _validate_digest(self.study_fingerprint_sha256)
        _validate_digest(self.dataset_fingerprint_sha256)
        if not self.stages:
            raise ValueError("scale calibration requires at least one stage")
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("calibration stage names must be unique")
        budgets = [stage.expected_max_candidates for stage in self.stages]
        if any(right <= left for left, right in zip(budgets, budgets[1:], strict=False)):
            raise ValueError("calibration expected_max_candidates must strictly increase")


def load_scale_calibration_json(path: str | Path) -> ScaleCalibrationSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("unsupported scale-calibration schema_version")
    return _spec_from_payload(payload)


def _spec_from_payload(payload: dict[str, Any]) -> ScaleCalibrationSpec:
    return ScaleCalibrationSpec(
        name=str(payload["name"]),
        study_file=str(payload["study_file"]),
        study_fingerprint_sha256=str(payload["study_fingerprint_sha256"]).lower(),
        dataset_fingerprint_sha256=str(payload["dataset_fingerprint_sha256"]).lower(),
        stages=tuple(
            CalibrationStage(
                name=str(row["name"]),
                search_file=str(row["search_file"]),
                search_fingerprint_sha256=str(
                    row["search_fingerprint_sha256"]
                ).lower(),
                expected_max_candidates=int(row["expected_max_candidates"]),
                min_evaluated_candidates=int(row["min_evaluated_candidates"]),
                max_elapsed_seconds=float(row["max_elapsed_seconds"]),
                max_peak_python_heap_mb=float(row["max_peak_python_heap_mb"]),
            )
            for row in payload["stages"]
        ),
    )


def scale_calibration_fingerprint(spec: ScaleCalibrationSpec) -> str:
    canonical = json.dumps(
        asdict(spec),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def scale_calibration_result_fingerprint(payload: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key != "scale_calibration_result_fingerprint_sha256"
    }
    canonical = json.dumps(
        stable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_scale_calibration(
    calibration_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    calibration_file = Path(calibration_path).resolve()
    spec = load_scale_calibration_json(calibration_file)
    root = calibration_file.parent
    study_path = _resolve_contract_path(root, spec.study_file, label="study_file")
    study = load_study_json(study_path)
    current_study_fp = study_fingerprint(study)
    if current_study_fp != spec.study_fingerprint_sha256:
        raise ValueError("scale calibration is pinned to a different study contract")
    if study.dataset_fingerprint_sha256 != spec.dataset_fingerprint_sha256:
        raise ValueError("scale calibration is pinned to a different dataset")

    results: list[dict[str, Any]] = []
    safe_max_candidates: int | None = None
    stopped_after_stage: str | None = None

    for stage in spec.stages:
        search_path = _resolve_contract_path(
            root, stage.search_file, label=f"stage {stage.name!r} search_file"
        )
        search_spec = load_search_json(search_path)
        current_search_fp = search_fingerprint(search_spec)
        if current_search_fp != stage.search_fingerprint_sha256:
            raise ValueError(
                f"calibration stage {stage.name!r} is pinned to a different search contract"
            )
        if search_spec.refinement.max_candidates != stage.expected_max_candidates:
            raise ValueError(
                f"calibration stage {stage.name!r} expected max_candidates="
                f"{stage.expected_max_candidates}, found "
                f"{search_spec.refinement.max_candidates}"
            )

        try:
            result, elapsed_seconds, peak_heap_mb = _measure_stage(
                study_path,
                search_path,
                data_directory=data_directory,
            )
            reasons = _stage_failures(
                stage,
                result,
                elapsed_seconds=elapsed_seconds,
                peak_heap_mb=peak_heap_mb,
                expected_study_fp=current_study_fp,
                expected_dataset_fp=spec.dataset_fingerprint_sha256,
                expected_search_fp=current_search_fp,
            )
            status = "PASS" if not reasons else "FAIL"
            row = {
                "name": stage.name,
                "status": status,
                "failure_reasons": reasons,
                "search_file": stage.search_file,
                "search_fingerprint_sha256": current_search_fp,
                "expected_max_candidates": stage.expected_max_candidates,
                "min_evaluated_candidates": stage.min_evaluated_candidates,
                "result_kind": result.get("kind"),
                "engine": result.get("search_engine"),
                "validation_accessed": result.get("validation_accessed"),
                "holdout_accessed": result.get("holdout_accessed"),
                "study_fingerprint_sha256": result.get("study_fingerprint_sha256"),
                "dataset_fingerprint_sha256": result.get("dataset_fingerprint_sha256"),
                "evaluated_candidates": result.get("evaluated_candidates"),
                "coarse_candidates": result.get("coarse_candidates"),
                "refined_candidates": result.get("refined_candidates"),
                "pareto_candidates": result.get("pareto_candidates"),
                "elapsed_seconds": elapsed_seconds,
                "peak_python_heap_mb": peak_heap_mb,
                "max_elapsed_seconds": stage.max_elapsed_seconds,
                "max_peak_python_heap_mb": stage.max_peak_python_heap_mb,
                "runtime_parity_passed": result.get("runtime_parity_passed"),
                "entry_signal_cache_misses": result.get("entry_signal_cache_misses"),
                "event_scan_reduction_fraction": result.get(
                    "event_scan_reduction_fraction"
                ),
                "bulk_entry_event_visits": result.get("bulk_entry_event_visits"),
                "bulk_entry_band_membership_checks": result.get(
                    "bulk_entry_band_membership_checks"
                ),
                "query_work_profile": result.get("query_work_profile"),
            }
        except Exception as exc:  # fail closed while preserving a calibration artifact
            status = "FAIL"
            row = {
                "name": stage.name,
                "status": status,
                "failure_reasons": ["ENGINE_ERROR"],
                "search_file": stage.search_file,
                "search_fingerprint_sha256": current_search_fp,
                "expected_max_candidates": stage.expected_max_candidates,
                "min_evaluated_candidates": stage.min_evaluated_candidates,
                "max_elapsed_seconds": stage.max_elapsed_seconds,
                "max_peak_python_heap_mb": stage.max_peak_python_heap_mb,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }

        results.append(row)
        if status != "PASS":
            stopped_after_stage = stage.name
            break
        safe_max_candidates = stage.expected_max_candidates

    payload: dict[str, Any] = {
        "schema_version": CALIBRATION_RESULT_SCHEMA_VERSION,
        "kind": "parameter_extract.scale_calibration_result",
        "calibration": spec.name,
        "calibration_fingerprint_sha256": scale_calibration_fingerprint(spec),
        "calibration_spec": asdict(spec),
        "study_fingerprint_sha256": current_study_fp,
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "engine": CALIBRATION_ENGINE,
        "machine": _machine_metadata(),
        "resource_measurement": {
            "elapsed": "time.perf_counter wall clock per complete stage run",
            "memory": "tracemalloc peak Python heap per complete stage run",
        },
        "fail_closed": True,
        "auto_raises_candidate_cap": False,
        "all_stages_passed": len(results) == len(spec.stages)
        and all(row["status"] == "PASS" for row in results),
        "safe_max_candidates": safe_max_candidates,
        "stopped_after_stage": stopped_after_stage,
        "stage_results": results,
    }
    payload["scale_calibration_result_fingerprint_sha256"] = (
        scale_calibration_result_fingerprint(payload)
    )
    problems = verify_scale_calibration_result(payload)
    if problems:
        raise RuntimeError(
            "generated scale-calibration result failed self-verification: "
            + "; ".join(problems)
        )
    return payload


def verify_scale_calibration_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != CALIBRATION_RESULT_SCHEMA_VERSION:
        problems.append("unsupported scale-calibration result schema_version")
    if payload.get("kind") != "parameter_extract.scale_calibration_result":
        problems.append("scale-calibration result kind is invalid")
    if payload.get("engine") != CALIBRATION_ENGINE:
        problems.append("scale-calibration engine is unsupported")
    if payload.get("fail_closed") is not True:
        problems.append("scale calibration must be fail_closed")
    if payload.get("auto_raises_candidate_cap") is not False:
        problems.append("scale calibration must not auto-raise candidate caps")

    spec_payload = payload.get("calibration_spec")
    spec: ScaleCalibrationSpec | None = None
    if not isinstance(spec_payload, dict):
        problems.append("calibration_spec is missing or invalid")
    else:
        try:
            spec = _spec_from_payload(spec_payload)
            if payload.get("calibration") != spec.name:
                problems.append("calibration name does not match calibration_spec")
            if payload.get("calibration_fingerprint_sha256") != scale_calibration_fingerprint(spec):
                problems.append("calibration fingerprint does not match calibration_spec")
            if payload.get("study_fingerprint_sha256") != spec.study_fingerprint_sha256:
                problems.append("result study fingerprint does not match calibration_spec")
            if payload.get("dataset_fingerprint_sha256") != spec.dataset_fingerprint_sha256:
                problems.append("result dataset fingerprint does not match calibration_spec")
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"calibration_spec is invalid: {exc}")

    stages = payload.get("stage_results")
    if not isinstance(stages, list):
        problems.append("stage_results is missing or invalid")
    elif spec is not None:
        if len(stages) > len(spec.stages):
            problems.append("stage_results contains more stages than calibration_spec")
        first_failure: str | None = None
        last_safe: int | None = None
        for index, row in enumerate(stages):
            if index >= len(spec.stages):
                break
            stage = spec.stages[index]
            if not isinstance(row, dict):
                problems.append(f"stage result {index} is invalid")
                continue
            if row.get("name") != stage.name:
                problems.append(f"stage result {index} name/order does not match contract")
            expected_failures = _stored_stage_failures(
                stage,
                row,
                expected_study_fp=spec.study_fingerprint_sha256,
                expected_dataset_fp=spec.dataset_fingerprint_sha256,
            )
            expected_status = "PASS" if not expected_failures else "FAIL"
            if row.get("failure_reasons") != expected_failures:
                problems.append(f"stage result {index} failure reasons do not match evidence")
            if row.get("status") != expected_status:
                problems.append(f"stage result {index} status does not match evidence")
            if first_failure is None and expected_status == "FAIL":
                first_failure = stage.name
                if index != len(stages) - 1:
                    problems.append("stage_results continued after the first failed stage")
            if first_failure is None and expected_status == "PASS":
                last_safe = stage.expected_max_candidates

        expected_all_passed = (
            len(stages) == len(spec.stages)
            and bool(stages)
            and first_failure is None
        )
        if payload.get("all_stages_passed") != expected_all_passed:
            problems.append("all_stages_passed is inconsistent with stage results")
        if payload.get("safe_max_candidates") != last_safe:
            problems.append("safe_max_candidates is inconsistent with stage results")
        if payload.get("stopped_after_stage") != first_failure:
            problems.append("stopped_after_stage is inconsistent with stage results")

    expected_result_fp = payload.get("scale_calibration_result_fingerprint_sha256")
    try:
        actual_result_fp = scale_calibration_result_fingerprint(payload)
        if expected_result_fp != actual_result_fp:
            problems.append("scale-calibration result fingerprint mismatch")
    except (TypeError, ValueError) as exc:
        problems.append(f"scale-calibration result fingerprint cannot be recomputed: {exc}")
    return problems


def _measure_stage(
    study_path: Path,
    search_path: Path,
    *,
    data_directory: str | Path,
) -> tuple[dict[str, Any], float, float]:
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = run_bulk_search(
            study_path,
            search_path,
            data_directory=data_directory,
        )
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, elapsed, peak / (1024.0 * 1024.0)


def _stage_failures(
    stage: CalibrationStage,
    result: dict[str, Any],
    *,
    elapsed_seconds: float,
    peak_heap_mb: float,
    expected_study_fp: str,
    expected_dataset_fp: str,
    expected_search_fp: str,
) -> list[str]:
    evidence = {
        "search_file": stage.search_file,
        "search_fingerprint_sha256": result.get("search_fingerprint_sha256"),
        "expected_max_candidates": stage.expected_max_candidates,
        "min_evaluated_candidates": stage.min_evaluated_candidates,
        "result_kind": result.get("kind"),
        "engine": result.get("search_engine"),
        "validation_accessed": result.get("validation_accessed"),
        "holdout_accessed": result.get("holdout_accessed"),
        "study_fingerprint_sha256": result.get("study_fingerprint_sha256"),
        "dataset_fingerprint_sha256": result.get("dataset_fingerprint_sha256"),
        "evaluated_candidates": result.get("evaluated_candidates"),
        "elapsed_seconds": elapsed_seconds,
        "peak_python_heap_mb": peak_heap_mb,
        "max_elapsed_seconds": stage.max_elapsed_seconds,
        "max_peak_python_heap_mb": stage.max_peak_python_heap_mb,
        "runtime_parity_passed": result.get("runtime_parity_passed"),
        "entry_signal_cache_misses": result.get("entry_signal_cache_misses"),
        "query_work_profile": result.get("query_work_profile"),
    }
    failures = _stored_stage_failures(
        stage,
        evidence,
        expected_study_fp=expected_study_fp,
        expected_dataset_fp=expected_dataset_fp,
    )
    if result.get("search_fingerprint_sha256") != expected_search_fp and "SEARCH_FINGERPRINT" not in failures:
        failures.append("SEARCH_FINGERPRINT")
    return failures


def _stored_stage_failures(
    stage: CalibrationStage,
    row: dict[str, Any],
    *,
    expected_study_fp: str,
    expected_dataset_fp: str,
) -> list[str]:
    stored = row.get("failure_reasons")
    if stored == ["ENGINE_ERROR"] or row.get("error_type") is not None:
        if not isinstance(row.get("error_type"), str) or not isinstance(row.get("error_message"), str):
            return ["ENGINE_ERROR_METADATA"]
        return ["ENGINE_ERROR"]

    failures: list[str] = []
    if row.get("result_kind") != "parameter_extract.discovery_search":
        failures.append("RESULT_KIND")
    if row.get("engine") != CALIBRATION_ENGINE:
        failures.append("ENGINE_IDENTITY")
    if row.get("runtime_parity_passed") is not True:
        failures.append("PARITY")
    if row.get("validation_accessed") is not False or row.get("holdout_accessed") is not False:
        failures.append("PHASE_ISOLATION")
    if row.get("study_fingerprint_sha256") != expected_study_fp:
        failures.append("STUDY_FINGERPRINT")
    if row.get("dataset_fingerprint_sha256") != expected_dataset_fp:
        failures.append("DATASET_FINGERPRINT")
    if row.get("search_fingerprint_sha256") != stage.search_fingerprint_sha256:
        failures.append("SEARCH_FINGERPRINT")
    if row.get("search_file") != stage.search_file:
        failures.append("SEARCH_FILE")
    if row.get("expected_max_candidates") != stage.expected_max_candidates:
        failures.append("CANDIDATE_BUDGET_CONTRACT")
    if row.get("min_evaluated_candidates") != stage.min_evaluated_candidates:
        failures.append("MIN_EVALUATED_CONTRACT")
    if row.get("entry_signal_cache_misses") != 0:
        failures.append("BULK_FALLBACK_MISS")
    evaluated = row.get("evaluated_candidates")
    if not isinstance(evaluated, int) or evaluated < stage.min_evaluated_candidates:
        failures.append("INSUFFICIENT_SCALE_EXERCISE")
    if isinstance(evaluated, int) and evaluated > stage.expected_max_candidates:
        failures.append("CANDIDATE_BUDGET_EXCEEDED")
    elapsed = row.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or elapsed < 0.0:
        failures.append("ELAPSED_MEASUREMENT")
    elif elapsed > stage.max_elapsed_seconds:
        failures.append("ELAPSED_TIME")
    peak = row.get("peak_python_heap_mb")
    if not isinstance(peak, (int, float)) or peak < 0.0:
        failures.append("PYTHON_HEAP_MEASUREMENT")
    elif peak > stage.max_peak_python_heap_mb:
        failures.append("PYTHON_HEAP")
    if row.get("max_elapsed_seconds") != stage.max_elapsed_seconds:
        failures.append("ELAPSED_LIMIT_CONTRACT")
    if row.get("max_peak_python_heap_mb") != stage.max_peak_python_heap_mb:
        failures.append("PYTHON_HEAP_LIMIT_CONTRACT")
    profile = row.get("query_work_profile")
    if not isinstance(profile, dict) or profile.get("candidate_evaluations") != evaluated:
        failures.append("WORK_PROFILE")
    return failures


def _resolve_contract_path(root: Path, relative: str, *, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError(f"{label} must be relative to the calibration file")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"{label} escapes the calibration directory")
    return resolved


def _machine_metadata() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError("expected a 64-character hexadecimal SHA-256 digest")
