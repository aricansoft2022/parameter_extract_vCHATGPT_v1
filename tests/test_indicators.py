from __future__ import annotations

import math

import pytest

from parameter_extract.indicators import (
    IndicatorPoint,
    entry_conditions,
    entry_signal,
    rolling_adr,
    rsi_exit_reached,
    wilder_adx,
    wilder_rsi,
)
from parameter_extract.models import StrategySpec


def _strategy(**overrides) -> StrategySpec:
    values = dict(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=30.0,
        adx_min=20.0,
        adx_max=40.0,
        exit_mode="rsi",
        rsi_exit=70.0,
    )
    values.update(overrides)
    return StrategySpec(**values)


def test_wilder_rsi_matches_live_service_seed_behavior():
    values = wilder_rsi([float(value) for value in range(1, 40)], 14)
    assert values[:14] == [None] * 14
    assert values[14] is not None
    assert math.isclose(values[-1], 100.0)


def test_wilder_rsi_monotonic_fall_is_zero():
    values = wilder_rsi([float(value) for value in range(40, 1, -1)], 14)
    assert math.isclose(values[-1], 0.0)


def test_adx_first_defined_value_is_index_27_for_period_14():
    highs = [float(10 + index) for index in range(40)]
    lows = [float(9 + index) for index in range(40)]
    closes = [float(9.5 + index) for index in range(40)]
    values = wilder_adx(highs, lows, closes, 14)
    assert values[26] is None
    assert values[27] is not None


def test_adr_is_simple_rolling_mean_of_range():
    values = rolling_adr([2.0] * 20, [1.0] * 20, 14)
    assert values[12] is None
    assert math.isclose(values[13], 1.0)


def test_entry_is_strict_on_all_four_boundaries():
    strategy = _strategy()
    clean_previous = IndicatorPoint(rsi=29.0, adx=30.0, adr=1.0)
    clean_current = IndicatorPoint(rsi=31.0, adx=30.0, adr=1.1)
    assert entry_signal(clean_previous, clean_current, strategy)

    assert not entry_signal(IndicatorPoint(30.0, 30.0, 1.0), clean_current, strategy)
    assert not entry_signal(clean_previous, IndicatorPoint(30.0, 30.0, 1.1), strategy)
    assert not entry_signal(clean_previous, IndicatorPoint(31.0, 20.0, 1.1), strategy)
    assert not entry_signal(clean_previous, IndicatorPoint(31.0, 40.0, 1.1), strategy)
    assert not entry_signal(clean_previous, IndicatorPoint(31.0, 30.0, 1.0), strategy)


def test_condition_diagnostics_are_independent():
    conditions = entry_conditions(
        IndicatorPoint(29.0, 30.0, 1.0),
        IndicatorPoint(31.0, 45.0, 1.1),
        _strategy(),
    )
    assert conditions == {
        "rsi_previous_strictly_below": True,
        "rsi_current_strictly_above": True,
        "adx_strictly_inside_band": False,
        "adr_strictly_increasing": True,
    }


def test_rsi_exit_is_strict():
    assert rsi_exit_reached(70.1, 70.0)
    assert not rsi_exit_reached(70.0, 70.0)
    assert not rsi_exit_reached(None, 70.0)


def test_invalid_period_is_rejected():
    with pytest.raises(ValueError):
        wilder_rsi([1.0, 2.0], 0)
