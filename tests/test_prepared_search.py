import json
import math
from dataclasses import asdict
from pathlib import Path

import parameter_extract.prepared as prepared_module
import parameter_extract.prepared_search as prepared_search_module
import parameter_extract.search as search_module
from parameter_extract.models import Candle, ExecutionModel, FundingEvent, StrategySpec
from parameter_extract.prepared import evaluate_prepared_discovery, prepare_discovery
from parameter_extract.prepared_search import (
    _evaluate_prepared_candidate,
    run_prepared_search,
)
from parameter_extract.search import _evaluate_candidate, run_search
from parameter_extract.signals import build_indicator_points, generate_signals
from parameter_extract.study import StudyContext, StudySpec, WindowSpec


def _context(*, with_gap: bool = True) -> StudyContext:
    base = 1_700_000_000_000
    candles: list[Candle] = []
    gap_offset = 0
    for index in range(620):
        if with_gap and index == 355:
            gap_offset += 60_000
        open_time = base + index * 60_000 + gap_offset
        close = (
            100.0
            + 5.4 * math.sin(index / 5.1)
            + 1.7 * math.sin(index / 13.7)
            + 0.006 * index
        )
        open_value = (
            100.0
            + 5.4 * math.sin((index - 0.55) / 5.1)
            + 1.7 * math.sin((index - 0.55) / 13.7)
            + 0.006 * (index - 0.55)
        )
        spread = 0.55 + 0.08 * (1.0 + math.sin(index / 9.0))
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

    discovery = WindowSpec(
        "d1",
        candles[300].open_time_ms,
        candles[450].open_time_ms,
    )
    validation = WindowSpec(
        "v1",
        candles[450].open_time_ms,
        candles[550].open_time_ms,
    )
    holdout = WindowSpec(
        "h1",
        candles[550].open_time_ms,
        candles[610].open_time_ms,
    )
    spec = StudySpec(
        name="prepared parity",
        symbol="BTCUSDT",
        dataset_manifest="unused-in-memory.json",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(
            taker_fee_bps=4.0,
            buy_slippage_bps=2.0,
            sell_slippage_bps=2.0,
        ),
        discovery=(discovery,),
        validation=(validation,),
        holdout=(holdout,),
        warmup_candles=300,
        min_trades=1,
    )
    funding = (
        FundingEvent(
            timestamp_ms=candles[372].open_time_ms + 30_000,
            rate=0.0001,
            mark_price=candles[372].close,
        ),
        FundingEvent(
            timestamp_ms=candles[420].open_time_ms + 30_000,
            rate=-0.00005,
            mark_price=candles[420].close,
        ),
    )
    return StudyContext(spec=spec, candles=tuple(candles), funding=funding)


def _tp_strategy(period: int = 14, entry: float = 45.0) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=0.0,
        adx_max=100.0,
        exit_mode="tp",
        tp_price_pct=1.0,
    )


def _rsi_strategy(period: int = 15, entry: float = 45.0) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=period,
        rsi_entry=entry,
        adx_min=0.0,
        adx_max=100.0,
        exit_mode="rsi",
        rsi_exit=65.0,
    )


def _search_payload(*, refinement: bool = True) -> dict:
    return {
        "schema_version": 1,
        "name": "prepared parity search",
        "exit_modes": ["tp", "rsi"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14, 15],
            "rsi_entry": {"start": 42.0, "stop": 46.0, "step": 4.0},
            "adx_min": {"start": 0.0, "stop": 0.0, "step": 1.0},
            "adx_max": {"start": 100.0, "stop": 100.0, "step": 1.0},
            "tp_price_pct": {"start": 0.8, "stop": 1.2, "step": 0.4},
            "rsi_exit": {"start": 62.0, "stop": 68.0, "step": 6.0},
        },
        "gates": {
            "min_total_trades": 1,
            "min_positive_window_fraction": 0.0,
        },
        "refinement": {
            "enabled": refinement,
            "step_divisor": 2,
            "radius_steps": 1,
            "max_seeds": 2,
            "max_candidates": 500,
        },
    }


def test_prepared_points_and_signal_times_match_truth_across_gap():
    context = _context(with_gap=True)
    prepared = prepare_discovery(context, rsi_periods=(14, 15))
    window = prepared.windows[0]

    for strategy in (_tp_strategy(14), _rsi_strategy(15)):
        truth_points = tuple(build_indicator_points(window.candles, strategy))
        prepared_points = window.points(strategy.rsi_period)
        assert prepared_points == truth_points

        truth_signals, _ = generate_signals(window.candles, strategy)
        truth_filtered = [
            signal
            for signal in truth_signals
            if window.start_ms <= signal.timestamp_ms < window.end_ms
        ]
        prepared_signals = prepared_module._signals_for_strategy(
            window,
            strategy,
            prepared_points,
        )
        assert [signal.timestamp_ms for signal in prepared_signals] == [
            signal.timestamp_ms for signal in truth_filtered
        ]
        assert [signal.candle_index for signal in prepared_signals] == [
            signal.candle_index for signal in truth_filtered
        ]


def test_prepared_candidate_matches_truth_for_tp_and_rsi_with_funding_and_gap():
    context = _context(with_gap=True)
    prepared = prepare_discovery(context, rsi_periods=(14, 15))

    for strategy in (_tp_strategy(), _rsi_strategy()):
        truth = _evaluate_candidate(context, strategy, stage="coarse")
        fast = _evaluate_prepared_candidate(
            context,
            prepared,
            strategy,
            stage="coarse",
        )
        assert fast == truth

        compact = evaluate_prepared_discovery(context, prepared, strategy)
        assert compact == truth["windows"]


def test_prepared_search_matches_slow_search_frontier_and_counts(tmp_path: Path, monkeypatch):
    context = _context(with_gap=True)
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload(refinement=True)), encoding="utf-8")

    monkeypatch.setattr(search_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(
        prepared_search_module,
        "load_study_context",
        lambda *a, **k: context,
    )

    slow = run_search("study.json", search_path, data_directory=tmp_path)
    fast = run_prepared_search("study.json", search_path, data_directory=tmp_path)

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
        assert fast[key] == slow[key]

    assert fast["search_engine"] == "prepared_exact_v1"
    assert fast["reference_engine"] == "truth_replay"
    assert fast["indicator_cache_rsi_periods"] == [14, 15]
    assert fast["prepared_discovery_window_count"] == 1
    assert fast["validation_accessed"] is False
    assert fast["holdout_accessed"] is False
    assert fast["frontier"]


def test_prepared_cache_is_data_invariant_across_strategy_thresholds():
    context = _context(with_gap=False)
    prepared = prepare_discovery(context, rsi_periods=(14,))
    window = prepared.windows[0]
    before = {
        "adx": window.adx,
        "adr": window.adr,
        "rsi": window.rsi_by_period[14],
    }

    for entry in (35.0, 45.0, 55.0):
        evaluate_prepared_discovery(context, prepared, _tp_strategy(14, entry))

    after = {
        "adx": window.adx,
        "adr": window.adr,
        "rsi": window.rsi_by_period[14],
    }
    assert after == before
    assert asdict(context.spec.execution)["entry_timing"] == "next_open"
