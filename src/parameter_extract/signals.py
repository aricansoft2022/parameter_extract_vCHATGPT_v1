from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .indicators import IndicatorPoint, entry_conditions, indicator_points
from .models import Candle, StrategySpec

ONE_MINUTE_MS = 60_000


@dataclass(frozen=True, slots=True)
class Signal:
    candle_index: int
    timestamp_ms: int
    reference_price: float
    previous: IndicatorPoint
    current: IndicatorPoint


def contiguous_segments(candles: Sequence[Candle]) -> list[tuple[int, int]]:
    """Return half-open index ranges whose open times are exactly one minute apart."""
    if not candles:
        return []
    segments: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(candles)):
        if candles[index].open_time_ms != candles[index - 1].open_time_ms + ONE_MINUTE_MS:
            segments.append((start, index))
            start = index
    segments.append((start, len(candles)))
    return segments


def build_indicator_points(
    candles: Sequence[Candle], strategy: StrategySpec
) -> list[IndicatorPoint | None]:
    """Compute indicators without ever carrying recursive state across a data gap."""
    points: list[IndicatorPoint | None] = [None] * len(candles)
    for start, end in contiguous_segments(candles):
        segment = candles[start:end]
        calculated = indicator_points(
            [c.high for c in segment],
            [c.low for c in segment],
            [c.close for c in segment],
            strategy.rsi_period,
        )
        points[start:end] = calculated
    return points


def generate_signals(
    candles: Sequence[Candle], strategy: StrategySpec
) -> tuple[list[Signal], list[IndicatorPoint | None]]:
    """Generate raw strategy signals; position occupancy is handled by replay, not here."""
    _validate_candle_order(candles)
    points = build_indicator_points(candles, strategy)
    signals: list[Signal] = []
    segment_starts = {start for start, _ in contiguous_segments(candles)}
    for index in range(1, len(candles)):
        if index in segment_starts:
            continue
        previous = points[index - 1]
        current = points[index]
        if previous is None or current is None:
            continue
        conditions = entry_conditions(previous, current, strategy)
        if all(conditions.values()):
            candle = candles[index]
            signals.append(
                Signal(
                    candle_index=index,
                    timestamp_ms=candle.close_time_ms,
                    reference_price=candle.close,
                    previous=previous,
                    current=current,
                )
            )
    return signals, points


def _validate_candle_order(candles: Sequence[Candle]) -> None:
    previous_open: int | None = None
    for candle in candles:
        if previous_open is not None and candle.open_time_ms <= previous_open:
            raise ValueError("candles must be strictly ordered by open_time_ms")
        previous_open = candle.open_time_ms
