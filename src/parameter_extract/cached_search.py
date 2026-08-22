from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .crossing_index import build_crossing_index
from .entry_signal_cache import EntrySignalCache, evaluate_cached_discovery
from .indexed_search import (
    INDEXED_SEARCH_ENGINE,
    _evaluate_indexed_candidate,
    _runtime_index_parity_check,
    _stratified_parity_sample,
)
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

CACHED_SEARCH_ENGINE = "entry_signal_cache_exact_v1"


def run_cached_search(
    study_path: str | Path,
    search_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Reuse raw signal membership across candidates that share the same entry key.

    Exit mode/threshold parameters are deliberately excluded from the cache key because
    they cannot change raw entry signals. Every candidate still executes through the same
    truth replay, so this layer factors signal generation only.
    """

    context = load_study_context(study_path, data_directory=data_directory)
    spec = load_search_json(search_path)
    prepared = prepare_discovery(context, rsi_periods=spec.rsi_periods)
    indexed = build_crossing_index(prepared)
    cache = EntrySignalCache.create(indexed)

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
    indexed_checked = _runtime_index_parity_check(context, prepared, indexed, coarse)
    cache_checked = _runtime_cache_parity_check(context, cache, coarse)
    cache.clear()

    evaluated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for strategy in coarse:
        key = _strategy_key(strategy)
        if key in seen:
            continue
        seen.add(key)
        evaluated.append(
            _evaluate_cached_candidate(context, cache, strategy, stage="coarse")
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
                    _evaluate_cached_candidate(
                        context,
                        cache,
                        refined,
                        stage="refined",
                    )
                )
                refined_count += 1
        gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
        frontier = _pareto_frontier(gated)

    frontier = sorted(frontier, key=_seed_priority, reverse=True)
    requests = cache.hits + cache.misses
    hit_fraction = 0.0 if requests == 0 else cache.hits / requests
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
        "search_engine": CACHED_SEARCH_ENGINE,
        "reference_engine": INDEXED_SEARCH_ENGINE,
        "truth_runtime_parity_checked_candidates": truth_checked,
        "indexed_runtime_parity_checked_candidates": indexed_checked,
        "cache_runtime_parity_checked_candidates": cache_checked,
        "runtime_parity_passed": True,
        "entry_signal_cache_key_fields": [
            "rsi_period",
            "rsi_entry",
            "adx_min",
            "adx_max",
        ],
        "entry_signal_cache_requests": requests,
        "entry_signal_cache_misses": cache.misses,
        "entry_signal_cache_hits": cache.hits,
        "entry_signal_cache_hit_fraction": hit_fraction,
        "unique_entry_signal_keys": cache.unique_keys,
        "cache_telemetry_note": (
            "Cache reuse removes repeated raw signal membership work across exit variants. "
            "Every candidate is still replayed through the full truth execution timeline."
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


def _runtime_cache_parity_check(
    context: StudyContext,
    cache: EntrySignalCache,
    candidates: Sequence[StrategySpec],
) -> int:
    sample = _stratified_parity_sample(candidates)
    # Add a same-entry/different-exit candidate when possible so parity also exercises
    # the factorization boundary itself rather than only distinct cache keys.
    first_by_entry: dict[tuple[int, float, float, float], StrategySpec] = {}
    for strategy in candidates:
        key = EntrySignalCache.key(strategy)
        previous = first_by_entry.get(key)
        if previous is not None and previous.exit_mode != strategy.exit_mode:
            if strategy not in sample:
                sample.append(strategy)
            break
        first_by_entry[key] = strategy

    for index, strategy in enumerate(sample):
        reference = _evaluate_indexed_candidate(
            context,
            cache.indexed,
            strategy,
            stage="coarse",
        )
        candidate = _evaluate_cached_candidate(
            context,
            cache,
            strategy,
            stage="coarse",
        )
        if candidate != reference:
            raise RuntimeError(
                "entry-signal cache runtime parity failed for deterministic sample "
                f"{index}: {asdict(strategy)}"
            )
    return len(sample)


def _evaluate_cached_candidate(
    context: StudyContext,
    cache: EntrySignalCache,
    strategy: StrategySpec,
    *,
    stage: str,
) -> dict[str, Any]:
    compact_windows = evaluate_cached_discovery(context, cache, strategy)
    return {
        "stage": stage,
        "strategy": asdict(strategy),
        "aggregate": _aggregate(compact_windows),
        "windows": compact_windows,
    }
