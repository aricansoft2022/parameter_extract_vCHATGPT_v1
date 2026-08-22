from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .deployment import (
    deployment_manifest_fingerprint,
    run_deployment_export,
    verify_deployment_manifest,
)
from .exchange_risk import verify_exchange_risk_result
from .manifest import sha256_file
from .risk import verify_risk_result
from .selection import verify_selection_result


def run_lineage_deployment_export(
    selection_result_path: str | Path,
    risk_result_path: str | Path,
    exchange_risk_result_path: str | Path,
    deployment_path: str | Path,
    *,
    teams_csv_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    selection_path = Path(selection_result_path)
    risk_path = Path(risk_result_path)
    exchange_path = Path(exchange_risk_result_path)

    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    risk = json.loads(risk_path.read_text(encoding="utf-8"))
    exchange = json.loads(exchange_path.read_text(encoding="utf-8"))

    selection_problems = verify_selection_result(selection)
    if selection_problems:
        raise ValueError(
            "selection-result verification failed: " + "; ".join(selection_problems)
        )
    risk_problems = verify_risk_result(risk)
    if risk_problems:
        raise ValueError("risk-result verification failed: " + "; ".join(risk_problems))
    exchange_problems = verify_exchange_risk_result(exchange)
    if exchange_problems:
        raise ValueError(
            "exchange-risk-result verification failed: " + "; ".join(exchange_problems)
        )

    actual_selection_sha = sha256_file(selection_path)
    actual_risk_sha = sha256_file(risk_path)
    if risk.get("source_selection_result_sha256") != actual_selection_sha:
        raise ValueError("risk result is pinned to a different selection-result file")
    if exchange.get("source_risk_result_sha256") != actual_risk_sha:
        raise ValueError("exchange-risk result is pinned to a different risk-result file")

    selected_fp = selection.get("selected_set_fingerprint_sha256")
    if risk.get("source_selected_set_fingerprint_sha256") != selected_fp:
        raise ValueError("risk result belongs to a different selected set")
    if exchange.get("source_selected_set_fingerprint_sha256") != selected_fp:
        raise ValueError("exchange-risk result belongs to a different selected set")
    if risk.get("status") != "RISK_BUDGET_PASS":
        raise ValueError("deployment export requires RISK_BUDGET_PASS")

    manifest = run_deployment_export(
        selection_path,
        exchange_path,
        deployment_path,
        teams_csv_path=teams_csv_path,
        manifest_path=manifest_path,
    )

    manifest.pop("deployment_manifest_fingerprint_sha256", None)
    manifest.update(
        {
            "source_risk_result_sha256": actual_risk_sha,
            "source_holdout_result_sha256": risk["source_holdout_result_sha256"],
            "risk_fingerprint_sha256": risk["risk_fingerprint_sha256"],
            "study_fingerprint_sha256": risk["study_fingerprint_sha256"],
            "dataset_fingerprint_sha256": risk["dataset_fingerprint_sha256"],
            "execution": risk["execution"],
            "complete_artifact_lineage_checked": True,
        }
    )
    manifest["deployment_manifest_fingerprint_sha256"] = (
        deployment_manifest_fingerprint(manifest)
    )
    problems = verify_deployment_manifest(
        manifest,
        csv_bytes=Path(teams_csv_path).read_bytes(),
    )
    if problems:
        raise RuntimeError(
            "lineage-enriched deployment manifest failed self-verification: "
            + "; ".join(problems)
        )
    Path(manifest_path).write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return manifest
