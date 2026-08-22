from __future__ import annotations

from parameter_extract.models import Candle, StrategySpec
from parameter_extract.signals import build_indicator_points, contiguous_segments


def _candle(index: int) -> Candle:
    open_time = index * 60_000
    price = 100.0 + index
    return Candle(
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.25,
    )


def _strategy() -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT", rsi_period=14, rsi_entry=30, adx_min=10, adx_max=90,
        exit_mode="rsi", rsi_exit=70,
    )


def test_contiguous_segments_split_on_missing_minute():
    candles = [_candle(i) for i in range(5)]
    gap = Candle(
        open_time_ms=7 * 60_000,
        close_time_ms=7 * 60_000 + 59_999,
        open=105,
        high=106,
        low=104,
        close=105.5,
    )
    candles.append(gap)
    assert contiguous_segments(candles) == [(0, 5), (5, 6)]


def test_recursive_indicators_restart_after_gap():
    first = [_candle(i) for i in range(40)]
    second = []
    for j in range(40):
        idx = 45 + j
        price = 200.0 + j
        second.append(
            Candle(
                open_time_ms=idx * 60_000,
                close_time_ms=idx * 60_000 + 59_999,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + 0.25,
            )
        )
    points = build_indicator_points(first + second, _strategy())
    assert points[39] is not None
    assert points[40] is None
    assert points[-1] is not None
