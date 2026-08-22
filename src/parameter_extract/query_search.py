from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .cached_search import (
    CACHED_SEARCH_ENGINE,
    _evaluate_cached_candidate,
    _runtime_cache_parity_check,
)
from .crossing_index import build_crossing_index
from .entry_signal_cache import EntrySignalCache
from .exit_query import (
    ExitQueryDiscovery,
    QueryReplayStats,
    build_exit_query_index,
    evaluate_query_discovery,
)
from .indexed_search import _runtime_index_parity_check, _stratified_parity_sample
from .models import StrategySpec
from .prepared import prepare_discovery
from .prepared_search import _runtime_parity_check
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
from .work_profile import QueryWorkProfile

QUERY_SEARCH_ENGINE = "exit_query_exact_v1"


def run_query_search(
    study_path: str | Path,
    search_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Run discovery search with exact first-exit and range-extrema queries.

    Entry signals still come from the exact entry-signal membership cache. Candidate grid,
    refinement, gates and Pareto semantics remain imported from the original search module.
    """
    context = load_study_context(study_path, data_directory=data_directory)
    spec = load_search_json(search_path)
    prepared = prepare_discovery(context, rsi_periods=spec.rsi_periods)
    indexed = build_crossing_index(prepared)
    cache = EntrySignalCache.create(indexed)
    query = build_exit_query_index(indexed)

    coarse = list(_coarse_candidates(context.spec.symbol, spec))
    if len(coarse) > spec.refinement.max_candidates:
        raise ValueError(
            f"coarse grid has {len(coarse)} candidates, above max_candidates="
            f"{spec.refinement.max_candidates}; widen steps before running"
        )

    truth_checked = _runtime_parity_check(context, prepared, coarse, sample_size=3)
    indexed_checked = _runtime_index_parity_check(context, prepared, indexed, coarse)
    cache_checked = _runtime_cache_parity_check(context, cache, coarse)
    query_checked = _runtime_query_parity_check(context, cache, query, coarse)
    cache.clear()

    work_profile = QueryWorkProfile()
    evaluated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for strategy in coarse:
        key = _strategy_key(strategy)
        if key in seen:
            continue
        seen.add(key)
        evaluated.append(
            _evaluate_query_candidate(
                context,
                cache,
                query,
                strategy,
                stage="coarse",
                work_profile=work_profile,
            )
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
                    _evaluate_query_candidate(
                        context,
                        cache,
                        query,
                        refined,
                        stage="refined",
                        work_profile=work_profile,
                    )
                )
                refined_count += 1
        gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
        frontier = _pareto_frontier(gated)

    frontier = sorted(frontier, key=_seed_priority, reverse=True)
    cache_requests = cache.hits + cache.misses
    cache_hit_fraction = 0.0 if cache_requests == 0 else cache.hits / cache_requests
    reference_candle_visits_upper_bound = len(evaluated) * sum(
        len(window.indexed.prepared.candles) for window in query.windows
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
        "search_engine": QUERY_SEARCH_ENGINE,
        "reference_engine": CACHED_SEARCH_ENGINE,
        "truth_runtime_parity_checked_candidates": truth_checked,
        "indexed_runtime_parity_checked_candidates": indexed_checked,
        "cache_runtime_parity_checked_candidates": cache_checked,
        "query_runtime_parity_checked_candidates": query_checked,
        "runtime_parity_passed": True,
        "entry_signal_cache_requests": cache_requests,
        "entry_signal_cache_misses": cache.misses,
        "entry_signal_cache_hits": cache.hits,
        "entry_signal_cache_hit_fraction": cache_hit_fraction,
        "unique_entry_signal_keys": cache.unique_keys,
        "query_work_profile": work_profile.as_dict(),
        "reference_full_candle_replay_visits_upper_bound": reference_candle_visits_upper_bound,
        "replay_acceleration_note": (
            "The query engine skips the per-candidate full candle replay loop for exit discovery. "
            "Funding work is range-selected by exact candle-index bisects before preserving the "
            "original chronological event arithmetic. The work profile contains deterministic "
            "logical counters; these counters are not measured wall-clock speedups."
        ),
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


def _runtime_query_parity_check(
    context: StudyContext,
    cache: EntrySignalCache,
    query: ExitQueryDiscovery,
    candidates: Sequence[StrategySpec],
) -> int:
    sample = _stratified_parity_sample(candidates)
    first_by_entry: dict[tuple[int, float, float, float], StrategySpec] = {}
    for strategy in candidates:
        key = EntrySignalCache.key(strategy)
        previous = first_by_entry.get(key)
        if previous is not None and (
            previous.exit_mode != strategy.exit_mode
            or previous.rsi_exit != strategy.rsi_exit
            or previous.tp_price_pct != strategy.tp_price_pct
        ):
            if strategy not in sample:
                sample.append(strategy)
            break
        first_by_entry[key] = strategy

    for index, strategy in enumerate(sample):
        reference = _evaluate_cached_candidate(context, cache, strategy, stage="coarse")
        candidate = _evaluate_query_candidate(context, cache, query, strategy, stage="coarse")
        if candidate != reference:
            raise RuntimeError(
                "exit-query runtime parity failed for deterministic sample "
                f"{index}: {asdict(strategy)}"
            )
    return len(sample)


def _evaluate_query_candidate(
    context: StudyContext,
    cache: EntrySignalCache,
    query: ExitQueryDiscovery,
    strategy: StrategySpec,
    *,
    stage: str,
    work_profile: QueryWorkProfile | None = None,
) -> dict[str, Any]:
    replay_stats = QueryReplayStats() if work_profile is not None else None
    compact_windows = evaluate_query_discovery(
        context,
        cache,
        query,
        strategy,
        work_stats=replay_stats,
    )
    if work_profile is not None:
        assert replay_stats is not None
        work_profile.record(compact_windows, query.windows, replay_stats=replay_stats)
    return {
        "stage": stage,
        "strategy": asdict(strategy),
        "aggregate": _aggregate(compact_windows),
        "windows": compact_windows,
    }
