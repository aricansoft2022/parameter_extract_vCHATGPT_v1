import json
import math
from pathlib import Path

import parameter_extract.indexed_search as indexed_search_module
import parameter_extract.prepared as prepared_module
import parameter_extract.prepared_search as prepared_search_module
from parameter_extract.crossing_index import (
    build_crossing_index,
    evaluate_indexed_discovery,
    indexed_event_counts,
    indexed_signals_for_strategy,
)
from parameter_extract.indexed_search import (
    _evaluate_indexed_candidate,
    _stratified_parity_sample,
    run_indexed_search,
)
from parameter_extract.models import Candle, ExecutionModel, FundingEvent, StrategySpec
from parameter_extract.prepared import evaluate_prepared_discovery, prepare_discovery
from parameter_extract.prepared_search import (
    _evaluate_prepared_candidate,
    run_prepared_search,
)
from parameter_extract.study import StudyContext, StudySpec, WindowSpec


def _context() -> StudyContext:
    base = 1_710_000_000_000
    candles: list[Candle] = []
    gap_offset = 0
    for index in range(620):
        if index == 360:
            gap_offset += 60_000
        open_time = base + index * 60_000 + gap_offset
        close = (
            100.0
            + 5.8 * math.sin(index / 4.8)
            + 1.3 * math.sin(index / 12.3)
            + 0.007 * index
        )
        open_value = (
            100.0
            + 5.8 * math.sin((index - 0.5) / 4.8)
            + 1.3 * math.sin((index - 0.5) / 12.3)
            + 0.007 * (index - 0.5)
        )
        spread = 0.5 + 0.1 * (1.0 + math.sin(index / 7.0))
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
        name="crossing index parity",
        symbol="BTCUSDT",
        dataset_manifest="unused.json",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
        discovery=(
            WindowSpec(
                "d1",
                candles[300].open_time_ms,
                candles[450].open_time_ms,
            ),
        ),
        validation=(
            WindowSpec(
                "v1",
                candles[450].open_time_ms,
                candles[550].open_time_ms,
            ),
        ),
        holdout=(
            WindowSpec(
                "h1",
                candles[550].open_time_ms,
                candles[610].open_time_ms,
            ),
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
        FundingEvent(
            timestamp_ms=candles[430].open_time_ms + 30_000,
            rate=-0.00005,
            mark_price=candles[430].close,
        ),
    )
    return StudyContext(spec=spec, candles=tuple(candles), funding=funding)


def _strategy(
    *,
    period: int = 14,
    entry: float = 45.0,
    adx_min: float = 0.0,
    adx_max: float = 100.0,
    exit_mode: str = "tp",
) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=adx_min,
        adx_max=adx_max,
        exit_mode=exit_mode,
        rsi_exit=65.0 if exit_mode == "rsi" else None,
        tp_price_pct=1.0 if exit_mode == "tp" else None,
    )


def _search_payload() -> dict:
    return {
        "schema_version": 1,
        "name": "crossing index search",
        "exit_modes": ["tp", "rsi"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14, 15],
            "rsi_entry": {"start": 42.0, "stop": 46.0, "step": 4.0},
            "adx_min": {"start": 0.0, "stop": 10.0, "step": 10.0},
            "adx_max": {"start": 90.0, "stop": 100.0, "step": 10.0},
            "tp_price_pct": {"start": 0.8, "stop": 1.2, "step": 0.4},
            "rsi_exit": {"start": 62.0, "stop": 68.0, "step": 6.0},
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
            "max_candidates": 1000,
        },
    }


def test_crossing_index_signals_equal_prepared_for_strict_thresholds_and_gap():
    context = _context()
    prepared = prepare_discovery(context, rsi_periods=(14, 15))
    indexed = build_crossing_index(prepared)

    candidates = (
        _strategy(period=14, entry=42.0, adx_min=0.0, adx_max=100.0),
        _strategy(period=14, entry=46.0, adx_min=10.0, adx_max=90.0),
        _strategy(period=15, entry=44.0, exit_mode="rsi"),
    )
    for strategy in candidates:
        prepared_points = prepared.windows[0].points(strategy.rsi_period)
        reference = prepared_module._signals_for_strategy(
            prepared.windows[0],
            strategy,
            prepared_points,
        )
        actual = indexed_signals_for_strategy(indexed.windows[0], strategy)
        assert actual == reference

    counts = indexed_event_counts(indexed)
    assert counts[14] > 0
    assert counts[15] > 0
    assert counts[14] < len(prepared.windows[0].candles)
    assert counts[15] < len(prepared.windows[0].candles)


def test_crossing_index_candidate_matches_prepared_for_tp_rsi_funding_and_gap():
    context = _context()
    prepared = prepare_discovery(context, rsi_periods=(14, 15))
    indexed = build_crossing_index(prepared)

    for strategy in (
        _strategy(period=14, exit_mode="tp"),
        _strategy(period=15, exit_mode="rsi"),
    ):
        reference = _evaluate_prepared_candidate(
            context,
            prepared,
            strategy,
            stage="coarse",
        )
        actual = _evaluate_indexed_candidate(
            context,
            indexed,
            strategy,
            stage="coarse",
        )
        assert actual == reference
        assert evaluate_indexed_discovery(context, indexed, strategy) == (
            evaluate_prepared_discovery(context, prepared, strategy)
        )


def test_crossing_index_full_search_matches_prepared_frontier(tmp_path: Path, monkeypatch):
    context = _context()
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload()), encoding="utf-8")

    monkeypatch.setattr(
        prepared_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        indexed_search_module,
        "load_study_context",
        lambda *args, **kwargs: context,
    )

    reference = run_prepared_search("study.json", search_path, data_directory=tmp_path)
    actual = run_indexed_search("study.json", search_path, data_directory=tmp_path)

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

    assert actual["search_engine"] == "crossing_index_exact_v1"
    assert actual["reference_engine"] == "prepared_exact_v1"
    assert actual["runtime_parity_passed"] is True
    assert actual["truth_runtime_parity_checked_candidates"] == 3
    assert actual["prepared_runtime_parity_checked_candidates"] >= 4
    assert actual["validation_accessed"] is False
    assert actual["holdout_accessed"] is False
    assert actual["frontier"]


def test_stratified_runtime_sample_covers_rsi_period_and_exit_mode_pairs():
    candidates = [
        _strategy(period=14, exit_mode="tp"),
        _strategy(period=14, entry=46.0, exit_mode="tp"),
        _strategy(period=14, exit_mode="rsi"),
        _strategy(period=15, exit_mode="tp"),
        _strategy(period=15, exit_mode="rsi"),
    ]
    sample = _stratified_parity_sample(candidates)
    pairs = {(row.rsi_period, row.exit_mode) for row in sample}
    assert pairs == {
        (14, "tp"),
        (14, "rsi"),
        (15, "tp"),
        (15, "rsi"),
    }
