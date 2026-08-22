import json
import math
from pathlib import Path

import parameter_extract.bulk_search as bulk_search_module
import parameter_extract.query_search as query_search_module
from parameter_extract.bulk_entry import bulk_prime_entry_signals
from parameter_extract.bulk_search import run_bulk_search
from parameter_extract.crossing_index import build_crossing_index
from parameter_extract.entry_signal_cache import EntrySignalCache
from parameter_extract.models import Candle, ExecutionModel, FundingEvent, StrategySpec
from parameter_extract.prepared import prepare_discovery
from parameter_extract.query_search import run_query_search
from parameter_extract.study import StudyContext, StudySpec, WindowSpec


def _context() -> StudyContext:
    base = 1_750_000_000_000
    candles: list[Candle] = []
    gap_offset = 0
    for index in range(620):
        if index == 365:
            gap_offset += 60_000
        open_time = base + index * 60_000 + gap_offset
        close = (
            100.0
            + 6.2 * math.sin(index / 4.9)
            + 1.0 * math.sin(index / 11.5)
            + 0.006 * index
        )
        open_value = (
            100.0
            + 6.2 * math.sin((index - 0.5) / 4.9)
            + 1.0 * math.sin((index - 0.5) / 11.5)
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
        name="bulk entry parity",
        symbol="BTCUSDT",
        dataset_manifest="unused.json",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
        discovery=(WindowSpec("d1", candles[300].open_time_ms, candles[450].open_time_ms),),
        validation=(WindowSpec("v1", candles[450].open_time_ms, candles[550].open_time_ms),),
        holdout=(WindowSpec("h1", candles[550].open_time_ms, candles[610].open_time_ms),),
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


def _tp(
    *,
    period: int = 14,
    entry: float = 45.0,
    adx_min: float = 0.0,
    adx_max: float = 100.0,
    tp: float = 0.2,
) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=adx_min,
        adx_max=adx_max,
        exit_mode="tp",
        tp_price_pct=tp,
    )


def _rsi(
    *,
    period: int = 14,
    entry: float = 45.0,
    adx_min: float = 0.0,
    adx_max: float = 100.0,
    exit_value: float = 60.0,
) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=adx_min,
        adx_max=adx_max,
        exit_mode="rsi",
        rsi_exit=exit_value,
    )


def _search_payload() -> dict:
    return {
        "schema_version": 1,
        "name": "bulk entry search",
        "exit_modes": ["tp", "rsi"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14, 15],
            "rsi_entry": {"start": 30.0, "stop": 60.0, "step": 15.0},
            "adx_min": {"start": 0.0, "stop": 20.0, "step": 10.0},
            "adx_max": {"start": 70.0, "stop": 100.0, "step": 15.0},
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
            "max_candidates": 5000,
        },
    }


def test_bulk_prime_matches_keywise_cache_for_entry_and_exit_variants():
    context = _context()
    prepared = prepare_discovery(context, rsi_periods=(14, 15))
    indexed = build_crossing_index(prepared)
    reference = EntrySignalCache.create(indexed)
    bulk = EntrySignalCache.create(indexed)

    strategies = [
        _tp(period=14, entry=30.0, adx_min=0.0, adx_max=100.0, tp=0.2),
        _tp(period=14, entry=30.0, adx_min=0.0, adx_max=100.0, tp=0.6),
        _rsi(period=14, entry=30.0, adx_min=0.0, adx_max=100.0),
        _tp(period=14, entry=45.0, adx_min=10.0, adx_max=85.0),
        _rsi(period=15, entry=45.0, adx_min=20.0, adx_max=100.0),
        _tp(period=15, entry=60.0, adx_min=0.0, adx_max=70.0),
    ]
    stats = bulk_prime_entry_signals(bulk, strategies)

    unique_keys = {EntrySignalCache.key(strategy) for strategy in strategies}
    assert stats.installed_keys == len(unique_keys)
    assert bulk.unique_keys == len(unique_keys)
    assert stats.event_visits > 0
    assert stats.keywise_event_scan_upper_bound > stats.event_visits

    for strategy in strategies:
        assert bulk.signals(strategy) == reference.signals(strategy)

    assert bulk.misses == 0
    assert bulk.hits == len(strategies)
    assert reference.misses == len(unique_keys)


def test_bulk_prime_only_scans_new_keys_on_second_stage():
    context = _context()
    indexed = build_crossing_index(prepare_discovery(context, rsi_periods=(14,)))
    cache = EntrySignalCache.create(indexed)

    first = [_tp(entry=30.0), _tp(entry=45.0), _rsi(entry=45.0)]
    first_stats = bulk_prime_entry_signals(cache, first)
    assert first_stats.installed_keys == 2

    second = [_tp(entry=45.0, tp=0.6), _tp(entry=60.0)]
    second_stats = bulk_prime_entry_signals(cache, second)
    assert second_stats.already_cached_keys == 1
    assert second_stats.installed_keys == 1
    assert cache.unique_keys == 3


def test_bulk_full_search_matches_query_frontier_and_has_no_fallback_misses(
    tmp_path: Path,
    monkeypatch,
):
    context = _context()
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload()), encoding="utf-8")

    monkeypatch.setattr(
        query_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        bulk_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )

    reference = run_query_search("study.json", search_path, data_directory=tmp_path)
    actual = run_bulk_search("study.json", search_path, data_directory=tmp_path)

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

    assert actual["search_engine"] == "bulk_entry_membership_exact_v1"
    assert actual["reference_engine"] == "exit_query_exact_v1"
    assert actual["runtime_parity_passed"] is True
    assert actual["bulk_runtime_parity_checked_candidates"] >= 4
    assert actual["entry_signal_cache_misses"] == 0
    assert actual["entry_signal_cache_hits"] == actual["evaluated_candidates"]
    assert actual["bulk_entry_installed_keys"] == actual["unique_entry_signal_keys"]
    assert actual["keywise_event_scan_upper_bound"] > actual["bulk_entry_event_visits"]
    assert 0.0 < actual["event_scan_reduction_fraction"] < 1.0
    assert actual["frontier"]
    assert actual["refined_candidates"] > 0
