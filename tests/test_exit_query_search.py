import json
import math
from pathlib import Path

import parameter_extract.cached_search as cached_search_module
import parameter_extract.query_search as query_search_module
from parameter_extract.cached_search import _evaluate_cached_candidate, run_cached_search
from parameter_extract.crossing_index import IndexedDiscovery, IndexedWindow, build_crossing_index
from parameter_extract.entry_signal_cache import EntrySignalCache
from parameter_extract.exit_query import build_exit_query_index, replay_signals_query
from parameter_extract.indicators import IndicatorPoint
from parameter_extract.models import Candle, ExecutionModel, FundingEvent, StrategySpec
from parameter_extract.prepared import PreparedDiscovery, PreparedWindow, prepare_discovery
from parameter_extract.query_search import _evaluate_query_candidate, run_query_search
from parameter_extract.replay import replay_signals
from parameter_extract.signals import Signal
from parameter_extract.study import StudyContext, StudySpec, WindowSpec


def _context() -> StudyContext:
    base = 1_730_000_000_000
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
        name="exit query parity",
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
        FundingEvent(
            timestamp_ms=candles[430].open_time_ms + 30_000,
            rate=-0.00005,
            mark_price=candles[430].close,
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
        "name": "exit query search",
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


def _manual_query_window(*, rsi_values: list[float], funding=()):
    base = 1_740_000_000_000
    candles: list[Candle] = []
    gap_offset = 0
    highs = [100.2, 100.2, 100.2, 100.2, 100.2, 100.1, 101.0, 100.1]
    lows = [99.8, 99.8, 99.8, 99.8, 99.8, 99.0, 98.5, 99.5]
    for index in range(8):
        if index == 3:
            gap_offset += 60_000
        open_time = base + index * 60_000 + gap_offset
        candles.append(
            Candle(
                open_time_ms=open_time,
                close_time_ms=open_time + 59_999,
                open=100.0,
                high=highs[index],
                low=lows[index],
                close=100.0,
            )
        )
    points = tuple(IndicatorPoint(rsi=value, adx=25.0, adr=1.0) for value in rsi_values)
    prepared_window = PreparedWindow(
        name="manual",
        start_ms=candles[0].open_time_ms,
        end_ms=candles[-1].close_time_ms + 1,
        warmup_candles=0,
        candles=tuple(candles),
        funding=tuple(funding),
        segment_starts=frozenset({0, 3}),
        adx=tuple(25.0 for _ in candles),
        adr=tuple(1.0 for _ in candles),
        rsi_by_period={14: tuple(rsi_values)},
    )
    indexed_window = IndexedWindow(
        prepared=prepared_window,
        points_by_period={14: points},
        events_by_period={14: ()},
    )
    prepared = PreparedDiscovery(
        study_name="manual",
        symbol="BTCUSDT",
        dataset_fingerprint_sha256="b" * 64,
        rsi_periods=(14,),
        windows=(prepared_window,),
    )
    indexed = IndexedDiscovery(prepared=prepared, windows=(indexed_window,))
    return candles, points, build_exit_query_index(indexed).windows[0]


def _signal(index: int, candle: Candle) -> Signal:
    previous = IndicatorPoint(rsi=40.0, adx=25.0, adr=1.0)
    current = IndicatorPoint(rsi=50.0, adx=25.0, adr=2.0)
    return Signal(
        candle_index=index,
        timestamp_ms=candle.close_time_ms,
        reference_price=candle.close,
        previous=previous,
        current=current,
    )


def test_query_replay_matches_truth_for_gap_skip_exit_candle_reuse_and_tp_funding():
    base = 1_740_000_000_000
    funding = (
        FundingEvent(timestamp_ms=base + 7 * 60_000 + 30_000, rate=0.001),
        FundingEvent(timestamp_ms=base + 7 * 60_000 + 40_000, rate=-0.001),
    )
    candles, points, query_window = _manual_query_window(
        rsi_values=[40.0] * 8,
        funding=funding,
    )
    # Because of the inserted gap, candle 6 opens at base + 7 minutes. Both events land
    # on the TP exit candle: the positive rate is charged, the negative benefit withheld.
    strategy = _tp(entry=45.0, tp=0.5)
    signals = tuple(_signal(index, candles[index]) for index in (2, 4, 5, 6))
    execution = ExecutionModel.expected_live()

    truth = replay_signals(
        candles,
        strategy,
        signals,
        points,
        execution=execution,
        funding=funding,
    )
    actual = replay_signals_query(
        query_window,
        strategy,
        signals,
        execution=execution,
    )
    assert actual == truth
    assert actual.cancelled_on_gap == 1
    assert actual.skipped_while_open == 1
    assert actual.accepted_signal_count == 2
    assert len(actual.trades) == 1
    assert actual.open_position is not None
    assert actual.trades[0].funding_return_pct < 0.0


def test_query_replay_matches_truth_for_signal_close_rsi_exit_timing():
    candles, points, query_window = _manual_query_window(
        rsi_values=[40.0, 80.0, 40.0, 80.0, 40.0, 80.0, 40.0, 40.0]
    )
    strategy = _rsi(entry=45.0, exit_value=60.0)
    signals = tuple(_signal(index, candles[index]) for index in (1, 2, 3))
    execution = ExecutionModel.frictionless()

    truth = replay_signals(
        candles,
        strategy,
        signals,
        points,
        execution=execution,
    )
    actual = replay_signals_query(
        query_window,
        strategy,
        signals,
        execution=execution,
    )
    assert actual == truth
    # RSI is already above the threshold on signal candle 1, but signal-close entry occurs
    # after that candle's exit check; the first legal RSI exit is therefore later.
    assert actual.trades[0].exit_time_ms == candles[3].close_time_ms


def test_query_candidates_match_cache_backed_truth_for_multiple_exit_variants():
    context = _context()
    prepared = prepare_discovery(context, rsi_periods=(14, 15))
    indexed = build_crossing_index(prepared)
    cache = EntrySignalCache.create(indexed)
    query = build_exit_query_index(indexed)

    for strategy in (
        _tp(period=14, tp=0.2),
        _tp(period=14, tp=0.6),
        _rsi(period=14, exit_value=60.0),
        _rsi(period=15, exit_value=65.0),
    ):
        reference = _evaluate_cached_candidate(context, cache, strategy, stage="coarse")
        actual = _evaluate_query_candidate(context, cache, query, strategy, stage="coarse")
        assert actual == reference


def test_query_full_search_matches_cached_frontier(tmp_path: Path, monkeypatch):
    context = _context()
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload()), encoding="utf-8")

    monkeypatch.setattr(
        cached_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        query_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )

    reference = run_cached_search("study.json", search_path, data_directory=tmp_path)
    actual = run_query_search("study.json", search_path, data_directory=tmp_path)

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

    assert actual["search_engine"] == "exit_query_exact_v1"
    assert actual["reference_engine"] == "entry_signal_cache_exact_v1"
    assert actual["runtime_parity_passed"] is True
    assert actual["query_runtime_parity_checked_candidates"] >= 4
    assert actual["entry_signal_cache_requests"] == actual["evaluated_candidates"]
    assert actual["entry_signal_cache_hits"] > 0
    assert actual["frontier"]
    assert actual["refined_candidates"] > 0
    assert actual["reference_full_candle_replay_visits_upper_bound"] > 0
