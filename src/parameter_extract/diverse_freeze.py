from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable

from .io import write_json
from .promotion import candidate_set_fingerprint, freeze_discovery_result, verify_candidate_set

DIVERSE_FREEZE_METHOD = "discovery_kcenter_v1"
_PARETO_OBJECTIVES = (
    "worst_window_return_pct",
    "median_window_return_pct",
    "worst_mae_pct",
    "max_drawdown_pct",
)


def freeze_diverse_discovery_result(path: str | Path, *, count: int) -> dict[str, Any]:
    """Freeze a discovery-only, diverse subset of the Pareto frontier.

    Selection never evaluates validation or holdout. It seeds coverage by exit mode, RSI
    period and Pareto-objective extremes, then fills remaining slots with deterministic
    farthest-point sampling over equal-weight parameter and discovery-behavior spaces.
    Strategies are copied verbatim from the frozen discovery frontier; no parameter retuning
    occurs.
    """
    if count < 1:
        raise ValueError("count must be positive")

    full = freeze_discovery_result(path)
    candidates = list(full["candidates"])
    if count > len(candidates):
        raise ValueError(
            f"requested {count} candidates but discovery frontier contains only {len(candidates)}"
        )
    if count == len(candidates):
        source = dict(full["source"])
        source["prevalidation_selection"] = {
            "method": DIVERSE_FREEZE_METHOD,
            "source_frontier_count": len(candidates),
            "requested_count": count,
            "selected_count": count,
            "parameters_retuned": False,
            "validation_accessed": False,
            "holdout_accessed": False,
            "selection_order": [row["candidate_fingerprint_sha256"] for row in candidates],
        }
        full["source"] = source
        full["candidate_set_fingerprint_sha256"] = candidate_set_fingerprint(full)
        return full

    by_fp = {row["candidate_fingerprint_sha256"]: row for row in candidates}
    parameter_vectors = _normalized_vectors(candidates, _parameter_features)
    behavior_vectors = _normalized_vectors(candidates, _behavior_features)

    selected: list[str] = []
    audit: dict[str, dict[str, Any]] = {}

    def add_seed(row: dict[str, Any], reason: str) -> None:
        fp = row["candidate_fingerprint_sha256"]
        if fp not in audit:
            selected.append(fp)
            audit[fp] = {
                "candidate_fingerprint_sha256": fp,
                "seed_reasons": [reason],
                "selection_distance": None,
            }
        elif reason not in audit[fp]["seed_reasons"]:
            audit[fp]["seed_reasons"].append(reason)

    exit_modes = sorted({str(row["strategy"]["exit_mode"]) for row in candidates})
    for mode in exit_modes:
        pool = [row for row in candidates if row["strategy"]["exit_mode"] == mode]
        add_seed(_best(pool), f"best_exit_mode:{mode}")

    periods = sorted({int(row["strategy"]["rsi_period"]) for row in candidates})
    for period in periods:
        pool = [row for row in candidates if int(row["strategy"]["rsi_period"]) == period]
        add_seed(_best(pool), f"best_rsi_period:{period}")

    for objective in _PARETO_OBJECTIVES:
        add_seed(
            _best_metric(candidates, objective),
            f"pareto_extreme:{objective}",
        )

    if len(selected) > count:
        raise ValueError(
            f"count={count} is too small for mandatory discovery coverage; "
            f"requires at least {len(selected)} slots"
        )

    while len(selected) < count:
        remaining = [fp for fp in by_fp if fp not in audit]
        best_fp = min(
            remaining,
            key=lambda fp: _greedy_sort_key(
                fp,
                selected,
                by_fp,
                parameter_vectors,
                behavior_vectors,
            ),
        )
        distance = _min_combined_distance(
            best_fp,
            selected,
            parameter_vectors,
            behavior_vectors,
        )
        selected.append(best_fp)
        audit[best_fp] = {
            "candidate_fingerprint_sha256": best_fp,
            "seed_reasons": ["farthest_discovery_behavior_parameter"],
            "selection_distance": distance,
        }

    chosen = [by_fp[fp] for fp in selected]
    chosen.sort(key=lambda row: row["candidate_fingerprint_sha256"])

    mode_counts: dict[str, int] = {}
    period_counts: dict[str, int] = {}
    for row in chosen:
        mode = str(row["strategy"]["exit_mode"])
        period = str(int(row["strategy"]["rsi_period"]))
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        period_counts[period] = period_counts.get(period, 0) + 1

    payload = dict(full)
    source = dict(full["source"])
    source["prevalidation_selection"] = {
        "method": DIVERSE_FREEZE_METHOD,
        "source_frontier_count": len(candidates),
        "requested_count": count,
        "selected_count": len(chosen),
        "coverage_seeds": {
            "exit_modes": exit_modes,
            "rsi_periods": periods,
            "pareto_objectives": list(_PARETO_OBJECTIVES),
        },
        "distance": {
            "parameter_space_weight": 0.5,
            "discovery_behavior_space_weight": 0.5,
            "normalization": "frontier_min_max_per_feature",
            "group_distance": "rms",
            "combined_distance": "root_mean_square_of_group_rms",
        },
        "selected_exit_mode_counts": dict(sorted(mode_counts.items())),
        "selected_rsi_period_counts": dict(sorted(period_counts.items(), key=lambda item: int(item[0]))),
        "parameters_retuned": False,
        "validation_accessed": False,
        "holdout_accessed": False,
        "selection_order": [audit[fp] for fp in selected],
    }
    payload["source"] = source
    payload["candidate_count"] = len(chosen)
    payload["candidates"] = chosen
    payload["candidate_set_fingerprint_sha256"] = candidate_set_fingerprint(payload)

    problems = verify_candidate_set(payload)
    if problems:
        raise RuntimeError("diverse frozen candidate set failed self-verification: " + "; ".join(problems))
    return payload


