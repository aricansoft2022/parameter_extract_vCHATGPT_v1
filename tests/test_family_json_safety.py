import json
import math

from parameter_extract.families import ParameterScales, _parameter_distance
from parameter_extract.models import StrategySpec


def test_cross_exit_parameter_distance_is_large_finite_and_strict_json_safe():
    scales = ParameterScales(
        rsi_period=1.0,
        rsi_entry=1.0,
        adx_min=2.0,
        adx_max=2.0,
        tp_price_pct=0.25,
        rsi_exit=2.0,
    )
    tp = StrategySpec(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=30.0,
        adx_min=10.0,
        adx_max=30.0,
        exit_mode="tp",
        tp_price_pct=1.0,
    )
    rsi = StrategySpec(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=30.0,
        adx_min=10.0,
        adx_max=30.0,
        exit_mode="rsi",
        rsi_exit=70.0,
    )
    distance = _parameter_distance(tp, rsi, scales)
    assert math.isfinite(distance)
    assert distance > 1_000_000.0
    json.dumps({"parameter_distance": distance}, allow_nan=False)
