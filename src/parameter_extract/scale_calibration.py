from __future__ import annotations

import gc
import hashlib
import json
import os
import platform
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .bulk_search import run_bulk_search
from .search import load_search_json, search_fingerprint
from .study import load_study_json, study_fingerprint

CALIBRATION_SCHEMA_VERSION = 1


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
        asdict(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_scale_calibration(
    calibration_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    calibration_file = Path(calibration_path)
    spec = load_scale_calibration_json(calibration_file)
    root = calibration_file.parent
    study_path = (root / spec.study_file).resolve()
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
        search_path = (root / stage.search_file).resolve()
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

    return {
        "schema_version": 1,
        "kind": "parameter_extract.scale_calibration_result",
        "calibration": spec.name,
        "calibration_fingerprint_sha256": scale_calibration_fingerprint(spec),
        "calibration_spec": asdict(spec),
        "study_fingerprint_sha256": current_study_fp,
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "engine": "bulk_entry_membership_exact_v1",
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
    failures: list[str] = []
    if result.get("kind") != "parameter_extract.discovery_search":
        failures.append("RESULT_KIND")
    if result.get("search_engine") != "bulk_entry_membership_exact_v1":
        failures.append("ENGINE_IDENTITY")
    if result.get("runtime_parity_passed") is not True:
        failures.append("PARITY")
    if result.get("validation_accessed") is not False or result.get("holdout_accessed") is not False:
        failures.append("PHASE_ISOLATION")
    if result.get("study_fingerprint_sha256") != expected_study_fp:
        failures.append("STUDY_FINGERPRINT")
    if result.get("dataset_fingerprint_sha256") != expected_dataset_fp:
        failures.append("DATASET_FINGERPRINT")
    if result.get("search_fingerprint_sha256") != expected_search_fp:
        failures.append("SEARCH_FINGERPRINT")
    if result.get("entry_signal_cache_misses") != 0:
        failures.append("BULK_FALLBACK_MISS")
    evaluated = result.get("evaluated_candidates")
    if not isinstance(evaluated, int) or evaluated < stage.min_evaluated_candidates:
        failures.append("INSUFFICIENT_SCALE_EXERCISE")
    if isinstance(evaluated, int) and evaluated > stage.expected_max_candidates:
        failures.append("CANDIDATE_BUDGET_EXCEEDED")
    if elapsed_seconds > stage.max_elapsed_seconds:
        failures.append("ELAPSED_TIME")
    if peak_heap_mb > stage.max_peak_python_heap_mb:
        failures.append("PYTHON_HEAP")
    profile = result.get("query_work_profile")
    if not isinstance(profile, dict) or profile.get("candidate_evaluations") != evaluated:
        failures.append("WORK_PROFILE")
    return failures


def _machine_metadata() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "executable": sys.executable,
    }


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError("expected a 64-character hexadecimal SHA-256 digest")
