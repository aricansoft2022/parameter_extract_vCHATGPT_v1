from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .holdout import verify_holdout_result
from .manifest import sha256_file
from .selection import verify_selection_result

RISK_SCHEMA_VERSION = 1
RISK_METHOD = "conservative_mae_budget_v1"


@dataclass(frozen=True, slots=True)
class RiskSpec:
    name: str
    source_holdout_result_sha256: str
    max_leverage_cap: int
    mae_stress_multiplier: float
    extra_adverse_move_pct: float
    required_headroom_pct: float
    allocation_pct: float
    reserve_pct: float
    min_total_closed_trades: int
    min_closed_trades_per_family: int
    max_stressed_adverse_move_pct: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("risk name cannot be empty")
        _validate_digest(self.source_holdout_result_sha256)
        if not 1 <= self.max_leverage_cap <= 125:
            raise ValueError("max_leverage_cap must be inside [1, 125]")
        if not math.isfinite(self.mae_stress_multiplier) or self.mae_stress_multiplier < 1.0:
            raise ValueError("mae_stress_multiplier must be finite and >= 1")
        for name in ("extra_adverse_move_pct", "required_headroom_pct"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.required_headroom_pct <= 0.0:
            raise ValueError("required_headroom_pct must be positive")
        if not math.isfinite(self.allocation_pct) or not 0.0 < self.allocation_pct <= 100.0:
            raise ValueError("allocation_pct must be finite and inside (0, 100]")
        if not math.isfinite(self.reserve_pct) or not 0.0 <= self.reserve_pct < 100.0:
            raise ValueError("reserve_pct must be finite and inside [0, 100)")
        if self.min_total_closed_trades < 1:
            raise ValueError("min_total_closed_trades must be positive")
        if self.min_closed_trades_per_family < 1:
            raise ValueError("min_closed_trades_per_family must be positive")
        if self.max_stressed_adverse_move_pct is not None:
            value = float(self.max_stressed_adverse_move_pct)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError("max_stressed_adverse_move_pct must be finite and positive")


def load_risk_json(path: str | Path) -> RiskSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != RISK_SCHEMA_VERSION:
        raise ValueError("unsupported risk schema_version")
    return RiskSpec(
        name=str(payload["name"]),
        source_holdout_result_sha256=str(
            payload["source_holdout_result_sha256"]
        ).lower(),
        max_leverage_cap=int(payload["max_leverage_cap"]),
        mae_stress_multiplier=float(payload["mae_stress_multiplier"]),
        extra_adverse_move_pct=float(payload["extra_adverse_move_pct"]),
        required_headroom_pct=float(payload["required_headroom_pct"]),
        allocation_pct=float(payload["allocation_pct"]),
        reserve_pct=float(payload["reserve_pct"]),
        min_total_closed_trades=int(payload["min_total_closed_trades"]),
        min_closed_trades_per_family=int(payload["min_closed_trades_per_family"]),
        max_stressed_adverse_move_pct=_optional_float(
            payload.get("max_stressed_adverse_move_pct")
        ),
    )


def risk_fingerprint(spec: RiskSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_risk(
    selection_result_path: str | Path,
    holdout_result_path: str | Path,
    risk_path: str | Path,
) -> dict[str, Any]:
    selection_path = Path(selection_result_path)
    holdout_path = Path(holdout_result_path)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
    spec = load_risk_json(risk_path)

    selection_problems = verify_selection_result(selection)
    if selection_problems:
        raise ValueError("selection-result verification failed: " + "; ".join(selection_problems))
    holdout_problems = verify_holdout_result(holdout)
    if holdout_problems:
        raise ValueError("holdout-result verification failed: " + "; ".join(holdout_problems))

    actual_holdout_sha = sha256_file(holdout_path)
    if spec.source_holdout_result_sha256 != actual_holdout_sha:
        raise ValueError("risk contract is pinned to a different holdout-result file")
    actual_selection_sha = sha256_file(selection_path)
    if holdout.get("source_selection_result_sha256") != actual_selection_sha:
        raise ValueError("holdout result is pinned to a different selection-result file")
    if holdout.get("status") != "PASS":
        raise ValueError("risk assessment requires a PASS sealed holdout result")
    if (
        holdout.get("source_selected_set_fingerprint_sha256")
        != selection.get("selected_set_fingerprint_sha256")
    ):
        raise ValueError("holdout and selection selected-set fingerprints disagree")
    if holdout.get("selected") != selection.get("selected"):
        raise ValueError("holdout and selection selected rows disagree")
    for field in (
        "study_fingerprint_sha256",
        "dataset_fingerprint_sha256",
        "symbol",
        "execution",
        "slot_count",
    ):
        if holdout.get(field) != selection.get(field):
            raise ValueError(f"holdout and selection {field} disagree")

    slot_count = int(holdout["slot_count"])
    implied_slots = math.floor(100.0 / spec.allocation_pct + 1e-12)
    if implied_slots != slot_count:
        raise ValueError(
            f"allocation_pct implies {implied_slots} live slots but research used {slot_count}"
        )

    evidence = _collect_trade_evidence(selection, holdout)
    summary = _summarize_evidence(
        evidence,
        selected=holdout["selected"],
        spec=spec,
    )
    failures = _risk_failures(summary, spec)
    status = "RISK_BUDGET_PASS" if not failures else "BLOCK"

    payload = {
        "schema_version": 1,
        "kind": "parameter_extract.risk_budget",
        "risk": spec.name,
        "risk_fingerprint_sha256": risk_fingerprint(spec),
        "risk_spec": asdict(spec),
        "source_holdout_result_sha256": actual_holdout_sha,
        "source_selection_result_sha256": actual_selection_sha,
        "source_selected_set_fingerprint_sha256": holdout[
            "source_selected_set_fingerprint_sha256"
        ],
        "study_fingerprint_sha256": holdout["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": holdout["dataset_fingerprint_sha256"],
        "symbol": holdout["symbol"],
        "execution": holdout["execution"],
        "risk_method": RISK_METHOD,
        "alpha_parameters_retuned": False,
        "selected_set_changed": False,
        "priority_reoptimized": False,
        "leverage_optimized": False,
        "holdout_reused_for_alpha_tuning": False,
        "exchange_liquidation_validated": False,
        "teams_export_ready": False,
        "slot_count": slot_count,
        "allocation_pct": spec.allocation_pct,
        "reserve_pct": spec.reserve_pct,
        "status": status,
        "failure_reasons": failures,
        "summary": summary,
        "trade_evidence": evidence,
    }
    problems = verify_risk_result(payload)
    if problems:
        raise RuntimeError(
            "generated risk result failed self-verification: " + "; ".join(problems)
        )
    return payload


def verify_risk_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported risk-result schema_version")
    if payload.get("kind") != "parameter_extract.risk_budget":
        problems.append("risk-result kind is invalid")
    if payload.get("risk_method") != RISK_METHOD:
        problems.append("risk method is unsupported")
    for flag in (
        "alpha_parameters_retuned",
        "selected_set_changed",
        "priority_reoptimized",
        "leverage_optimized",
        "holdout_reused_for_alpha_tuning",
        "exchange_liquidation_validated",
        "teams_export_ready",
    ):
        if payload.get(flag) is not False:
            problems.append(f"{flag} must be false")

    spec_payload = payload.get("risk_spec")
    spec: RiskSpec | None = None
    if not isinstance(spec_payload, dict):
        problems.append("risk_spec is missing or invalid")
    else:
        try:
            spec = RiskSpec(**spec_payload)
            if payload.get("risk_fingerprint_sha256") != risk_fingerprint(spec):
                problems.append("risk fingerprint does not match risk_spec")
            if (
                payload.get("source_holdout_result_sha256")
                != spec.source_holdout_result_sha256
            ):
                problems.append("risk source holdout-result SHA is inconsistent")
        except (TypeError, ValueError) as exc:
            problems.append(f"risk_spec is invalid: {exc}")

    evidence = payload.get("trade_evidence")
    if not isinstance(evidence, list):
        problems.append("trade_evidence is missing or invalid")
        return problems
    selected_snapshot = payload.get("summary", {}).get("families")
    if not isinstance(selected_snapshot, list):
        problems.append("risk family summary is missing or invalid")
        return problems
    try:
        selected_stub = [
            {
                "family_id": row["family_id"],
                "candidate_fingerprint_sha256": row["candidate_fingerprint_sha256"],
            }
            for row in selected_snapshot
        ]
        recomputed = _summarize_evidence(evidence, selected=selected_stub, spec=spec)
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"risk evidence cannot be summarized: {exc}")
        return problems
    if payload.get("summary") != recomputed:
        problems.append("risk summary does not match stored trade evidence")
    if spec is not None:
        failures = _risk_failures(recomputed, spec)
        expected = "RISK_BUDGET_PASS" if not failures else "BLOCK"
        if payload.get("failure_reasons") != failures:
            problems.append("risk failure reasons do not match policy")
        if payload.get("status") != expected:
            problems.append("risk status does not match policy")
    return problems


def _collect_trade_evidence(
    selection: dict[str, Any],
    holdout: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for source, windows in (
        ("development", selection["selected_portfolio_windows"]),
        ("holdout", holdout["windows"]),
    ):
        for window in windows:
            for trade in window.get("trades", []):
                mae = float(trade["mae_pct"])
                holding = float(trade["holding_minutes"])
                if not math.isfinite(mae) or not math.isfinite(holding):
                    raise ValueError("trade MAE/holding evidence must be finite")
                evidence.append(
                    {
                        "source": source,
                        "phase": str(window["phase"]),
                        "window": str(window["name"]),
                        "family_id": str(trade["family_id"]),
                        "candidate_fingerprint_sha256": str(
                            trade["candidate_fingerprint_sha256"]
                        ),
                        "mae_pct": mae,
                        "adverse_move_pct": max(0.0, -mae),
                        "holding_minutes": holding,
                    }
                )
    return evidence


def _summarize_evidence(
    evidence: Sequence[dict[str, Any]],
    *,
    selected: Sequence[dict[str, Any]],
    spec: RiskSpec | None,
) -> dict[str, Any]:
    if spec is None:
        raise ValueError("risk spec is required to summarize evidence")
    selected_rows = [
        {
            "family_id": str(row["family_id"]),
            "candidate_fingerprint_sha256": str(
                row["candidate_fingerprint_sha256"]
            ),
        }
        for row in selected
    ]
    families: list[dict[str, Any]] = []
    all_adverse: list[float] = []
    all_holding: list[float] = []
    for selected_row in selected_rows:
        family_rows = [
            row
            for row in evidence
            if row["family_id"] == selected_row["family_id"]
            and row["candidate_fingerprint_sha256"]
            == selected_row["candidate_fingerprint_sha256"]
        ]
        adverse = [float(row["adverse_move_pct"]) for row in family_rows]
        holding = [float(row["holding_minutes"]) for row in family_rows]
        all_adverse.extend(adverse)
        all_holding.extend(holding)
        worst = max(adverse) if adverse else None
        stressed = (
            None
            if worst is None
            else worst * spec.mae_stress_multiplier + spec.extra_adverse_move_pct
        )
        required_budget = (
            None if stressed is None else stressed + spec.required_headroom_pct
        )
        ceiling = _leverage_ceiling(required_budget)
        families.append(
            {
                **selected_row,
                "closed_trade_count": len(family_rows),
                "worst_adverse_move_pct": worst,
                "p95_adverse_move_pct": _nearest_rank(adverse, 0.95),
                "max_holding_minutes": max(holding) if holding else None,
                "stressed_adverse_move_pct": stressed,
                "required_adverse_budget_pct": required_budget,
                "mae_budget_leverage_ceiling": ceiling,
            }
        )

    worst_all = max(all_adverse) if all_adverse else None
    stressed_all = (
        None
        if worst_all is None
        else worst_all * spec.mae_stress_multiplier + spec.extra_adverse_move_pct
    )
    required_all = (
        None if stressed_all is None else stressed_all + spec.required_headroom_pct
    )
    ceiling_all = _leverage_ceiling(required_all)
    provisional = (
        None
        if ceiling_all is None or ceiling_all < 1
        else min(spec.max_leverage_cap, ceiling_all)
    )
    return {
        "evidence_scope": "selected_portfolio_discovery_validation_plus_sealed_holdout",
        "closed_trade_count": len(evidence),
        "development_trade_count": sum(row["source"] == "development" for row in evidence),
        "holdout_trade_count": sum(row["source"] == "holdout" for row in evidence),
        "worst_adverse_move_pct": worst_all,
        "p95_adverse_move_pct": _nearest_rank(all_adverse, 0.95),
        "max_holding_minutes": max(all_holding) if all_holding else None,
        "stressed_adverse_move_pct": stressed_all,
        "required_adverse_budget_pct": required_all,
        "mae_budget_leverage_ceiling": ceiling_all,
        "policy_max_leverage_cap": spec.max_leverage_cap,
        "provisional_deployment_leverage": provisional,
        "liquidation_model": "NOT_VALIDATED_MAE_BUDGET_ONLY",
        "families": families,
    }


def _risk_failures(summary: dict[str, Any], spec: RiskSpec) -> list[str]:
    failures: list[str] = []
    if summary["closed_trade_count"] < spec.min_total_closed_trades:
        failures.append("INSUFFICIENT_TOTAL_RISK_TRADES")
    for row in summary["families"]:
        if row["closed_trade_count"] < spec.min_closed_trades_per_family:
            failures.append(f"INSUFFICIENT_FAMILY_RISK_TRADES:{row['family_id']}")
    stressed = summary["stressed_adverse_move_pct"]
    if stressed is None:
        failures.append("NO_MAE_EVIDENCE")
    elif (
        spec.max_stressed_adverse_move_pct is not None
        and stressed > spec.max_stressed_adverse_move_pct
    ):
        failures.append("STRESSED_ADVERSE_MOVE")
    ceiling = summary["mae_budget_leverage_ceiling"]
    if ceiling is None or ceiling < 1:
        failures.append("NO_POSITIVE_MAE_BUDGET_LEVERAGE")
    return failures


def _leverage_ceiling(required_budget_pct: float | None) -> int | None:
    if required_budget_pct is None:
        return None
    if required_budget_pct <= 0.0:
        return 125
    return math.floor(100.0 / required_budget_pct)


def _nearest_rank(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc
