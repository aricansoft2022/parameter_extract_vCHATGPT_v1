from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .manifest import sha256_file
from .models import StrategySpec
from .portfolio import _Candidate, _aggregate_portfolio_windows, _run_portfolio_window
from .promotion import candidate_fingerprint
from .selection import verify_selection_result
from .study import StudyContext, load_study_context, study_fingerprint

HOLDOUT_SCHEMA_VERSION = 1
HOLDOUT_POLICY = "predeclared_sealed_holdout_gates_v1"


@dataclass(frozen=True, slots=True)
class HoldoutGates:
    min_total_closed_trades: int
    min_positive_window_fraction: float
    min_fixed_baseline_total_return_pct: float | None = None
    min_median_window_return_pct: float | None = None
    min_worst_window_return_pct: float | None = None
    min_worst_within_window_closed_drawdown_pct: float | None = None
    max_open_at_end_windows: int | None = None
    max_pending_at_end_windows: int | None = None

    def __post_init__(self) -> None:
        if self.min_total_closed_trades < 0:
            raise ValueError("min_total_closed_trades cannot be negative")
        if not math.isfinite(float(self.min_positive_window_fraction)) or not (
            0.0 <= self.min_positive_window_fraction <= 1.0
        ):
            raise ValueError("min_positive_window_fraction must be finite and inside [0, 1]")
        for name in (
            "min_fixed_baseline_total_return_pct",
            "min_median_window_return_pct",
            "min_worst_window_return_pct",
            "min_worst_within_window_closed_drawdown_pct",
        ):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite when supplied")
        for name in ("max_open_at_end_windows", "max_pending_at_end_windows"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class HoldoutSpec:
    name: str
    source_selection_result_sha256: str
    source_selected_set_fingerprint_sha256: str
    gates: HoldoutGates

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("holdout name cannot be empty")
        _validate_digest(self.source_selection_result_sha256)
        _validate_digest(self.source_selected_set_fingerprint_sha256)


def load_holdout_json(path: str | Path) -> HoldoutSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != HOLDOUT_SCHEMA_VERSION:
        raise ValueError("unsupported holdout schema_version")
    gates = payload["gates"]
    return HoldoutSpec(
        name=str(payload["name"]),
        source_selection_result_sha256=str(
            payload["source_selection_result_sha256"]
        ).lower(),
        source_selected_set_fingerprint_sha256=str(
            payload["source_selected_set_fingerprint_sha256"]
        ).lower(),
        gates=HoldoutGates(
            min_total_closed_trades=int(gates["min_total_closed_trades"]),
            min_positive_window_fraction=float(gates["min_positive_window_fraction"]),
            min_fixed_baseline_total_return_pct=_optional_float(
                gates.get("min_fixed_baseline_total_return_pct")
            ),
            min_median_window_return_pct=_optional_float(
                gates.get("min_median_window_return_pct")
            ),
            min_worst_window_return_pct=_optional_float(
                gates.get("min_worst_window_return_pct")
            ),
            min_worst_within_window_closed_drawdown_pct=_optional_float(
                gates.get("min_worst_within_window_closed_drawdown_pct")
            ),
            max_open_at_end_windows=_optional_int(gates.get("max_open_at_end_windows")),
            max_pending_at_end_windows=_optional_int(
                gates.get("max_pending_at_end_windows")
            ),
        ),
    )


def holdout_fingerprint(spec: HoldoutSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_holdout(
    study_path: str | Path,
    selection_result_path: str | Path,
    holdout_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    if not context.spec.holdout:
        raise ValueError("study contains no sealed holdout windows")

    selection_path = Path(selection_result_path)
    selection_result = json.loads(selection_path.read_text(encoding="utf-8"))
    spec = load_holdout_json(holdout_path)
    actual_selection_sha = sha256_file(selection_path)
    if spec.source_selection_result_sha256 != actual_selection_sha:
        raise ValueError("holdout contract is pinned to a different selection-result file")

    problems = verify_selection_result(selection_result)
    if problems:
        raise ValueError("selection-result verification failed: " + "; ".join(problems))
    if (
        selection_result.get("selected_set_fingerprint_sha256")
        != spec.source_selected_set_fingerprint_sha256
    ):
        raise ValueError("holdout contract is pinned to a different selected-set fingerprint")

    current_study_fp = study_fingerprint(context.spec)
    if selection_result.get("study_fingerprint_sha256") != current_study_fp:
        raise ValueError("selection result belongs to a different study contract")
    if (
        selection_result.get("dataset_fingerprint_sha256")
        != context.spec.dataset_fingerprint_sha256
    ):
        raise ValueError("selection result belongs to a different dataset")
    if selection_result.get("symbol") != context.spec.symbol:
        raise ValueError("selection-result symbol does not match the study")
    if selection_result.get("execution") != asdict(context.spec.execution):
        raise ValueError("selection-result execution assumptions do not match the study")

    candidates = _candidates_from_selection(selection_result)
    slot_count = int(selection_result["slot_count"])
    windows = [
        _run_portfolio_window(
            "holdout",
            window,
            context,
            candidates,
            slot_count=slot_count,
        )
        for window in context.spec.holdout
    ]
    aggregate = _aggregate_portfolio_windows(windows, slot_count=slot_count)
    evaluation = _holdout_evaluation(windows, aggregate)
    failures = _holdout_failures(evaluation, spec.gates)
    status = "PASS" if not failures else "FAIL"

    payload = {
        "schema_version": 1,
        "kind": "parameter_extract.sealed_holdout_result",
        "holdout": spec.name,
        "holdout_fingerprint_sha256": holdout_fingerprint(spec),
        "holdout_spec": asdict(spec),
        "source_selection_result_sha256": actual_selection_sha,
        "source_selected_set_fingerprint_sha256": spec.source_selected_set_fingerprint_sha256,
        "study_fingerprint_sha256": current_study_fp,
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "symbol": context.spec.symbol,
        "execution": asdict(context.spec.execution),
        "holdout_policy": HOLDOUT_POLICY,
        "strategy_parameters_retuned": False,
        "selection_gates_retuned": False,
        "selected_set_changed": False,
        "slot_count_changed": False,
        "priority_reoptimized": False,
        "leverage_applied": False,
        "evaluator_discovery_accessed": False,
        "evaluator_validation_accessed": False,
        "holdout_accessed": True,
        "slot_count": slot_count,
        "selected_count": len(candidates),
        "selected": [
            {
                "priority": row.priority,
                "family_id": row.family_id,
                "candidate_fingerprint_sha256": row.fingerprint,
                "strategy": asdict(row.strategy),
            }
            for row in candidates
        ],
        "status": status,
        "failure_reasons": failures,
        "evaluation": evaluation,
        "aggregate": aggregate,
        "windows": windows,
    }
    self_problems = verify_holdout_result(payload)
    if self_problems:
        raise RuntimeError(
            "generated holdout result failed self-verification: "
            + "; ".join(self_problems)
        )
    return payload


def verify_holdout_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported holdout-result schema_version")
    if payload.get("kind") != "parameter_extract.sealed_holdout_result":
        problems.append("holdout-result kind is invalid")
    if payload.get("holdout_policy") != HOLDOUT_POLICY:
        problems.append("holdout policy is unsupported")
    for flag in (
        "strategy_parameters_retuned",
        "selection_gates_retuned",
        "selected_set_changed",
        "slot_count_changed",
        "priority_reoptimized",
        "leverage_applied",
        "evaluator_discovery_accessed",
        "evaluator_validation_accessed",
    ):
        if payload.get(flag) is not False:
            problems.append(f"{flag} must be false")
    if payload.get("holdout_accessed") is not True:
        problems.append("holdout_accessed must be true")

    spec_payload = payload.get("holdout_spec")
    spec: HoldoutSpec | None = None
    if not isinstance(spec_payload, dict):
        problems.append("holdout_spec is missing or invalid")
    else:
        try:
            gates = HoldoutGates(**spec_payload["gates"])
            spec = HoldoutSpec(
                name=str(spec_payload["name"]),
                source_selection_result_sha256=str(
                    spec_payload["source_selection_result_sha256"]
                ),
                source_selected_set_fingerprint_sha256=str(
                    spec_payload["source_selected_set_fingerprint_sha256"]
                ),
                gates=gates,
            )
            if payload.get("holdout_fingerprint_sha256") != holdout_fingerprint(spec):
                problems.append("holdout fingerprint does not match holdout_spec")
            if (
                payload.get("source_selection_result_sha256")
                != spec.source_selection_result_sha256
            ):
                problems.append("holdout source selection-result SHA is inconsistent")
            if (
                payload.get("source_selected_set_fingerprint_sha256")
                != spec.source_selected_set_fingerprint_sha256
            ):
                problems.append("holdout selected-set fingerprint is inconsistent")
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"holdout_spec is invalid: {exc}")

    slot_count = payload.get("slot_count")
    if not isinstance(slot_count, int) or slot_count < 1:
        problems.append("holdout slot_count is invalid")
        return problems

    selected = payload.get("selected")
    if not isinstance(selected, list) or not selected:
        problems.append("holdout selected set is missing or empty")
        return problems
    if payload.get("selected_count") != len(selected):
        problems.append("selected_count does not match selected rows")
    seen_fps: set[str] = set()
    for index, row in enumerate(selected):
        try:
            strategy = StrategySpec(**row["strategy"])
            fingerprint = candidate_fingerprint(strategy)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"selected row {index}: invalid strategy: {exc}")
            continue
        if row.get("priority") != index + 1:
            problems.append(f"selected row {index}: priority is inconsistent")
        if row.get("candidate_fingerprint_sha256") != fingerprint:
            problems.append(f"selected row {index}: strategy fingerprint mismatch")
        if fingerprint in seen_fps:
            problems.append(f"selected row {index}: duplicate strategy fingerprint")
        seen_fps.add(fingerprint)

    windows = payload.get("windows")
    if not isinstance(windows, list) or not windows:
        problems.append("holdout windows are missing or empty")
        return problems
    if any(row.get("phase") != "holdout" for row in windows if isinstance(row, dict)):
        problems.append("holdout result contains a non-holdout window")
    try:
        recomputed_aggregate = _aggregate_portfolio_windows(
            windows,
            slot_count=slot_count,
        )
        recomputed_evaluation = _holdout_evaluation(windows, recomputed_aggregate)
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"holdout windows are invalid: {exc}")
        return problems
    if payload.get("aggregate") != recomputed_aggregate:
        problems.append("holdout aggregate does not match stored windows")
    if payload.get("evaluation") != recomputed_evaluation:
        problems.append("holdout evaluation does not match stored windows")

    if spec is not None:
        failures = _holdout_failures(recomputed_evaluation, spec.gates)
        expected_status = "PASS" if not failures else "FAIL"
        if payload.get("failure_reasons") != failures:
            problems.append("holdout failure reasons do not match predeclared gates")
        if payload.get("status") != expected_status:
            problems.append("holdout status does not match predeclared gates")
    return problems


def _candidates_from_selection(selection_result: dict[str, Any]) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    for index, row in enumerate(selection_result["selected"]):
        if row.get("priority") != index + 1:
            raise ValueError("selection result compact priority is inconsistent")
        strategy = StrategySpec(**row["strategy"])
        fingerprint = candidate_fingerprint(strategy)
        if row.get("candidate_fingerprint_sha256") != fingerprint:
            raise ValueError("selection result strategy fingerprint mismatch")
        candidates.append(
            _Candidate(
                family_id=str(row["family_id"]),
                fingerprint=fingerprint,
                strategy=strategy,
                priority=index + 1,
            )
        )
    return tuple(candidates)


def _holdout_evaluation(
    windows: Sequence[dict[str, Any]],
    aggregate: dict[str, Any],
) -> dict[str, Any]:
    returns = [float(row["fixed_baseline_portfolio_return_pct"]) for row in windows]
    return {
        "window_count": len(windows),
        "positive_window_fraction": (
            0.0 if not windows else sum(value > 0.0 for value in returns) / len(windows)
        ),
        "fixed_baseline_total_return_pct": float(
            aggregate["fixed_baseline_total_return_pct"]
        ),
        "median_window_return_pct": float(aggregate["median_window_return_pct"]),
        "worst_window_return_pct": float(aggregate["worst_window_return_pct"]),
        "worst_within_window_closed_drawdown_pct": float(
            aggregate["worst_within_window_closed_drawdown_pct"]
        ),
        "closed_trade_count": int(aggregate["closed_trade_count"]),
        "open_at_end_windows": int(aggregate["open_at_end_windows"]),
        "pending_at_end_windows": int(aggregate["pending_at_end_windows"]),
        "accepted_entry_count": int(aggregate["accepted_entry_count"]),
        "blocked_no_slot_count": int(aggregate["blocked_no_slot_count"]),
        "slot_contention_fraction_of_raw_signals": float(
            aggregate["slot_contention_fraction_of_raw_signals"]
        ),
    }


