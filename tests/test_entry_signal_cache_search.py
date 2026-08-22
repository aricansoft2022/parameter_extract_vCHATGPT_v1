import json
import math
from pathlib import Path

import parameter_extract.cached_search as cached_search_module
import parameter_extract.indexed_search as indexed_search_module
from parameter_extract.cached_search import (
    _evaluate_cached_candidate,
    run_cached_search,
)
from parameter_extract.crossing_index import build_crossing_index
from parameter_extract.entry_signal_cache import EntrySignalCache, evaluate_cached_discovery
from parameter_extract.indexed_search import (
    _evaluate_indexed_candidate,
    run_indexed_search,
)
from parameter_extract.models import Candle, ExecutionModel, FundingEvent, StrategySpec
from parameter_extract.prepared import prepare_discovery
from parameter_extract.study import StudyContext, StudySpec, WindowSpec


def _context() -> StudyContext:
    base = 1_720_000_000_000
    candles: list[Candle] = []
    gap_offset = 0
    for index in range(620):
        if index == 365:
            gap_offset += 60_000
        open_time = base + index * 60_000 + gap_offset
        close = (
            100.0
            + 6.0 * math.sin(index / 4.9)
            + 1.1 * math.sin(index / 11.8)
            + 0.006 * index
        )
        open_value = (
            100.0
            + 6.0 * math.sin((index - 0.5) / 4.9)
            + 1.1 * math.sin((index - 0.5) / 11.8)
            + 0.006 * (index - 0.5)
        )
        spread = 0.55 + 0.1 * (1.0 + math.sin(index / 8.0))
        candles.append(
            Candle(
                open_time_ms=open_time,
                close_time_ms=open_time + 59_999,
                open=open_value,
                high=max(open_value, close) + spread,
                low=min(open_value, close) - spread,
                close=close,
                volume=100.0 + index,
            )
        )

    spec = StudySpec(
        name="entry cache parity",
        symbol="BTCUSDT",
        dataset_manifest="unused.json",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
        discovery=(
            WindowSpec("d1", candles[300].open_time_ms, candles[450].open_time_ms),
        ),
        validation=(
            WindowSpec("v1", candles[450].open_time_ms, candles[550].open_time_ms),
        ),
        holdout=(
            WindowSpec("h1", candles[550].open_time_ms, candles[610].open_time_ms),
        ),
        warmup_candles=300,
        min_trades=1,
    )
    funding = (
        FundingEvent(
            timestamp_ms=candles[390].open_time_ms + 30_000,
            rate=0.0001,
            mark_price=candles[390].close,
        ),
    )
    return StudyContext(spec=spec, candles=tuple(candles), funding=funding)


def _tp(*, period: int = 14, entry: float = 45.0, tp: float = 0.2) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=0.0,
        adx_max=100.0,
        exit_mode="tp",
        tp_price_pct=tp,
    )


def _rsi(*, period: int = 14, entry: float = 45.0, exit_value: float = 60.0) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=0.0,
        adx_max=100.0,
        exit_mode="rsi",
        rsi_exit=exit_value,
    )


def _search_payload() -> dict:
    return {
        "schema_version": 1,
        "name": "entry cache search",
        "exit_modes": ["tp", "rsi"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14, 15],
            "rsi_entry": {"start": 30.0, "stop": 60.0, "step": 15.0},
            "adx_min": {"start": 0.0, "stop": 0.0, "step": 1.0},
            "adx_max": {"start": 100.0, "stop": 100.0, "step": 1.0},
            "tp_price_pct": {"start": 0.2, "stop": 0.6, "step": 0.2},
            "rsi_exit": {"start": 55.0, "stop": 70.0, "step": 5.0},
        },
        "gates": {
            "min_total_trades": 1,
            "min_positive_window_fraction": 0.0,
        },
        "refinement": {
            "enabled": True,
            "step_divisor": 2,
            "radius_steps": 1,
            "max_seeds": 2,
            "max_candidates": 1500,
        },
    }


def test_entry_signal_cache_reuses_same_entry_key_across_exit_variants():
    context = _context()
    prepared = prepare_discovery(context, rsi_periods=(14,))
    indexed = build_crossing_index(prepared)
    cache = EntrySignalCache.create(indexed)

    tp = _tp(tp=0.2)
    tp_other = _tp(tp=0.6)
    rsi = _rsi(exit_value=60.0)
    assert EntrySignalCache.key(tp) == EntrySignalCache.key(tp_other)
    assert EntrySignalCache.key(tp) == EntrySignalCache.key(rsi)

    first = cache.signals(tp)
    second = cache.signals(tp_other)
    third = cache.signals(rsi)
    assert first is second
    assert first is third
    assert cache.misses == 1
    assert cache.hits == 2
    assert cache.unique_keys == 1


def test_cached_candidate_matches_crossing_index_for_different_exits():
    context = _context()
    prepared = prepare_discovery(context, rsi_periods=(14, 15))
    indexed = build_crossing_index(prepared)
    cache = EntrySignalCache.create(indexed)

    for strategy in (
        _tp(period=14, tp=0.2),
        _tp(period=14, tp=0.6),
        _rsi(period=14, exit_value=60.0),
        _rsi(period=15, exit_value=65.0),
    ):
        reference = _evaluate_indexed_candidate(
            context,
            indexed,
            strategy,
            stage="coarse",
        )
        actual = _evaluate_cached_candidate(
            context,
            cache,
            strategy,
            stage="coarse",
        )
        assert actual == reference

    assert cache.hits > 0
    assert evaluate_cached_discovery(context, cache, _tp()) == (
        _evaluate_indexed_candidate(context, indexed, _tp(), stage="coarse")["windows"]
    )


def test_cached_full_search_matches_indexed_frontier_and_reuses_keys(tmp_path: Path, monkeypatch):
    context = _context()
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload()), encoding="utf-8")

    monkeypatch.setattr(
        indexed_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        cached_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )

    reference = run_indexed_search("study.json", search_path, data_directory=tmp_path)
    actual = run_cached_search("study.json", search_path, data_directory=tmp_path)

    for key in (
        "search_fingerprint_sha256",
        "study_fingerprint_sha256",
        "dataset_fingerprint_sha256",
        "execution",
        "symbol",
        "phase_used",
        "validation_accessed",
        "holdout_accessed",
        "pareto_objectives",
        "coarse_candidates",
        "refined_candidates",
        "evaluated_candidates",
        "passed_gates",
        "pareto_candidates",
        "frontier",
    ):
        assert actual[key] == reference[key]

    assert actual["search_engine"] == "entry_signal_cache_exact_v1"
    assert actual["reference_engine"] == "crossing_index_exact_v1"
    assert actual["runtime_parity_passed"] is True
    assert actual["entry_signal_cache_requests"] == actual["evaluated_candidates"]
    assert actual["entry_signal_cache_misses"] == actual["unique_entry_signal_keys"]
    assert actual["entry_signal_cache_hits"] > 0
    assert actual["entry_signal_cache_misses"] < actual["evaluated_candidates"]
    assert 0.0 < actual["entry_signal_cache_hit_fraction"] < 1.0
    assert actual["frontier"]
    assert actual["refined_candidates"] > 0
