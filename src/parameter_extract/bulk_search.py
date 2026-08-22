from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .bulk_entry import BulkPrimeStats, bulk_prime_entry_signals
from .cached_search import _runtime_cache_parity_check
from .crossing_index import build_crossing_index
from .entry_signal_cache import EntrySignalCache
from .exit_query import build_exit_query_index
from .indexed_search import _runtime_index_parity_check, _stratified_parity_sample
from .models import StrategySpec
from .prepared import prepare_discovery
from .prepared_search import _runtime_parity_check
from .query_search import (
    QUERY_SEARCH_ENGINE,
    _evaluate_query_candidate,
    _runtime_query_parity_check,
)
from .search import (
    PARETO_OBJECTIVES,
    _coarse_candidates,
    _pareto_frontier,
    _passes_gates,
    _refined_candidates,
    _seed_priority,
    _strategy_key,
    load_search_json,
    search_fingerprint,
)
from .study import load_study_context, study_fingerprint
from .work_profile import QueryWorkProfile

BULK_SEARCH_ENGINE = "bulk_entry_membership_exact_v1"


def run_bulk_search(
    study_path: str | Path,
    search_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    spec = load_search_json(search_path)
    prepared = prepare_discovery(context, rsi_periods=spec.rsi_periods)
    indexed = build_crossing_index(prepared)
    query = build_exit_query_index(indexed)

    coarse = list(_coarse_candidates(context.spec.symbol, spec))
    if len(coarse) > spec.refinement.max_candidates:
        raise ValueError(
            f"coarse grid has {len(coarse)} candidates, above max_candidates="
            f"{spec.refinement.max_candidates}; widen steps before running"
        )

    parity_cache = EntrySignalCache.create(indexed)
    truth_checked = _runtime_parity_check(context, prepared, coarse, sample_size=3)
    indexed_checked = _runtime_index_parity_check(context, prepared, indexed, coarse)
    cache_checked = _runtime_cache_parity_check(context, parity_cache, coarse)
    query_checked = _runtime_query_parity_check(context, parity_cache, query, coarse)
    bulk_checked, bulk_preflight_stats = _runtime_bulk_parity_check(indexed, coarse)

    cache = EntrySignalCache.create(indexed)
    bulk_stats = bulk_prime_entry_signals(cache, coarse)
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
        refined_strategies: list[StrategySpec] = []
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
                refined_strategies.append(refined)

        if refined_strategies:
            bulk_stats = bulk_stats.plus(
                bulk_prime_entry_signals(cache, refined_strategies)
            )
            for refined in refined_strategies:
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
    scan_reduction_fraction = (
        0.0
        if bulk_stats.keywise_event_scan_upper_bound == 0
        else 1.0 - bulk_stats.event_visits / bulk_stats.keywise_event_scan_upper_bound
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
        "search_engine": BULK_SEARCH_ENGINE,
        "reference_engine": QUERY_SEARCH_ENGINE,
        "truth_runtime_parity_checked_candidates": truth_checked,
        "indexed_runtime_parity_checked_candidates": indexed_checked,
        "cache_runtime_parity_checked_candidates": cache_checked,
        "query_runtime_parity_checked_candidates": query_checked,
        "bulk_runtime_parity_checked_candidates": bulk_checked,
        "bulk_runtime_parity_installed_keys": bulk_preflight_stats.installed_keys,
        "runtime_parity_passed": True,
        "entry_signal_cache_requests": cache_requests,
        "entry_signal_cache_misses": cache.misses,
        "entry_signal_cache_hits": cache.hits,
        "entry_signal_cache_hit_fraction": cache_hit_fraction,
        "unique_entry_signal_keys": cache.unique_keys,
        "bulk_entry_installed_keys": bulk_stats.installed_keys,
        "bulk_entry_event_visits": bulk_stats.event_visits,
        "bulk_entry_band_membership_checks": bulk_stats.band_membership_checks,
        "keywise_event_scan_upper_bound": bulk_stats.keywise_event_scan_upper_bound,
        "event_scan_reduction_fraction": scan_reduction_fraction,
        "query_work_profile": work_profile.as_dict(),
        "bulk_telemetry_note": (
            "Event-scan reduction and query-work fields are deterministic logical work "
            "counters, not wall-clock speed claims."
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


def _runtime_bulk_parity_check(
    indexed: Any,
    candidates: Sequence[StrategySpec],
) -> tuple[int, BulkPrimeStats]:
    sample = _stratified_parity_sample(candidates)
    if candidates and candidates[-1] not in sample:
        sample.append(candidates[-1])

    reference = EntrySignalCache.create(indexed)
    candidate = EntrySignalCache.create(indexed)
    stats = bulk_prime_entry_signals(candidate, sample)
    for index, strategy in enumerate(sample):
        expected = reference.signals(strategy)
        actual = candidate.signals(strategy)
        if actual != expected:
            raise RuntimeError(
                "bulk entry-membership runtime parity failed for deterministic sample "
                f"{index}: {asdict(strategy)}"
            )
    return len(sample), stats