def _holdout_failures(
    evaluation: dict[str, Any],
    gates: HoldoutGates,
) -> list[str]:
    failures: list[str] = []
    if evaluation["closed_trade_count"] < gates.min_total_closed_trades:
        failures.append("INSUFFICIENT_CLOSED_TRADES")
    if evaluation["positive_window_fraction"] < gates.min_positive_window_fraction:
        failures.append("POSITIVE_WINDOW_FRACTION")
    if (
        gates.min_fixed_baseline_total_return_pct is not None
        and evaluation["fixed_baseline_total_return_pct"]
        < gates.min_fixed_baseline_total_return_pct
    ):
        failures.append("TOTAL_RETURN")
    if (
        gates.min_median_window_return_pct is not None
        and evaluation["median_window_return_pct"] < gates.min_median_window_return_pct
    ):
        failures.append("MEDIAN_WINDOW_RETURN")
    if (
        gates.min_worst_window_return_pct is not None
        and evaluation["worst_window_return_pct"] < gates.min_worst_window_return_pct
    ):
        failures.append("WORST_WINDOW_RETURN")
    if (
        gates.min_worst_within_window_closed_drawdown_pct is not None
        and evaluation["worst_within_window_closed_drawdown_pct"]
        < gates.min_worst_within_window_closed_drawdown_pct
    ):
        failures.append("WORST_WITHIN_WINDOW_DRAWDOWN")
    if (
        gates.max_open_at_end_windows is not None
        and evaluation["open_at_end_windows"] > gates.max_open_at_end_windows
    ):
        failures.append("OPEN_AT_END_WINDOWS")
    if (
        gates.max_pending_at_end_windows is not None
        and evaluation["pending_at_end_windows"] > gates.max_pending_at_end_windows
    ):
        failures.append("PENDING_AT_END_WINDOWS")
    return failures


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc
