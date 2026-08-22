from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .manifest import sha256_file
from .models import StrategySpec
from .study import evaluate_strategy, load_study_context, study_fingerprint

CANDIDATE_SET_SCHEMA_VERSION = 1
VALIDATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ValidationGates:
    min_total_trades: int
    min_positive_window_fraction: float
    min_median_window_return_pct: float | None = None
    min_worst_window_return_pct: float | None = None
    min_worst_mae_pct: float | None = None
    max_open_at_end_windows: int | None = None

    def __post_init__(self) -> None:
        if self.min_total_trades < 1:
            raise ValueError("min_total_trades must be positive")
        if not 0.0 <= self.min_positive_window_fraction <= 1.0:
            raise ValueError("min_positive_window_fraction must be inside [0, 1]")
        if self.max_open_at_end_windows is not None and self.max_open_at_end_windows < 0:
            raise ValueError("max_open_at_end_windows cannot be negative")


@dataclass(frozen=True, slots=True)
class ValidationSpec:
    name: str
    source_candidate_set_fingerprint_sha256: str
    gates: ValidationGates

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("validation name cannot be empty")
        _validate_digest(self.source_candidate_set_fingerprint_sha256)


def candidate_fingerprint(strategy: StrategySpec | dict[str, Any]) -> str:
    normalized = strategy if isinstance(strategy, StrategySpec) else StrategySpec(**strategy)
    canonical = json.dumps(
        asdict(normalized), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def candidate_set_fingerprint(payload: dict[str, Any]) -> str:
    stable = {
        "schema_version": payload.get("schema_version"),
        "kind": payload.get("kind"),
        "source": payload.get("source"),
        "parameters_frozen": payload.get("parameters_frozen"),
        "candidates": payload.get("candidates"),
    }
    canonical = json.dumps(
        stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def freeze_discovery_result(path: str | Path) -> dict[str, Any]:
    source_path = Path(path)
    result = json.loads(source_path.read_text(encoding="utf-8"))
    if result.get("kind") != "parameter_extract.discovery_search":
        raise ValueError("input is not a discovery search result")
    if result.get("phase_used") != "discovery":
        raise ValueError("search result was not produced from discovery phase only")
    if result.get("validation_accessed") is not False:
        raise ValueError("search result indicates validation access")
    if result.get("holdout_accessed") is not False:
        raise ValueError("search result indicates holdout access")
    frontier = result.get("frontier")
    if not isinstance(frontier, list) or not frontier:
        raise ValueError("discovery result has no Pareto candidates to freeze")

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in frontier:
        strategy = StrategySpec(**row["strategy"])
        fingerprint = candidate_fingerprint(strategy)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(
            {
                "candidate_fingerprint_sha256": fingerprint,
                "strategy": asdict(strategy),
                "discovery": {
                    "stage": row.get("stage"),
                    "aggregate": row.get("aggregate"),
                    "windows": row.get("windows"),
                },
            }
        )
    candidates.sort(key=lambda row: row["candidate_fingerprint_sha256"])

    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_SET_SCHEMA_VERSION,
        "kind": "parameter_extract.frozen_candidate_set",
        "source": {
            "search_result_sha256": sha256_file(source_path),
            "search_fingerprint_sha256": result.get("search_fingerprint_sha256"),
            "study_fingerprint_sha256": result.get("study_fingerprint_sha256"),
            "dataset_fingerprint_sha256": result.get("dataset_fingerprint_sha256"),
            "symbol": result.get("symbol"),
            "execution": result.get("execution"),
            "pareto_objectives": result.get("pareto_objectives"),
        },
        "parameters_frozen": True,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    payload["candidate_set_fingerprint_sha256"] = candidate_set_fingerprint(payload)
    return payload


def verify_candidate_set(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != CANDIDATE_SET_SCHEMA_VERSION:
        problems.append("unsupported candidate-set schema_version")
    if payload.get("kind") != "parameter_extract.frozen_candidate_set":
        problems.append("candidate-set kind is invalid")
    if payload.get("parameters_frozen") is not True:
        problems.append("candidate set is not marked parameters_frozen")
    expected = payload.get("candidate_set_fingerprint_sha256")
    actual = candidate_set_fingerprint(payload)
    if expected != actual:
        problems.append(
            f"candidate-set fingerprint mismatch: expected {expected}, computed {actual}"
        )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        problems.append("candidate list is missing or invalid")
        return problems
    if payload.get("candidate_count") != len(candidates):
        problems.append("candidate_count does not match candidate list length")
    seen: set[str] = set()
    for index, row in enumerate(candidates):
        try:
            strategy = StrategySpec(**row["strategy"])
            actual_candidate = candidate_fingerprint(strategy)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"candidate {index}: invalid strategy: {exc}")
            continue
        expected_candidate = row.get("candidate_fingerprint_sha256")
        if expected_candidate != actual_candidate:
            problems.append(
                f"candidate {index}: fingerprint mismatch: expected {expected_candidate}, "
                f"computed {actual_candidate}"
            )
        if actual_candidate in seen:
            problems.append(f"candidate {index}: duplicate strategy fingerprint")
        seen.add(actual_candidate)
    return problems


def load_validation_json(path: str | Path) -> ValidationSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != VALIDATION_SCHEMA_VERSION:
        raise ValueError("unsupported validation schema_version")
    gates = payload["gates"]
    return ValidationSpec(
        name=str(payload["name"]),
        source_candidate_set_fingerprint_sha256=str(
            payload["source_candidate_set_fingerprint_sha256"]
        ).lower(),
        gates=ValidationGates(
            min_total_trades=int(gates["min_total_trades"]),
            min_positive_window_fraction=float(gates["min_positive_window_fraction"]),
            min_median_window_return_pct=_optional_float(
                gates.get("min_median_window_return_pct")
            ),
            min_worst_window_return_pct=_optional_float(
                gates.get("min_worst_window_return_pct")
            ),
            min_worst_mae_pct=_optional_float(gates.get("min_worst_mae_pct")),
            max_open_at_end_windows=(
                None
                if gates.get("max_open_at_end_windows") is None
                else int(gates["max_open_at_end_windows"])
            ),
        ),
    )


def validation_fingerprint(spec: ValidationSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_validation(
    study_path: str | Path,
    candidate_set_path: str | Path,
    validation_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    candidate_set = json.loads(Path(candidate_set_path).read_text(encoding="utf-8"))
    problems = verify_candidate_set(candidate_set)
    if problems:
        raise ValueError("candidate-set verification failed: " + "; ".join(problems))
    validation = load_validation_json(validation_path)
    candidate_set_fp = candidate_set["candidate_set_fingerprint_sha256"]
    if validation.source_candidate_set_fingerprint_sha256 != candidate_set_fp:
        raise ValueError("validation contract is pinned to a different candidate set")

    source = candidate_set["source"]
    current_study_fp = study_fingerprint(context.spec)
    if source.get("study_fingerprint_sha256") != current_study_fp:
        raise ValueError("candidate set was frozen from a different study contract")
    if source.get("dataset_fingerprint_sha256") != context.spec.dataset_fingerprint_sha256:
        raise ValueError("candidate set was frozen from a different dataset")
    if source.get("symbol") != context.spec.symbol:
        raise ValueError("candidate-set symbol does not match the study")
    if source.get("execution") != asdict(context.spec.execution):
        raise ValueError("candidate-set execution assumptions do not match the study")

    rows: list[dict[str, Any]] = []
    promoted = 0
    for frozen in candidate_set["candidates"]:
        strategy = StrategySpec(**frozen["strategy"])
        result = evaluate_strategy(context, strategy, phases=("validation",))
        if result["phases_evaluated"] != ["validation"] or result["holdout_revealed"]:
            raise RuntimeError("validation phase isolation failed")
        windows = _compact_windows(result["windows"])
        aggregate = _aggregate(windows)
        reasons = _gate_failures(aggregate, validation.gates)
        status = "PASS" if not reasons else "REJECT"
        if status == "PASS":
            promoted += 1
        rows.append(
            {
                "candidate_fingerprint_sha256": frozen["candidate_fingerprint_sha256"],
                "strategy": frozen["strategy"],
                "discovery": frozen["discovery"],
                "validation": {
                    "aggregate": aggregate,
                    "windows": windows,
                },
                "promotion_status": status,
                "rejection_reasons": reasons,
            }
        )

    return {
        "schema_version": 1,
        "kind": "parameter_extract.validation_result",
        "validation": validation.name,
        "validation_fingerprint_sha256": validation_fingerprint(validation),
        "validation_spec": asdict(validation),
        "source_candidate_set_fingerprint_sha256": candidate_set_fp,
        "study_fingerprint_sha256": current_study_fp,
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "symbol": context.spec.symbol,
        "execution": asdict(context.spec.execution),
        "parameters_retuned": False,
        "discovery_accessed": False,
        "validation_accessed": True,
        "holdout_accessed": False,
        "candidate_count": len(rows),
        "promoted_count": promoted,
        "rejected_count": len(rows) - promoted,
        "candidates": rows,
    }


def _compact_windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for row in rows:
        metrics = row["metrics"]
        compact.append(
            {
                "name": row["name"],
                "return_pct": metrics["total_return_pct"],
                "trade_count": metrics["trade_count"],
                "worst_mae_pct": metrics["worst_mae_pct"],
                "drawdown_pct": metrics["max_closed_equity_drawdown_pct"],
                "max_holding_minutes": metrics["max_holding_minutes"],
                "open_at_end": metrics["open_at_end"],
            }
        )
    return compact


def _aggregate(windows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["return_pct"]) for row in windows]
    maes = [
        float(row["worst_mae_pct"])
        for row in windows
        if row["worst_mae_pct"] is not None
    ]
    drawdowns = [float(row["drawdown_pct"]) for row in windows]
    holding = [
        float(row["max_holding_minutes"])
        for row in windows
        if row["max_holding_minutes"] is not None
    ]
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value / 100.0
    ordered = sorted(returns)
    middle = len(ordered) // 2
    if not ordered:
        median = 0.0
    elif len(ordered) % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2.0
    return {
        "window_count": len(windows),
        "positive_window_fraction": (
            0.0 if not windows else sum(value > 0.0 for value in returns) / len(windows)
        ),
        "total_trades": sum(int(row["trade_count"]) for row in windows),
        "compounded_window_return_pct": (equity - 1.0) * 100.0,
        "median_window_return_pct": median,
        "worst_window_return_pct": min(returns) if returns else 0.0,
        "worst_mae_pct": min(maes) if maes else None,
        "max_drawdown_pct": min(drawdowns) if drawdowns else 0.0,
        "max_holding_minutes": max(holding) if holding else None,
        "open_at_end_windows": sum(bool(row["open_at_end"]) for row in windows),
    }


def _gate_failures(metrics: dict[str, Any], gates: ValidationGates) -> list[str]:
    reasons: list[str] = []
    if metrics["total_trades"] < gates.min_total_trades:
        reasons.append("INSUFFICIENT_TRADES")
    if metrics["positive_window_fraction"] < gates.min_positive_window_fraction:
        reasons.append("POSITIVE_WINDOW_FRACTION")
    if (
        gates.min_median_window_return_pct is not None
        and metrics["median_window_return_pct"] < gates.min_median_window_return_pct
    ):
        reasons.append("MEDIAN_WINDOW_RETURN")
    if (
        gates.min_worst_window_return_pct is not None
        and metrics["worst_window_return_pct"] < gates.min_worst_window_return_pct
    ):
        reasons.append("WORST_WINDOW_RETURN")
    if gates.min_worst_mae_pct is not None:
        mae = metrics["worst_mae_pct"]
        if mae is None or mae < gates.min_worst_mae_pct:
            reasons.append("WORST_MAE")
    if (
        gates.max_open_at_end_windows is not None
        and metrics["open_at_end_windows"] > gates.max_open_at_end_windows
    ):
        reasons.append("OPEN_AT_END_WINDOWS")
    return reasons


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
