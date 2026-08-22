from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .manifest import manifest_fingerprint
from .research_bundle import verify_research_bundle
from .scale_calibration import load_scale_calibration_json, scale_calibration_fingerprint
from .search import load_search_json, search_fingerprint
from .study import load_study_json, study_fingerprint


def seal_research_bundle(
    *,
    name: str,
    manifest_file: str | Path,
    study_file: str | Path,
    discovery_search_file: str | Path,
    calibration_file: str | Path,
    output_file: str | Path,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Create one bundle.json from already-authored contracts and verify it atomically.

    The builder never copies or edits upstream contracts. All four contract files must already
    live inside the output bundle directory. Existing output is never overwritten.
    """
    if not name.strip():
        raise ValueError("research bundle name cannot be empty")

    output = Path(output_file).resolve()
    root = output.parent
    if output.exists():
        raise FileExistsError(f"research bundle output already exists: {output}")
    if not root.is_dir():
        raise ValueError(f"research bundle output directory does not exist: {root}")

    manifest_path, manifest_rel = _contract_inside(root, manifest_file, label="manifest")
    study_path, study_rel = _contract_inside(root, study_file, label="study")
    search_path, search_rel = _contract_inside(
        root, discovery_search_file, label="discovery search"
    )
    calibration_path, calibration_rel = _contract_inside(
        root, calibration_file, label="scale calibration"
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset_fp = manifest.get("dataset_fingerprint_sha256")
    computed_dataset_fp = manifest_fingerprint(manifest)
    if not isinstance(dataset_fp, str) or dataset_fp != computed_dataset_fp:
        raise ValueError("manifest does not carry a valid self-verifying dataset fingerprint")

    study = load_study_json(study_path)
    study_fp = study_fingerprint(study)
    search = load_search_json(search_path)
    search_fp = search_fingerprint(search)
    calibration = load_scale_calibration_json(calibration_path)
    calibration_fp = scale_calibration_fingerprint(calibration)

    payload = {
        "schema_version": 1,
        "name": name,
        "manifest_file": manifest_rel,
        "dataset_fingerprint_sha256": dataset_fp,
        "study_file": study_rel,
        "study_fingerprint_sha256": study_fp,
        "discovery_search_file": search_rel,
        "discovery_search_fingerprint_sha256": search_fp,
        "calibration_file": calibration_rel,
        "calibration_fingerprint_sha256": calibration_fp,
    }

    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary research bundle output already exists: {temporary}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    try:
        verification = verify_research_bundle(temporary, data_directory=data_directory)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "schema_version": 1,
        "kind": "parameter_extract.research_bundle_seal",
        "output": str(output),
        "bundle": payload,
        "verification": verification,
    }


def _contract_inside(root: Path, path: str | Path, *, label: str) -> tuple[Path, str]:
    root_resolved = root.resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root_resolved / candidate).resolve()
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{label} contract escapes the research bundle directory") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} contract does not exist: {path}")
    return resolved, relative.as_posix()
