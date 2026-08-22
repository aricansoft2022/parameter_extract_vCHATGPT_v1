from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .crossing_index import (
    IndexedDiscovery,
    build_crossing_index,
    evaluate_indexed_discovery,
    indexed_event_counts,
)
from .models import StrategySpec
from .prepared import prepare_discovery
from .prepared_search import (
    PREPARED_SEARCH_ENGINE,
    _evaluate_prepared_candidate,
    _runtime_parity_check,
)
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

INDEXED_SEARCH_ENGINE = "crossing_index_exact_v1"
RUNTIME_INDEX_PARITY_MAX_PAIRS = 12


def run_indexed_search(
    study_path: str | Path,
    search_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Run discovery search by scanning only candidate-independent crossing events.

    The prepared-exact engine is used as the immediate parity oracle and itself checks a
    deterministic sample against truth replay. Candidate generation, refinement, gates,
    aggregation and Pareto semantics remain imported from the reference search module.
    """

    context = load_study_context(study_path, data_directory=data_directory)
    spec = load_search_json(search_path)
    prepared = prepare_discovery(context, rsi_periods=spec.rsi_periods)
    indexed = build_crossing_index(prepared)
    if indexed.validation_accessed or indexed.holdout_accessed:
        raise RuntimeError("indexed search phase isolation failed")

    coarse = list(_coarse_candidates(context.spec.symbol, spec))
    if len(coarse) > spec.refinement.max_candidates:
        raise ValueError(
            f"coarse grid has {len(coarse)} candidates, above max_candidates="
            f"{spec.refinement.max_candidates}; widen steps before running"
        )

    truth_checked = _runtime_parity_check(
        context,
        prepared,
        coarse,
        sample_size=3,
    )
    index_checked = _runtime_index_parity_check(context, prepared, indexed, coarse)

    evaluated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for strategy in coarse:
        key = _strategy_key(strategy)
        if key in seen:
            continue
        seen.add(key)
        evaluated.append(
            _evaluate_indexed_candidate(context, indexed, strategy, stage="coarse")
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
                    _evaluate_indexed_candidate(
                        context,
                        indexed,
                        refined,
                        stage="refined",
                    )
                )
                refined_count += 1
        gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
        frontier = _pareto_frontier(gated)

    frontier = sorted(frontier, key=_seed_priority, reverse=True)
    event_counts = indexed_event_counts(indexed)
    full_checks_per_candidate = sum(
        max(0, len(window.prepared.candles) - 1) for window in indexed.windows
    )
    full_signal_filter_checks = full_checks_per_candidate * len(evaluated)
    indexed_signal_filter_checks = sum(
        event_counts[StrategySpec(**row["strategy"]).rsi_period]
        for row in evaluated
    )
    signal_filter_reduction_pct = (
        0.0
        if full_signal_filter_checks == 0
        else (1.0 - indexed_signal_filter_checks / full_signal_filter_checks) * 100.0
    )
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
        "search_engine": INDEXED_SEARCH_ENGINE,
        "reference_engine": PREPARED_SEARCH_ENGINE,
        "truth_runtime_parity_checked_candidates": truth_checked,
        "prepared_runtime_parity_checked_candidates": index_checked,
        "runtime_parity_passed": True,
        "indexed_event_counts_by_rsi_period": {
            str(period): count for period, count in sorted(event_counts.items())
        },
        "signal_filter_full_candle_checks_reference": full_signal_filter_checks,
        "signal_filter_indexed_event_checks": indexed_signal_filter_checks,
        "signal_filter_check_reduction_pct": signal_filter_reduction_pct,
        "signal_filter_telemetry_note": (
            "This measures only raw entry-signal filtering work. replay_signals still "
            "walks each candidate's full candle timeline, so this is not total runtime speedup."
        ),
        "prepared_discovery_window_count": len(indexed.windows),
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


def _runtime_index_parity_check(
    context: StudyContext,
    prepared: Any,
    indexed: IndexedDiscovery,
    candidates: Sequence[StrategySpec],
) -> int:
    sample = _stratified_parity_sample(candidates)
    for index, strategy in enumerate(sample):
        reference = _evaluate_prepared_candidate(
            context,
            prepared,
            strategy,
            stage="coarse",
        )
        candidate = _evaluate_indexed_candidate(
            context,
            indexed,
            strategy,
            stage="coarse",
        )
        if candidate != reference:
            raise RuntimeError(
                "crossing-index runtime parity failed for deterministic sample "
                f"{index}: {asdict(strategy)}"
            )
    return len(sample)


def _stratified_parity_sample(
    candidates: Sequence[StrategySpec],
) -> list[StrategySpec]:
    sample: list[StrategySpec] = []
    seen_pairs: set[tuple[int, str]] = set()
    for strategy in candidates:
        pair = (strategy.rsi_period, strategy.exit_mode)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        sample.append(strategy)
        if len(sample) >= RUNTIME_INDEX_PARITY_MAX_PAIRS:
            break
    if candidates and candidates[-1] not in sample and len(sample) < RUNTIME_INDEX_PARITY_MAX_PAIRS:
        sample.append(candidates[-1])
    return sample


def _evaluate_indexed_candidate(
    context: StudyContext,
    indexed: IndexedDiscovery,
    strategy: StrategySpec,
    *,
    stage: str,
) -> dict[str, Any]:
    compact_windows = evaluate_indexed_discovery(context, indexed, strategy)
    return {
        "stage": stage,
        "strategy": asdict(strategy),
        "aggregate": _aggregate(compact_windows),
        "windows": compact_windows,
    }
