from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from .indicators import IndicatorPoint
from .metrics import summarize
from .models import StrategySpec
from .prepared import PreparedDiscovery, PreparedWindow
from .replay import replay_signals
from .signals import Signal
from .study import StudyContext


@dataclass(frozen=True, slots=True)
class CrossingEvent:
    candle_index: int
    timestamp_ms: int
    reference_price: float
    previous: IndicatorPoint
    current: IndicatorPoint


@dataclass(frozen=True, slots=True)
class IndexedWindow:
    prepared: PreparedWindow
    points_by_period: dict[int, tuple[IndicatorPoint | None, ...]]
    events_by_period: dict[int, tuple[CrossingEvent, ...]]


@dataclass(frozen=True, slots=True)
class IndexedDiscovery:
    prepared: PreparedDiscovery
    windows: tuple[IndexedWindow, ...]
    validation_accessed: bool = False
    holdout_accessed: bool = False


def build_crossing_index(prepared: PreparedDiscovery) -> IndexedDiscovery:
    windows: list[IndexedWindow] = []
    for window in prepared.windows:
        points_by_period: dict[int, tuple[IndicatorPoint | None, ...]] = {}
        events_by_period: dict[int, tuple[CrossingEvent, ...]] = {}
        for period in prepared.rsi_periods:
            points = window.points(period)
            points_by_period[period] = points
            events: list[CrossingEvent] = []
            for index in range(1, len(window.candles)):
                if index in window.segment_starts:
                    continue
                previous = points[index - 1]
                current = points[index]
                if previous is None or current is None:
                    continue
                # Candidate-independent necessary conditions for every strict entry.
                if current.rsi <= previous.rsi:
                    continue
                if current.adr <= previous.adr:
                    continue
                candle = window.candles[index]
                if not (window.start_ms <= candle.close_time_ms < window.end_ms):
                    continue
                events.append(
                    CrossingEvent(
                        candle_index=index,
                        timestamp_ms=candle.close_time_ms,
                        reference_price=candle.close,
                        previous=previous,
                        current=current,
                    )
                )
            events_by_period[period] = tuple(events)
        windows.append(
            IndexedWindow(
                prepared=window,
                points_by_period=points_by_period,
                events_by_period=events_by_period,
            )
        )
    return IndexedDiscovery(prepared=prepared, windows=tuple(windows))


def indexed_signals_for_strategy(
    window: IndexedWindow,
    strategy: StrategySpec,
) -> tuple[Signal, ...]:
    events = window.events_by_period.get(strategy.rsi_period)
    if events is None:
        raise ValueError("strategy RSI period was not indexed")
    signals: list[Signal] = []
    for event in events:
        if not (event.previous.rsi < strategy.rsi_entry < event.current.rsi):
            continue
        if not (strategy.adx_min < event.current.adx < strategy.adx_max):
            continue
        signals.append(
            Signal(
                candle_index=event.candle_index,
                timestamp_ms=event.timestamp_ms,
                reference_price=event.reference_price,
                previous=event.previous,
                current=event.current,
            )
        )
    return tuple(signals)


def evaluate_indexed_discovery(
    context: StudyContext,
    indexed: IndexedDiscovery,
    strategy: StrategySpec,
) -> list[dict[str, object]]:
    prepared = indexed.prepared
    if strategy.symbol != context.spec.symbol or strategy.symbol != prepared.symbol:
        raise ValueError("strategy symbol does not match indexed discovery")
    if prepared.study_name != context.spec.name:
        raise ValueError("indexed discovery belongs to a different study")
    if prepared.dataset_fingerprint_sha256 != context.spec.dataset_fingerprint_sha256:
        raise ValueError("indexed discovery belongs to a different dataset")
    if indexed.validation_accessed or indexed.holdout_accessed:
        raise RuntimeError("indexed discovery phase isolation failed")
    if strategy.rsi_period not in prepared.rsi_periods:
        raise ValueError("strategy RSI period was not indexed")

    rows: list[dict[str, object]] = []
    for window in indexed.windows:
        points = window.points_by_period[strategy.rsi_period]
        signals = indexed_signals_for_strategy(window, strategy)
        replay = replay_signals(
            window.prepared.candles,
            strategy,
            signals,
            points,
            execution=context.spec.execution,
            funding=window.prepared.funding,
        )
        normalized = replace(
            replay,
            dataset_start_ms=window.prepared.start_ms,
            dataset_end_ms=window.prepared.end_ms - 1,
        )
        metrics = summarize(normalized, min_trades=context.spec.min_trades).as_dict()
        rows.append(
            {
                "name": window.prepared.name,
                "return_pct": metrics["total_return_pct"],
                "trade_count": metrics["trade_count"],
                "worst_mae_pct": metrics["worst_mae_pct"],
                "drawdown_pct": metrics["max_closed_equity_drawdown_pct"],
                "max_holding_minutes": metrics["max_holding_minutes"],
                "open_at_end": metrics["open_at_end"],
            }
        )
    return rows


def indexed_event_counts(indexed: IndexedDiscovery) -> dict[int, int]:
    totals = {period: 0 for period in indexed.prepared.rsi_periods}
    for window in indexed.windows:
        for period, events in window.events_by_period.items():
            totals[period] += len(events)
    return totals


def indexed_candidate_scan_upper_bound(
    indexed: IndexedDiscovery,
    strategies: Sequence[StrategySpec],
) -> int:
    counts = indexed_event_counts(indexed)
    return sum(counts[strategy.rsi_period] for strategy in strategies)
