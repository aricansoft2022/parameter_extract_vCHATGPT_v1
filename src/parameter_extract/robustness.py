from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .manifest import sha256_file
from .models import StrategySpec
from .study import evaluate_strategy, load_study_context, study_fingerprint

ROBUSTNESS_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class NeighborhoodSteps:
    include_rsi_period: bool
    rsi_entry: float
    adx_min: float
    adx_max: float
    tp_price_pct: float
    rsi_exit: float

    def __post_init__(self) -> None:
        for name in ("rsi_entry", "adx_min", "adx_max", "tp_price_pct", "rsi_exit"):
            if float(getattr(self, name)) <= 0.0:
                raise ValueError(f"{name} neighborhood step must be positive")


@dataclass(frozen=True, slots=True)
class RobustnessGates:
    min_neighbor_count: int
    min_validation_pass_fraction: float
    min_discovery_positive_fraction: float
    max_center_validation_advantage_pct: float | None = None

    def __post_init__(self) -> None:
        if self.min_neighbor_count < 1:
            raise ValueError("min_neighbor_count must be positive")
        for name in ("min_validation_pass_fraction", "min_discovery_positive_fraction"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be inside [0, 1]")
        if (
            self.max_center_validation_advantage_pct is not None
            and self.max_center_validation_advantage_pct < 0.0
        ):
            raise ValueError("max_center_validation_advantage_pct cannot be negative")


@dataclass(frozen=True, slots=True)
class RobustnessSpec:
    name: str
    source_validation_result_sha256: str
    steps: NeighborhoodSteps
    gates: RobustnessGates
    max_neighbor_evaluations: int = 10_000

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("robustness name cannot be empty")
        _validate_digest(self.source_validation_result_sha256)
        if self.max_neighbor_evaluations < 1:
            raise ValueError("max_neighbor_evaluations must be positive")


def load_robustness_json(path: str | Path) -> RobustnessSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != ROBUSTNESS_SCHEMA_VERSION:
        raise ValueError("unsupported robustness schema_version")
    steps = payload["steps"]
    gates = payload["gates"]
    return RobustnessSpec(
        name=str(payload["name"]),
        source_validation_result_sha256=str(
            payload["source_validation_result_sha256"]
        ).lower(),
        steps=NeighborhoodSteps(
            include_rsi_period=bool(steps.get("include_rsi_period", True)),
            rsi_entry=float(steps["rsi_entry"]),
            adx_min=float(steps["adx_min"]),
            adx_max=float(steps["adx_max"]),
            tp_price_pct=float(steps["tp_price_pct"]),
            rsi_exit=float(steps["rsi_exit"]),
        ),
        gates=RobustnessGates(
            min_neighbor_count=int(gates["min_neighbor_count"]),
            min_validation_pass_fraction=float(gates["min_validation_pass_fraction"]),
            min_discovery_positive_fraction=float(gates["min_discovery_positive_fraction"]),
            max_center_validation_advantage_pct=_optional_float(
                gates.get("max_center_validation_advantage_pct")
            ),
        ),
        max_neighbor_evaluations=int(payload.get("max_neighbor_evaluations", 10_000)),
    )


def robustness_fingerprint(spec: RobustnessSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_robustness(
    study_path: str | Path,
    validation_result_path: str | Path,
    robustness_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    validation_path = Path(validation_result_path)
    validation_result = json.loads(validation_path.read_text(encoding="utf-8"))
    spec = load_robustness_json(robustness_path)
    actual_validation_sha = sha256_file(validation_path)
    if spec.source_validation_result_sha256 != actual_validation_sha:
        raise ValueError("robustness contract is pinned to a different validation-result file")
    if validation_result.get("kind") != "parameter_extract.validation_result":
        raise ValueError("input is not a validation result")
    if validation_result.get("parameters_retuned") is not False:
        raise ValueError("validation result does not prove parameters stayed frozen")
    if validation_result.get("holdout_accessed") is not False:
        raise ValueError("validation result indicates holdout access")
    if validation_result.get("study_fingerprint_sha256") != study_fingerprint(context.spec):
        raise ValueError("validation result belongs to a different study contract")
    if validation_result.get("dataset_fingerprint_sha256") != context.spec.dataset_fingerprint_sha256:
        raise ValueError("validation result belongs to a different dataset")
    if validation_result.get("symbol") != context.spec.symbol:
        raise ValueError("validation-result symbol does not match the study")
    if validation_result.get("execution") != asdict(context.spec.execution):
        raise ValueError("validation-result execution assumptions do not match the study")

    centers = [
        row
        for row in validation_result.get("candidates", [])
        if row.get("promotion_status") == "PASS"
    ]
    if not centers:
        raise ValueError("validation result contains no PASS candidates to diagnose")

    planned = sum(len(_axis_neighbors(StrategySpec(**row["strategy"]), spec.steps)) for row in centers)
    if planned > spec.max_neighbor_evaluations:
        raise ValueError(
            f"robustness requires {planned} neighbor evaluations, above max_neighbor_evaluations="
            f"{spec.max_neighbor_evaluations}"
        )

    validation_gates = validation_result["validation_spec"]["gates"]
    rows: list[dict[str, Any]] = []
    robust_count = 0
    for center in centers:
        strategy = StrategySpec(**center["strategy"])
        neighbor_rows = []
        for axis, direction, neighbor in _axis_neighbors(strategy, spec.steps):
            result = evaluate_strategy(
                context,
                neighbor,
                phases=("discovery", "validation"),
            )
            if result["holdout_revealed"]:
                raise RuntimeError("robustness phase isolation failed")
            discovery_windows = [
                row for row in result["windows"] if row["phase"] == "discovery"
            ]
            validation_windows = [
                row for row in result["windows"] if row["phase"] == "validation"
            ]
            discovery_aggregate = _aggregate(_compact_windows(discovery_windows))
            validation_aggregate = _aggregate(_compact_windows(validation_windows))
            validation_reasons = _validation_gate_failures(
                validation_aggregate, validation_gates
            )
            neighbor_rows.append(
                {
                    "axis": axis,
                    "direction": direction,
                    "diagnostic_only": True,
                    "strategy": asdict(neighbor),
                    "discovery": discovery_aggregate,
                    "validation": validation_aggregate,
                    "validation_gate_pass": not validation_reasons,
                    "validation_gate_failures": validation_reasons,
                }
            )

        metrics = _neighborhood_metrics(center, neighbor_rows)
        failures = _robustness_gate_failures(metrics, spec.gates)
        status = "ROBUST" if not failures else "FRAGILE"
        if status == "ROBUST":
            robust_count += 1
        rows.append(
            {
                "candidate_fingerprint_sha256": center[
                    "candidate_fingerprint_sha256"
                ],
                "center_strategy": center["strategy"],
                "center_parameters_retuned": False,
                "neighbor_strategies_promotable": False,
                "status": status,
                "failure_reasons": failures,
                "metrics": metrics,
                "neighbors": neighbor_rows,
            }
        )

    return {
        "schema_version": 1,
        "kind": "parameter_extract.neighborhood_robustness",
        "robustness": spec.name,
        "robustness_fingerprint_sha256": robustness_fingerprint(spec),
        "robustness_spec": asdict(spec),
        "source_validation_result_sha256": actual_validation_sha,
        "study_fingerprint_sha256": study_fingerprint(context.spec),
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "symbol": context.spec.symbol,
        "parameters_retuned": False,
        "neighbor_strategies_promotable": False,
        "discovery_accessed": True,
        "validation_accessed": True,
        "holdout_accessed": False,
        "center_count": len(rows),
        "robust_count": robust_count,
        "fragile_count": len(rows) - robust_count,
        "centers": rows,
    }


def _axis_neighbors(
    center: StrategySpec, steps: NeighborhoodSteps
) -> list[tuple[str, str, StrategySpec]]:
    proposals: list[tuple[str, str, dict[str, Any]]] = []
    base = asdict(center)
    if steps.include_rsi_period:
        for direction, delta in (("minus", -1), ("plus", 1)):
            proposals.append(("rsi_period", direction, {**base, "rsi_period": center.rsi_period + delta}))
    for axis, step in (
        ("rsi_entry", steps.rsi_entry),
        ("adx_min", steps.adx_min),
        ("adx_max", steps.adx_max),
    ):
        value = float(getattr(center, axis))
        proposals.append((axis, "minus", {**base, axis: value - step}))
        proposals.append((axis, "plus", {**base, axis: value + step}))
    if center.exit_mode == "tp":
        assert center.tp_price_pct is not None
        proposals.append(
            (
                "tp_price_pct",
                "minus",
                {**base, "tp_price_pct": center.tp_price_pct - steps.tp_price_pct},
            )
        )
        proposals.append(
            (
                "tp_price_pct",
                "plus",
                {**base, "tp_price_pct": center.tp_price_pct + steps.tp_price_pct},
            )
        )
    else:
        assert center.rsi_exit is not None
        proposals.append(
            (
                "rsi_exit",
                "minus",
                {**base, "rsi_exit": center.rsi_exit - steps.rsi_exit},
            )
        )
        proposals.append(
            (
                "rsi_exit",
                "plus",
                {**base, "rsi_exit": center.rsi_exit + steps.rsi_exit},
            )
        )

    neighbors: list[tuple[str, str, StrategySpec]] = []
    seen: set[str] = set()
    for axis, direction, payload in proposals:
        try:
            neighbor = StrategySpec(**payload)
        except (TypeError, ValueError):
            continue
        key = json.dumps(asdict(neighbor), sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        neighbors.append((axis, direction, neighbor))
    return neighbors


def _compact_windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for row in rows:
        metrics = row["metrics"]
        compact.append(
            {
                "return_pct": metrics["total_return_pct"],
                "trade_count": metrics["trade_count"],
                "worst_mae_pct": metrics["worst_mae_pct"],
                "drawdown_pct": metrics["max_closed_equity_drawdown_pct"],
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
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value / 100.0
    return {
        "window_count": len(windows),
        "positive_window_fraction": (
            0.0 if not windows else sum(value > 0.0 for value in returns) / len(windows)
        ),
        "total_trades": sum(int(row["trade_count"]) for row in windows),
        "compounded_window_return_pct": (equity - 1.0) * 100.0,
        "median_window_return_pct": statistics.median(returns) if returns else 0.0,
        "worst_window_return_pct": min(returns) if returns else 0.0,
        "worst_mae_pct": min(maes) if maes else None,
        "max_drawdown_pct": min(drawdowns) if drawdowns else 0.0,
        "open_at_end_windows": sum(bool(row["open_at_end"]) for row in windows),
    }


def _validation_gate_failures(
    metrics: dict[str, Any], gates: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if metrics["total_trades"] < int(gates["min_total_trades"]):
        reasons.append("INSUFFICIENT_TRADES")
    if metrics["positive_window_fraction"] < float(gates["min_positive_window_fraction"]):
        reasons.append("POSITIVE_WINDOW_FRACTION")
    minimum = gates.get("min_median_window_return_pct")
    if minimum is not None and metrics["median_window_return_pct"] < float(minimum):
        reasons.append("MEDIAN_WINDOW_RETURN")
    minimum = gates.get("min_worst_window_return_pct")
    if minimum is not None and metrics["worst_window_return_pct"] < float(minimum):
        reasons.append("WORST_WINDOW_RETURN")
    minimum = gates.get("min_worst_mae_pct")
    if minimum is not None:
        mae = metrics["worst_mae_pct"]
        if mae is None or mae < float(minimum):
            reasons.append("WORST_MAE")
    maximum = gates.get("max_open_at_end_windows")
    if maximum is not None and metrics["open_at_end_windows"] > int(maximum):
        reasons.append("OPEN_AT_END_WINDOWS")
    return reasons


def _neighborhood_metrics(
    center: dict[str, Any], neighbors: list[dict[str, Any]]
) -> dict[str, Any]:
    validation_returns = [
        float(row["validation"]["compounded_window_return_pct"]) for row in neighbors
    ]
    center_validation_return = float(
        center["validation"]["aggregate"]["compounded_window_return_pct"]
    )
    median_validation_return = (
        statistics.median(validation_returns) if validation_returns else 0.0
    )
    return {
        "neighbor_count": len(neighbors),
        "validation_pass_fraction": (
            0.0
            if not neighbors
            else sum(bool(row["validation_gate_pass"]) for row in neighbors) / len(neighbors)
        ),
        "discovery_positive_fraction": (
            0.0
            if not neighbors
            else sum(
                row["discovery"]["positive_window_fraction"] > 0.5
                for row in neighbors
            )
            / len(neighbors)
        ),
        "center_validation_compounded_return_pct": center_validation_return,
        "median_neighbor_validation_compounded_return_pct": median_validation_return,
        "center_validation_advantage_pct": center_validation_return - median_validation_return,
        "worst_neighbor_validation_return_pct": (
            min(validation_returns) if validation_returns else None
        ),
    }


def _robustness_gate_failures(
    metrics: dict[str, Any], gates: RobustnessGates
) -> list[str]:
    reasons: list[str] = []
    if metrics["neighbor_count"] < gates.min_neighbor_count:
        reasons.append("NEIGHBOR_COUNT")
    if metrics["validation_pass_fraction"] < gates.min_validation_pass_fraction:
        reasons.append("VALIDATION_NEIGHBOR_SURVIVAL")
    if metrics["discovery_positive_fraction"] < gates.min_discovery_positive_fraction:
        reasons.append("DISCOVERY_NEIGHBOR_STABILITY")
    if (
        gates.max_center_validation_advantage_pct is not None
        and metrics["center_validation_advantage_pct"]
        > gates.max_center_validation_advantage_pct
    ):
        reasons.append("CENTER_SPIKE")
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
