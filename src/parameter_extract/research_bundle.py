from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bulk_search import BULK_SEARCH_ENGINE, run_bulk_search
from .manifest import manifest_fingerprint, sha256_file, verify_manifest
from .scale_calibration import (
    load_scale_calibration_json,
    run_scale_calibration,
    scale_calibration_fingerprint,
    verify_scale_calibration_result,
)
from .search import load_search_json, search_fingerprint
from .study import load_study_json, study_fingerprint

RESEARCH_BUNDLE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ResearchBundleSpec:
    name: str
    manifest_file: str
    dataset_fingerprint_sha256: str
    study_file: str
    study_fingerprint_sha256: str
    discovery_search_file: str
    discovery_search_fingerprint_sha256: str
    calibration_file: str
    calibration_fingerprint_sha256: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("research bundle name cannot be empty")
        for field_name in (
            "manifest_file",
            "study_file",
            "discovery_search_file",
            "calibration_file",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} cannot be empty")
        for field_name in (
            "dataset_fingerprint_sha256",
            "study_fingerprint_sha256",
            "discovery_search_fingerprint_sha256",
            "calibration_fingerprint_sha256",
        ):
            _validate_digest(getattr(self, field_name))


def load_research_bundle_json(path: str | Path) -> ResearchBundleSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != RESEARCH_BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported research-bundle schema_version")
    return ResearchBundleSpec(
        name=str(payload["name"]),
        manifest_file=str(payload["manifest_file"]),
        dataset_fingerprint_sha256=str(payload["dataset_fingerprint_sha256"]).lower(),
        study_file=str(payload["study_file"]),
        study_fingerprint_sha256=str(payload["study_fingerprint_sha256"]).lower(),
        discovery_search_file=str(payload["discovery_search_file"]),
        discovery_search_fingerprint_sha256=str(
            payload["discovery_search_fingerprint_sha256"]
        ).lower(),
        calibration_file=str(payload["calibration_file"]),
        calibration_fingerprint_sha256=str(payload["calibration_fingerprint_sha256"]).lower(),
    )


def research_bundle_fingerprint(spec: ResearchBundleSpec) -> str:
    canonical = json.dumps(
        asdict(spec),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verify_research_bundle(
    bundle_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Verify all static provenance needed before calibration or discovery.

    This function only reads contracts/data identities. It never evaluates discovery,
    validation or holdout windows.
    """
    bundle_file = Path(bundle_path).resolve()
    spec = load_research_bundle_json(bundle_file)
    root = bundle_file.parent

    manifest_path = _resolve_contract_path(root, spec.manifest_file, label="manifest_file")
    study_path = _resolve_contract_path(root, spec.study_file, label="study_file")
    search_path = _resolve_contract_path(
        root, spec.discovery_search_file, label="discovery_search_file"
    )
    calibration_path = _resolve_contract_path(
        root, spec.calibration_file, label="calibration_file"
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored_dataset_fp = manifest.get("dataset_fingerprint_sha256")
    computed_dataset_fp = manifest_fingerprint(manifest)
    if stored_dataset_fp != computed_dataset_fp:
        raise ValueError("bundle manifest fingerprint does not self-verify")
    if stored_dataset_fp != spec.dataset_fingerprint_sha256:
        raise ValueError("bundle is pinned to a different dataset manifest")
    candle_audit = manifest.get("candles")
    if not isinstance(candle_audit, dict) or candle_audit.get("integrity_ok") is not True:
        raise ValueError("bundle dataset candle integrity gate is not healthy")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("bundle manifest files section is missing or invalid")
    for label in ("candles", "funding"):
        record = files.get(label)
        if isinstance(record, dict) and record.get("checksum_verified") is False:
            raise ValueError(f"bundle dataset {label} failed its external checksum")
    manifest_problems = verify_manifest(manifest, directory=data_directory)
    if manifest_problems:
        raise ValueError("bundle dataset verification failed: " + "; ".join(manifest_problems))

    study = load_study_json(study_path)
    current_study_fp = study_fingerprint(study)
    if current_study_fp != spec.study_fingerprint_sha256:
        raise ValueError("bundle is pinned to a different study contract")
    if study.dataset_fingerprint_sha256 != spec.dataset_fingerprint_sha256:
        raise ValueError("bundle study is pinned to a different dataset")
    study_manifest_path = _resolve_contract_path(
        study_path.parent,
        study.dataset_manifest,
        label="study dataset_manifest",
        containment_root=root,
    )
    if study_manifest_path != manifest_path:
        raise ValueError("bundle study does not reference the exact bundled manifest")

    search = load_search_json(search_path)
    current_search_fp = search_fingerprint(search)
    if current_search_fp != spec.discovery_search_fingerprint_sha256:
        raise ValueError("bundle is pinned to a different discovery search contract")

    calibration = load_scale_calibration_json(calibration_path)
    current_calibration_fp = scale_calibration_fingerprint(calibration)
    if current_calibration_fp != spec.calibration_fingerprint_sha256:
        raise ValueError("bundle is pinned to a different scale-calibration contract")
    if calibration.study_fingerprint_sha256 != spec.study_fingerprint_sha256:
        raise ValueError("bundle calibration is pinned to a different study")
    if calibration.dataset_fingerprint_sha256 != spec.dataset_fingerprint_sha256:
        raise ValueError("bundle calibration is pinned to a different dataset")
    calibration_study_path = _resolve_contract_path(
        calibration_path.parent,
        calibration.study_file,
        label="calibration study_file",
        containment_root=root,
    )
    if calibration_study_path != study_path:
        raise ValueError("bundle calibration does not reference the exact bundled study")

    stage_budgets: list[int] = []
    for stage in calibration.stages:
        stage_search_path = _resolve_contract_path(
            calibration_path.parent,
            stage.search_file,
            label=f"calibration stage {stage.name!r} search_file",
            containment_root=root,
        )
        stage_search = load_search_json(stage_search_path)
        stage_search_fp = search_fingerprint(stage_search)
        if stage_search_fp != stage.search_fingerprint_sha256:
            raise ValueError(
                f"bundle calibration stage {stage.name!r} search fingerprint drift"
            )
        if stage_search.refinement.max_candidates != stage.expected_max_candidates:
            raise ValueError(
                f"bundle calibration stage {stage.name!r} candidate budget drift"
            )
        stage_budgets.append(stage.expected_max_candidates)

    required_safe = search.refinement.max_candidates
    if max(stage_budgets) < required_safe:
        raise ValueError(
            "bundle calibration ladder never reaches the discovery search candidate budget"
        )

    return {
        "schema_version": 1,
        "kind": "parameter_extract.research_bundle_verification",
        "bundle": spec.name,
        "bundle_fingerprint_sha256": research_bundle_fingerprint(spec),
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "study_fingerprint_sha256": spec.study_fingerprint_sha256,
        "discovery_search_fingerprint_sha256": spec.discovery_search_fingerprint_sha256,
        "calibration_fingerprint_sha256": spec.calibration_fingerprint_sha256,
        "required_safe_max_candidates": required_safe,
        "calibration_stage_budgets": stage_budgets,
        "manifest_verified": True,
        "contract_lineage_verified": True,
        "ready_for_calibration": True,
        "ready_for_discovery": False,
        "discovery_accessed": False,
        "validation_accessed": False,
        "holdout_accessed": False,
    }


def run_bundle_calibration(
    bundle_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    verification = verify_research_bundle(bundle_path, data_directory=data_directory)
    bundle_file = Path(bundle_path).resolve()
    spec = load_research_bundle_json(bundle_file)
    calibration_path = _resolve_contract_path(
        bundle_file.parent, spec.calibration_file, label="calibration_file"
    )
    result = run_scale_calibration(calibration_path, data_directory=data_directory)
    problems = verify_scale_calibration_result(result)
    if problems:
        raise RuntimeError(
            "bundle calibration result failed self-verification: " + "; ".join(problems)
        )
    if result.get("calibration_fingerprint_sha256") != spec.calibration_fingerprint_sha256:
        raise RuntimeError("bundle calibration result belongs to a different calibration contract")
    if result.get("study_fingerprint_sha256") != spec.study_fingerprint_sha256:
        raise RuntimeError("bundle calibration result belongs to a different study")
    if result.get("dataset_fingerprint_sha256") != spec.dataset_fingerprint_sha256:
        raise RuntimeError("bundle calibration result belongs to a different dataset")
    result["research_bundle"] = {
        "bundle": spec.name,
        "bundle_fingerprint_sha256": verification["bundle_fingerprint_sha256"],
        "required_safe_max_candidates": verification["required_safe_max_candidates"],
    }
    return result


def run_bundle_discovery(
    bundle_path: str | Path,
    calibration_result_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    verification = verify_research_bundle(bundle_path, data_directory=data_directory)
    bundle_file = Path(bundle_path).resolve()
    spec = load_research_bundle_json(bundle_file)
    root = bundle_file.parent
    study_path = _resolve_contract_path(root, spec.study_file, label="study_file")
    search_path = _resolve_contract_path(
        root, spec.discovery_search_file, label="discovery_search_file"
    )

    calibration_result_file = Path(calibration_result_path).resolve()
    calibration_result = json.loads(calibration_result_file.read_text(encoding="utf-8"))
    problems = verify_scale_calibration_result(calibration_result)
    if problems:
        raise ValueError("calibration result verification failed: " + "; ".join(problems))
    if calibration_result.get("calibration_fingerprint_sha256") != spec.calibration_fingerprint_sha256:
        raise ValueError("calibration result belongs to a different bundle calibration")
    if calibration_result.get("study_fingerprint_sha256") != spec.study_fingerprint_sha256:
        raise ValueError("calibration result belongs to a different bundle study")
    if calibration_result.get("dataset_fingerprint_sha256") != spec.dataset_fingerprint_sha256:
        raise ValueError("calibration result belongs to a different bundle dataset")

    safe_max = calibration_result.get("safe_max_candidates")
    required_safe = verification["required_safe_max_candidates"]
    if not isinstance(safe_max, int) or safe_max < required_safe:
        raise RuntimeError(
            "discovery blocked: calibration safe_max_candidates does not cover the "
            f"discovery search budget ({safe_max!r} < {required_safe})"
        )

    result = run_bulk_search(study_path, search_path, data_directory=data_directory)
    if result.get("kind") != "parameter_extract.discovery_search":
        raise RuntimeError("bundle discovery returned an unexpected result kind")
    if result.get("search_engine") != BULK_SEARCH_ENGINE:
        raise RuntimeError("bundle discovery did not use the approved bulk exact engine")
    if result.get("runtime_parity_passed") is not True:
        raise RuntimeError("bundle discovery runtime parity did not pass")
    if result.get("validation_accessed") is not False or result.get("holdout_accessed") is not False:
        raise RuntimeError("bundle discovery phase isolation failed")
    if result.get("study_fingerprint_sha256") != spec.study_fingerprint_sha256:
        raise RuntimeError("bundle discovery study fingerprint drift")
    if result.get("dataset_fingerprint_sha256") != spec.dataset_fingerprint_sha256:
        raise RuntimeError("bundle discovery dataset fingerprint drift")
    if result.get("search_fingerprint_sha256") != spec.discovery_search_fingerprint_sha256:
        raise RuntimeError("bundle discovery search fingerprint drift")
    evaluated = result.get("evaluated_candidates")
    if not isinstance(evaluated, int) or evaluated > safe_max:
        raise RuntimeError("bundle discovery exceeded the calibrated safe candidate budget")

    result["research_bundle"] = {
        "bundle": spec.name,
        "bundle_fingerprint_sha256": verification["bundle_fingerprint_sha256"],
        "calibration_result_sha256": sha256_file(calibration_result_file),
        "calibration_result_fingerprint_sha256": calibration_result.get(
            "scale_calibration_result_fingerprint_sha256"
        ),
        "calibrated_safe_max_candidates": safe_max,
        "required_safe_max_candidates": required_safe,
        "calibration_gate_passed": True,
    }
    return result


def _resolve_contract_path(
    root: Path,
    relative: str,
    *,
    label: str,
    containment_root: Path | None = None,
) -> Path:
    if Path(relative).is_absolute():
        raise ValueError(f"{label} must be relative")
    base = root.resolve()
    container = base if containment_root is None else containment_root.resolve()
    path = (base / relative).resolve()
    try:
        path.relative_to(container)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the research bundle directory") from exc
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {relative}")
    return path


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError("expected a 64-character hexadecimal SHA-256 digest")