def _quality(row: dict[str, Any]) -> tuple[float, float, float, float, int]:
    metrics = row["discovery"]["aggregate"]
    return (
        _number(metrics.get("worst_window_return_pct"), -1e12),
        _number(metrics.get("median_window_return_pct"), -1e12),
        _number(metrics.get("worst_mae_pct"), -1e12),
        _number(metrics.get("max_drawdown_pct"), -1e12),
        int(metrics.get("total_trades") or 0),
    )


def _best(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return min(
        rows,
        key=lambda row: (
            tuple(-value for value in _quality(row)),
            row["candidate_fingerprint_sha256"],
        ),
    )


def _best_metric(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    return min(
        rows,
        key=lambda row: (
            -_number(row["discovery"]["aggregate"].get(metric), -1e12),
            tuple(-value for value in _quality(row)),
            row["candidate_fingerprint_sha256"],
        ),
    )


def _parameter_features(row: dict[str, Any]) -> dict[str, float]:
    strategy = row["strategy"]
    mode = str(strategy["exit_mode"])
    return {
        "rsi_period": float(strategy["rsi_period"]),
        "rsi_entry": float(strategy["rsi_entry"]),
        "adx_min": float(strategy["adx_min"]),
        "adx_max": float(strategy["adx_max"]),
        "is_tp": 1.0 if mode == "tp" else 0.0,
        "is_rsi_exit": 1.0 if mode == "rsi" else 0.0,
        "tp_price_pct": 0.0 if strategy.get("tp_price_pct") is None else float(strategy["tp_price_pct"]),
        "rsi_exit": 0.0 if strategy.get("rsi_exit") is None else float(strategy["rsi_exit"]),
    }


def _behavior_features(row: dict[str, Any]) -> dict[str, float]:
    discovery = row["discovery"]
    aggregate = discovery["aggregate"]
    features = {
        "aggregate:worst_window_return_pct": _number(aggregate.get("worst_window_return_pct")),
        "aggregate:median_window_return_pct": _number(aggregate.get("median_window_return_pct")),
        "aggregate:compounded_window_return_pct": _number(aggregate.get("compounded_window_return_pct")),
        "aggregate:worst_mae_pct": _number(aggregate.get("worst_mae_pct")),
        "aggregate:max_drawdown_pct": _number(aggregate.get("max_drawdown_pct")),
        "aggregate:total_trades": float(aggregate.get("total_trades") or 0),
        "aggregate:max_holding_minutes": _number(aggregate.get("max_holding_minutes")),
        "aggregate:open_at_end_windows": float(aggregate.get("open_at_end_windows") or 0),
    }
    for window in sorted(discovery.get("windows") or [], key=lambda item: str(item.get("name"))):
        prefix = f"window:{window.get('name')}"
        features[f"{prefix}:return_pct"] = _number(window.get("return_pct"))
        features[f"{prefix}:trade_count"] = float(window.get("trade_count") or 0)
        features[f"{prefix}:worst_mae_pct"] = _number(window.get("worst_mae_pct"))
        features[f"{prefix}:drawdown_pct"] = _number(window.get("drawdown_pct"))
        features[f"{prefix}:max_holding_minutes"] = _number(window.get("max_holding_minutes"))
        features[f"{prefix}:open_at_end"] = 1.0 if window.get("open_at_end") else 0.0
    return features


def _normalized_vectors(
    rows: list[dict[str, Any]], feature_fn: Callable[[dict[str, Any]], dict[str, float]]
) -> dict[str, tuple[float, ...]]:
    raw = {row["candidate_fingerprint_sha256"]: feature_fn(row) for row in rows}
    keys = sorted({key for values in raw.values() for key in values})
    minimum = {key: min(values.get(key, 0.0) for values in raw.values()) for key in keys}
    maximum = {key: max(values.get(key, 0.0) for values in raw.values()) for key in keys}
    vectors: dict[str, tuple[float, ...]] = {}
    for fp, values in raw.items():
        normalized: list[float] = []
        for key in keys:
            low = minimum[key]
            high = maximum[key]
            value = values.get(key, 0.0)
            normalized.append(0.0 if high == low else (value - low) / (high - low))
        vectors[fp] = tuple(normalized)
    return vectors


def _rms_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("feature vectors have different lengths")
    if not left:
        return 0.0
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)) / len(left))


def _combined_distance(
    left: str,
    right: str,
    parameter_vectors: dict[str, tuple[float, ...]],
    behavior_vectors: dict[str, tuple[float, ...]],
) -> float:
    parameter = _rms_distance(parameter_vectors[left], parameter_vectors[right])
    behavior = _rms_distance(behavior_vectors[left], behavior_vectors[right])
    return math.sqrt((parameter * parameter + behavior * behavior) / 2.0)


def _min_combined_distance(
    candidate: str,
    selected: list[str],
    parameter_vectors: dict[str, tuple[float, ...]],
    behavior_vectors: dict[str, tuple[float, ...]],
) -> float:
    return min(
        _combined_distance(candidate, existing, parameter_vectors, behavior_vectors)
        for existing in selected
    )


def _greedy_sort_key(
    fp: str,
    selected: list[str],
    by_fp: dict[str, dict[str, Any]],
    parameter_vectors: dict[str, tuple[float, ...]],
    behavior_vectors: dict[str, tuple[float, ...]],
) -> tuple[Any, ...]:
    distance = _min_combined_distance(fp, selected, parameter_vectors, behavior_vectors)
    quality = _quality(by_fp[fp])
    return (-distance, tuple(-value for value in quality), fp)


def _number(value: Any, default: float = 0.0) -> float:
    return default if value is None else float(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-freeze-diverse",
        description="Freeze a deterministic diverse discovery-only subset before validation.",
    )
    parser.add_argument("--search-result", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = freeze_diverse_discovery_result(args.search_result, count=args.count)
    write_json(args.output, payload)
    selection = payload["source"]["prevalidation_selection"]
    print(
        json.dumps(
            {
                "output": args.output,
                "candidate_set_fingerprint_sha256": payload["candidate_set_fingerprint_sha256"],
                "source_frontier_count": selection["source_frontier_count"],
                "candidate_count": payload["candidate_count"],
                "selected_exit_mode_counts": selection.get("selected_exit_mode_counts"),
                "selected_rsi_period_counts": selection.get("selected_rsi_period_counts"),
                "parameters_frozen": payload["parameters_frozen"],
                "validation_accessed": selection["validation_accessed"],
                "holdout_accessed": selection["holdout_accessed"],
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
