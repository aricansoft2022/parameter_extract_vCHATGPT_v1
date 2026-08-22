from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import StrategySpec
from .prepared import PreparedDiscovery, evaluate_prepared_discovery, prepare_discovery
from .search import (
    PARETO_OBJECTIVES,
    _aggregate,
    _coarse_candidates,
    _pareto_frontier,
    _passes_gates,
    _refined_candidates,
    _seed_priority,
    _strategy_key,
    load_search_json,
    search_fingerprint,
)
from .study import StudyContext, load_study_context, study_fingerprint

PREPARED_SEARCH_ENGINE = "prepared_exact_v1"
REFERENCE_ENGINE = "truth_replay"


def run_prepared_search(
    study_path: str | Path,
    search_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Run the existing discovery search semantics with reusable indicator state.

    This is an exact prepared path, not yet the threshold-inverted/factorized engine.
    Candidate generation, gates, Pareto objectives and refinement ordering are imported
    from the reference search implementation. Entry/exit execution still uses the shared
    truth replay through ``evaluate_prepared_discovery``.
    """

    context = load_study_context(study_path, data_directory=data_directory)
    spec = load_search_json(search_path)
    prepared = prepare_discovery(context, rsi_periods=spec.rsi_periods)
    if prepared.validation_accessed or prepared.holdout_accessed:
        raise RuntimeError("prepared search phase isolation failed")

    coarse = list(_coarse_candidates(context.spec.symbol, spec))
    if len(coarse) > spec.refinement.max_candidates:
        raise ValueError(
            f"coarse grid has {len(coarse)} candidates, above max_candidates="
            f"{spec.refinement.max_candidates}; widen steps before running"
        )

    evaluated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for strategy in coarse:
        key = _strategy_key(strategy)
        if key in seen:
            continue
        seen.add(key)
        evaluated.append(
            _evaluate_prepared_candidate(context, prepared, strategy, stage="coarse")
        )

    gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
    frontier = _pareto_frontier(gated)

    refined_count = 0
    if spec.refinement.enabled and frontier:
        seeds = sorted(frontier, key=_seed_priority, reverse=True)[: spec.refinement.max_seeds]
        for seed in seeds:
            strategy = StrategySpec(**seed["strategy"])
            for refined in _refined_candidates(context.spec.symbol, spec, strategy):
                key = _strategy_key(refined)
                if key in seen:
                    continue
                if len(seen) >= spec.refinement.max_candidates:
                    raise ValueError(
                        "refinement reached max_candidates before finishing; reduce max_seeds, "
                        "radius_steps or coarse grid size"
                    )
                seen.add(key)
                evaluated.append(
                    _evaluate_prepared_candidate(
                        context,
                        prepared,
                        refined,
                        stage="refined",
                    )
                )
                refined_count += 1
        gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
        frontier = _pareto_frontier(gated)

    frontier = sorted(frontier, key=_seed_priority, reverse=True)
    return {
        "schema_version": 1,
        "kind": "parameter_extract.discovery_search",
        "search": spec.name,
        "search_fingerprint_sha256": search_fingerprint(spec),
        "search_spec": asdict(spec),
        "study": context.spec.name,
        "study_fingerprint_sha256": study_fingerprint(context.spec),
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "execution": asdict(context.spec.execution),
        "symbol": context.spec.symbol,
        "phase_used": "discovery",
        "validation_accessed": False,
        "holdout_accessed": False,
        "search_engine": PREPARED_SEARCH_ENGINE,
        "reference_engine": REFERENCE_ENGINE,
        "indicator_cache_rsi_periods": list(prepared.rsi_periods),
        "prepared_discovery_window_count": len(prepared.windows),
        "pareto_objectives": list(PARETO_OBJECTIVES),
        "frontier_order_note": (
            "Frontier rows are ordered only to choose refinement/reporting order; "
            "the order is not a scalar fitness ranking."
        ),
        "coarse_candidates": len(coarse),
        "refined_candidates": refined_count,
        "evaluated_candidates": len(evaluated),
        "passed_gates": len(gated),
        "pareto_candidates": len(frontier),
        "frontier": frontier,
    }


def _evaluate_prepared_candidate(
    context: StudyContext,
    prepared: PreparedDiscovery,
    strategy: StrategySpec,
    *,
    stage: str,
) -> dict[str, Any]:
    compact_windows = evaluate_prepared_discovery(context, prepared, strategy)
    return {
        "stage": stage,
        "strategy": asdict(strategy),
        "aggregate": _aggregate(compact_windows),
        "windows": compact_windows,
    }
