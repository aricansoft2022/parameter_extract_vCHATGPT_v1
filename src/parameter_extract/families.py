from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .manifest import sha256_file
from .models import StrategySpec
from .promotion import candidate_fingerprint
from .robustness import (
    NeighborhoodSteps,
    RobustnessGates,
    RobustnessSpec,
    robustness_fingerprint,
)
from .study import collect_strategy_evidence, load_study_context, study_fingerprint

FAMILY_SCHEMA_VERSION = 1
CROSS_EXIT_MODE_DISTANCE = 1e12


@dataclass(frozen=True, slots=True)
class ParameterScales:
    rsi_period: float
    rsi_entry: float
    adx_min: float
    adx_max: float
    tp_price_pct: float
    rsi_exit: float

    def __post_init__(self) -> None:
        for name in (
            "rsi_period",
            "rsi_entry",
            "adx_min",
            "adx_max",
            "tp_price_pct",
            "rsi_exit",
        ):
            if float(getattr(self, name)) <= 0.0:
                raise ValueError(f"{name} parameter scale must be positive")


@dataclass(frozen=True, slots=True)
class FamilyThresholds:
    signal_tolerance_minutes: float
    min_raw_signal_dice: float
    min_accepted_signal_dice: float
    min_exposure_jaccard: float
    max_parameter_distance: float

    def __post_init__(self) -> None:
        if self.signal_tolerance_minutes < 0.0:
            raise ValueError("signal_tolerance_minutes cannot be negative")
        for name in (
            "min_raw_signal_dice",
            "min_accepted_signal_dice",
            "min_exposure_jaccard",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be inside [0, 1]")
        if self.max_parameter_distance < 0.0:
            raise ValueError("max_parameter_distance cannot be negative")


@dataclass(frozen=True, slots=True)
class FamilySpec:
    name: str
    source_robustness_result_sha256: str
    thresholds: FamilyThresholds
    parameter_scales: ParameterScales
    max_pair_evaluations: int = 20_000

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("family name cannot be empty")
        _validate_digest(self.source_robustness_result_sha256)
        if self.max_pair_evaluations < 1:
            raise ValueError("max_pair_evaluations must be positive")


def load_family_json(path: str | Path) -> FamilySpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != FAMILY_SCHEMA_VERSION:
        raise ValueError("unsupported family schema_version")
    thresholds = payload["thresholds"]
    scales = payload["parameter_scales"]
    return FamilySpec(
        name=str(payload["name"]),
        source_robustness_result_sha256=str(
            payload["source_robustness_result_sha256"]
        ).lower(),
        thresholds=FamilyThresholds(
            signal_tolerance_minutes=float(thresholds["signal_tolerance_minutes"]),
            min_raw_signal_dice=float(thresholds["min_raw_signal_dice"]),
            min_accepted_signal_dice=float(thresholds["min_accepted_signal_dice"]),
            min_exposure_jaccard=float(thresholds["min_exposure_jaccard"]),
            max_parameter_distance=float(thresholds["max_parameter_distance"]),
        ),
        parameter_scales=ParameterScales(
            rsi_period=float(scales["rsi_period"]),
            rsi_entry=float(scales["rsi_entry"]),
            adx_min=float(scales["adx_min"]),
            adx_max=float(scales["adx_max"]),
            tp_price_pct=float(scales["tp_price_pct"]),
            rsi_exit=float(scales["rsi_exit"]),
        ),
        max_pair_evaluations=int(payload.get("max_pair_evaluations", 20_000)),
    )


def family_fingerprint(spec: FamilySpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_family_clustering(
    study_path: str | Path,
    robustness_result_path: str | Path,
    family_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    robustness_path = Path(robustness_result_path)
    robustness_result = json.loads(robustness_path.read_text(encoding="utf-8"))
    spec = load_family_json(family_path)
    actual_robustness_sha = sha256_file(robustness_path)
    if spec.source_robustness_result_sha256 != actual_robustness_sha:
        raise ValueError("family contract is pinned to a different robustness-result file")

    problems = verify_robustness_result(robustness_result)
    if problems:
        raise ValueError("robustness-result verification failed: " + "; ".join(problems))
    if robustness_result.get("study_fingerprint_sha256") != study_fingerprint(context.spec):
        raise ValueError("robustness result belongs to a different study contract")
    if robustness_result.get("dataset_fingerprint_sha256") != context.spec.dataset_fingerprint_sha256:
        raise ValueError("robustness result belongs to a different dataset")
    if robustness_result.get("symbol") != context.spec.symbol:
        raise ValueError("robustness-result symbol does not match the study")
    if robustness_result.get("execution") != asdict(context.spec.execution):
        raise ValueError("robustness-result execution assumptions do not match the study")

    centers = [row for row in robustness_result["centers"] if row["status"] == "ROBUST"]
    if not centers:
        raise ValueError("robustness result contains no ROBUST centers to cluster")

    pair_count = len(centers) * (len(centers) - 1) // 2
    if pair_count > spec.max_pair_evaluations:
        raise ValueError(
            f"family clustering requires {pair_count} pair evaluations, above "
            f"max_pair_evaluations={spec.max_pair_evaluations}"
        )

    evidence: dict[str, dict[str, Any]] = {}
    for center in centers:
        strategy = StrategySpec(**center["center_strategy"])
        fingerprint = center["candidate_fingerprint_sha256"]
        result = collect_strategy_evidence(
            context,
            strategy,
            phases=("discovery", "validation"),
        )
        if result.get("phases_evaluated") != ["discovery", "validation"]:
            raise RuntimeError("family evidence phase isolation failed")
        if result.get("holdout_accessed") is not False:
            raise RuntimeError("family evidence phase isolation failed")
        evidence[fingerprint] = _flatten_evidence(result)

    pair_rows: list[dict[str, Any]] = []
    pair_lookup: dict[frozenset[str], dict[str, Any]] = {}
    for left_index, left in enumerate(centers):
        for right in centers[left_index + 1 :]:
            pair = _compare_centers(
                left,
                right,
                evidence[left["candidate_fingerprint_sha256"]],
                evidence[right["candidate_fingerprint_sha256"]],
                spec,
            )
            pair_rows.append(pair)
            pair_lookup[frozenset((pair["left"], pair["right"]))] = pair

    ordered = sorted(centers, key=_representative_sort_key)
    clusters: list[list[dict[str, Any]]] = []
    for center in ordered:
        compatible: list[tuple[float, str, int]] = []
        center_fp = center["candidate_fingerprint_sha256"]
        for index, cluster in enumerate(clusters):
            pairs = [
                pair_lookup[frozenset((center_fp, member["candidate_fingerprint_sha256"]))]
                for member in cluster
            ]
            if all(pair["same_family"] for pair in pairs):
                minimum_score = min(pair["family_score"] for pair in pairs)
                representative_fp = cluster[0]["candidate_fingerprint_sha256"]
                compatible.append((minimum_score, representative_fp, index))
        if not compatible:
            clusters.append([center])
            continue
        _, _, chosen_index = sorted(
            compatible,
            key=lambda item: (-item[0], item[1], item[2]),
        )[0]
        clusters[chosen_index].append(center)

    family_rows: list[dict[str, Any]] = []
    representatives: list[dict[str, Any]] = []
    for family_index, cluster in enumerate(clusters, start=1):
        representative = cluster[0]
        member_fps = [row["candidate_fingerprint_sha256"] for row in cluster]
        family_id = f"F{family_index:04d}"
        within_pairs = [
            pair
            for pair in pair_rows
            if pair["left"] in member_fps and pair["right"] in member_fps
        ]
        family_rows.append(
            {
                "family_id": family_id,
                "representative_candidate_fingerprint_sha256": representative[
                    "candidate_fingerprint_sha256"
                ],
                "representative_strategy": representative["center_strategy"],
                "representative_selection": "robustness_stability_v1",
                "member_count": len(cluster),
                "members": [
                    {
                        "candidate_fingerprint_sha256": row[
                            "candidate_fingerprint_sha256"
                        ],
                        "strategy": row["center_strategy"],
                        "robustness_metrics": row["metrics"],
                    }
                    for row in cluster
                ],
                "within_family_pairs": within_pairs,
            }
        )
        representatives.append(
            {
                "family_id": family_id,
                "candidate_fingerprint_sha256": representative[
                    "candidate_fingerprint_sha256"
                ],
                "strategy": representative["center_strategy"],
                "parameters_retuned": False,
            }
        )

    return {
        "schema_version": 1,
        "kind": "parameter_extract.strategy_families",
        "family": spec.name,
        "family_fingerprint_sha256": family_fingerprint(spec),
        "family_spec": asdict(spec),
        "representative_policy": "robustness_stability_v1",
        "source_robustness_result_sha256": actual_robustness_sha,
        "study_fingerprint_sha256": study_fingerprint(context.spec),
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "symbol": context.spec.symbol,
        "execution": asdict(context.spec.execution),
        "parameters_retuned": False,
        "representatives_are_existing_robust_centers": True,
        "discovery_accessed": True,
        "validation_accessed": True,
        "holdout_accessed": False,
        "robust_center_count": len(centers),
        "pair_evaluations": pair_count,
        "family_count": len(family_rows),
        "deduplicated_center_count": len(centers) - len(family_rows),
        "families": family_rows,
        "representatives": representatives,
        "pairwise": pair_rows,
    }


def verify_robustness_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported robustness-result schema_version")
    if payload.get("kind") != "parameter_extract.neighborhood_robustness":
        problems.append("robustness-result kind is invalid")
    if payload.get("parameters_retuned") is not False:
        problems.append("robustness result does not prove frozen center parameters")
    if payload.get("neighbor_strategies_promotable") is not False:
        problems.append("robustness result allows neighbor promotion")
    if payload.get("discovery_accessed") is not True:
        problems.append("robustness result does not indicate discovery access")
    if payload.get("validation_accessed") is not True:
        problems.append("robustness result does not indicate validation access")
    if payload.get("holdout_accessed") is not False:
        problems.append("robustness result indicates holdout access")

    spec_payload = payload.get("robustness_spec")
    if not isinstance(spec_payload, dict):
        problems.append("robustness_spec is missing or invalid")
    else:
        try:
            steps = NeighborhoodSteps(**spec_payload["steps"])
            gates = RobustnessGates(**spec_payload["gates"])
            robust_spec = RobustnessSpec(
                name=str(spec_payload["name"]),
                source_validation_result_sha256=str(
                    spec_payload["source_validation_result_sha256"]
                ),
                steps=steps,
                gates=gates,
                max_neighbor_evaluations=int(spec_payload["max_neighbor_evaluations"]),
            )
            if payload.get("robustness_fingerprint_sha256") != robustness_fingerprint(
                robust_spec
            ):
                problems.append("robustness fingerprint does not match robustness_spec")
            if (
                payload.get("source_validation_result_sha256")
                != robust_spec.source_validation_result_sha256
            ):
                problems.append("robustness source validation-result SHA is inconsistent")
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"robustness_spec is invalid: {exc}")

    centers = payload.get("centers")
    if not isinstance(centers, list):
        problems.append("robustness center list is missing or invalid")
        return problems
    if payload.get("center_count") != len(centers):
        problems.append("center_count does not match center list length")

    robust = 0
    fragile = 0
    seen: set[str] = set()
    neighbor_total = 0
    for index, row in enumerate(centers):
        if not isinstance(row, dict):
            problems.append(f"center {index}: row is invalid")
            continue
        try:
            strategy = StrategySpec(**row["center_strategy"])
            actual_fp = candidate_fingerprint(strategy)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"center {index}: invalid strategy: {exc}")
            continue
        if row.get("candidate_fingerprint_sha256") != actual_fp:
            problems.append(f"center {index}: strategy fingerprint mismatch")
        if actual_fp in seen:
            problems.append(f"center {index}: duplicate strategy fingerprint")
        seen.add(actual_fp)
        if row.get("center_parameters_retuned") is not False:
            problems.append(f"center {index}: center_parameters_retuned is not false")
        if row.get("neighbor_strategies_promotable") is not False:
            problems.append(f"center {index}: neighbor strategies are marked promotable")

        status = row.get("status")
        failures = row.get("failure_reasons")
        if status == "ROBUST":
            robust += 1
            if failures not in ([], None):
                problems.append(f"center {index}: ROBUST center has failure reasons")
        elif status == "FRAGILE":
            fragile += 1
            if not isinstance(failures, list) or not failures:
                problems.append(f"center {index}: FRAGILE center lacks failure reasons")
        else:
            problems.append(f"center {index}: invalid robustness status")

        neighbors = row.get("neighbors")
        if not isinstance(neighbors, list):
            problems.append(f"center {index}: neighbors are missing or invalid")
            continue
        neighbor_total += len(neighbors)
        for neighbor_index, neighbor_row in enumerate(neighbors):
            if neighbor_row.get("diagnostic_only") is not True:
                problems.append(
                    f"center {index} neighbor {neighbor_index}: not marked diagnostic_only"
                )
            try:
                neighbor_strategy = StrategySpec(**neighbor_row["strategy"])
                neighbor_fp = candidate_fingerprint(neighbor_strategy)
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(
                    f"center {index} neighbor {neighbor_index}: invalid strategy: {exc}"
                )
                continue
            if neighbor_row.get("candidate_fingerprint_sha256") != neighbor_fp:
                problems.append(
                    f"center {index} neighbor {neighbor_index}: strategy fingerprint mismatch"
                )

    if payload.get("robust_count") != robust:
        problems.append("robust_count does not match ROBUST centers")
    if payload.get("fragile_count") != fragile:
        problems.append("fragile_count does not match FRAGILE centers")
    if payload.get("neighbor_evaluations") != neighbor_total:
        problems.append("neighbor_evaluations does not match stored neighbor evidence")
    return problems


def _flatten_evidence(result: dict[str, Any]) -> dict[str, Any]:
    raw: list[int] = []
    accepted: list[int] = []
    intervals: list[tuple[int, int]] = []
    for window in result["windows"]:
        raw.extend(int(value) for value in window["raw_signal_times_ms"])
        accepted.extend(int(value) for value in window["accepted_signal_times_ms"])
        intervals.extend(
            (int(start), int(end)) for start, end in window["position_intervals_ms"]
        )
    return {
        "raw_signal_times_ms": sorted(set(raw)),
        "accepted_signal_times_ms": sorted(set(accepted)),
        "position_intervals_ms": _merge_intervals(intervals),
    }


def _compare_centers(
    left: dict[str, Any],
    right: dict[str, Any],
    left_evidence: dict[str, Any],
    right_evidence: dict[str, Any],
    spec: FamilySpec,
) -> dict[str, Any]:
    left_strategy = StrategySpec(**left["center_strategy"])
    right_strategy = StrategySpec(**right["center_strategy"])
    tolerance_ms = int(round(spec.thresholds.signal_tolerance_minutes * 60_000.0))
    raw_dice = _tolerant_dice(
        left_evidence["raw_signal_times_ms"],
        right_evidence["raw_signal_times_ms"],
        tolerance_ms,
    )
    accepted_dice = _tolerant_dice(
        left_evidence["accepted_signal_times_ms"],
        right_evidence["accepted_signal_times_ms"],
        tolerance_ms,
    )
    exposure_jaccard = _interval_jaccard(
        left_evidence["position_intervals_ms"],
        right_evidence["position_intervals_ms"],
    )
    parameter_distance = _parameter_distance(
        left_strategy,
        right_strategy,
        spec.parameter_scales,
    )
    same_exit_mode = left_strategy.exit_mode == right_strategy.exit_mode
    same_family = (
        same_exit_mode
        and raw_dice >= spec.thresholds.min_raw_signal_dice
        and accepted_dice >= spec.thresholds.min_accepted_signal_dice
        and exposure_jaccard >= spec.thresholds.min_exposure_jaccard
        and parameter_distance <= spec.thresholds.max_parameter_distance
    )
    distance_score = _distance_similarity(
        parameter_distance, spec.thresholds.max_parameter_distance
    )
    family_score = (raw_dice + accepted_dice + exposure_jaccard + distance_score) / 4.0
    return {
        "left": left["candidate_fingerprint_sha256"],
        "right": right["candidate_fingerprint_sha256"],
        "same_exit_mode": same_exit_mode,
        "raw_signal_dice": raw_dice,
        "accepted_signal_dice": accepted_dice,
        "exposure_jaccard": exposure_jaccard,
        "parameter_distance": parameter_distance,
        "parameter_distance_similarity": distance_score,
        "family_score": family_score,
        "same_family": same_family,
    }


def _tolerant_dice(left: Iterable[int], right: Iterable[int], tolerance_ms: int) -> float:
    left_values = sorted(left)
    right_values = sorted(right)
    if not left_values and not right_values:
        return 1.0
    if not left_values or not right_values:
        return 0.0
    i = 0
    j = 0
    matches = 0
    while i < len(left_values) and j < len(right_values):
        delta = left_values[i] - right_values[j]
        if abs(delta) <= tolerance_ms:
            matches += 1
            i += 1
            j += 1
        elif delta < -tolerance_ms:
            i += 1
        else:
            j += 1
    return 2.0 * matches / (len(left_values) + len(right_values))


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        current = merged[-1]
        if start <= current[1]:
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _interval_jaccard(
    left: Iterable[tuple[int, int]], right: Iterable[tuple[int, int]]
) -> float:
    left_values = _merge_intervals(left)
    right_values = _merge_intervals(right)
    left_duration = sum(end - start for start, end in left_values)
    right_duration = sum(end - start for start, end in right_values)
    if left_duration == 0 and right_duration == 0:
        return 1.0
    i = 0
    j = 0
    intersection = 0
    while i < len(left_values) and j < len(right_values):
        left_start, left_end = left_values[i]
        right_start, right_end = right_values[j]
        intersection += max(0, min(left_end, right_end) - max(left_start, right_start))
        if left_end <= right_end:
            i += 1
        else:
            j += 1
    union = left_duration + right_duration - intersection
    return 0.0 if union <= 0 else intersection / union


def _parameter_distance(
    left: StrategySpec,
    right: StrategySpec,
    scales: ParameterScales,
) -> float:
    if left.exit_mode != right.exit_mode:
        # A large finite sentinel keeps persisted pair evidence strict-JSON compatible while
        # the separate `same_exit_mode` gate guarantees these strategies cannot share a V1 family.
        return CROSS_EXIT_MODE_DISTANCE
    values = [
        (left.rsi_period - right.rsi_period) / scales.rsi_period,
        (left.rsi_entry - right.rsi_entry) / scales.rsi_entry,
        (left.adx_min - right.adx_min) / scales.adx_min,
        (left.adx_max - right.adx_max) / scales.adx_max,
    ]
    if left.exit_mode == "tp":
        assert left.tp_price_pct is not None and right.tp_price_pct is not None
        values.append((left.tp_price_pct - right.tp_price_pct) / scales.tp_price_pct)
    else:
        assert left.rsi_exit is not None and right.rsi_exit is not None
        values.append((left.rsi_exit - right.rsi_exit) / scales.rsi_exit)
    return math.sqrt(sum(value * value for value in values) / len(values))


def _distance_similarity(distance: float, maximum: float) -> float:
    if not math.isfinite(distance):
        return 0.0
    if maximum == 0.0:
        return 1.0 if distance == 0.0 else 0.0
    return max(0.0, 1.0 - distance / maximum)


def _representative_sort_key(center: dict[str, Any]) -> tuple[Any, ...]:
    metrics = center["metrics"]
    worst_neighbor = metrics.get("worst_neighbor_validation_return_pct")
    return (
        -float(metrics.get("validation_pass_fraction", 0.0)),
        -float(metrics.get("discovery_stable_neighbor_fraction", 0.0)),
        -float(worst_neighbor if worst_neighbor is not None else -1e12),
        abs(float(metrics.get("center_validation_advantage_pct", 1e12))),
        -float(metrics.get("center_validation_compounded_return_pct", -1e12)),
        center["candidate_fingerprint_sha256"],
    )


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc
