from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import ADR_PERIOD, ADX_PERIOD, StrategySpec


@dataclass(frozen=True, slots=True)
class IndicatorPoint:
    rsi: float
    adx: float
    adr: float


def entry_conditions(
    previous: IndicatorPoint, current: IndicatorPoint, strategy: StrategySpec
) -> dict[str, bool]:
    """Return the four strict ccbot entry legs independently."""
    return {
        "rsi_previous_strictly_below": previous.rsi < strategy.rsi_entry,
        "rsi_current_strictly_above": current.rsi > strategy.rsi_entry,
        "adx_strictly_inside_band": strategy.adx_min < current.adx < strategy.adx_max,
        "adr_strictly_increasing": current.adr > previous.adr,
    }


def entry_signal(previous: IndicatorPoint, current: IndicatorPoint, strategy: StrategySpec) -> bool:
    return all(entry_conditions(previous, current, strategy).values())


def rsi_exit_reached(rsi: float | None, threshold: float) -> bool:
    return rsi is not None and rsi > threshold


def wilder_rsi(closes: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result

    gain_total = 0.0
    loss_total = 0.0
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gain_total += max(change, 0.0)
        loss_total += max(-change, 0.0)
    average_gain = gain_total / period
    average_loss = loss_total / period
    result[period] = _rsi_from_averages(average_gain, average_loss)

    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        average_gain = ((average_gain * (period - 1)) + max(change, 0.0)) / period
        average_loss = ((average_loss * (period - 1)) + max(-change, 0.0)) / period
        result[index] = _rsi_from_averages(average_gain, average_loss)
    return result


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_loss == 0.0:
        return 100.0
    if average_gain == 0.0:
        return 0.0
    return 100.0 - (100.0 / (1.0 + (average_gain / average_loss)))


def wilder_adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = ADX_PERIOD,
) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    size = len(closes)
    result: list[float | None] = [None] * size
    if not (len(highs) == len(lows) == size) or size < period * 2:
        return result

    true_range = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for i in range(1, size):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if up_move > down_move and up_move > 0.0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0.0 else 0.0
        true_range[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    tr_smooth = sum(true_range[1 : period + 1])
    plus_smooth = sum(plus_dm[1 : period + 1])
    minus_smooth = sum(minus_dm[1 : period + 1])

    dx: list[float | None] = [None] * size
    for i in range(period, size):
        if i > period:
            tr_smooth = tr_smooth - (tr_smooth / period) + true_range[i]
            plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[i]
            minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[i]
        if tr_smooth == 0.0:
            dx[i] = 0.0
            continue
        plus_di = 100.0 * plus_smooth / tr_smooth
        minus_di = 100.0 * minus_smooth / tr_smooth
        spread = plus_di + minus_di
        dx[i] = 0.0 if spread == 0.0 else 100.0 * abs(plus_di - minus_di) / spread

    first_index = period * 2 - 1
    seed = [value for value in dx[period : first_index + 1] if value is not None]
    if len(seed) != period:
        return result
    adx = sum(seed) / period
    result[first_index] = adx
    for i in range(first_index + 1, size):
        current = dx[i]
        if current is None:
            continue
        adx = ((adx * (period - 1)) + current) / period
        result[i] = adx
    return result


def rolling_adr(
    highs: Sequence[float], lows: Sequence[float], period: int = ADR_PERIOD
) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(highs) != len(lows):
        raise ValueError("high and low series lengths differ")
    result: list[float | None] = [None] * len(highs)
    window = 0.0
    for i, (high, low) in enumerate(zip(highs, lows, strict=True)):
        window += high - low
        if i >= period:
            window -= highs[i - period] - lows[i - period]
        if i >= period - 1:
            result[i] = window / period
    return result


def indicator_points(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], rsi_period: int
) -> list[IndicatorPoint | None]:
    rsi_values = wilder_rsi(closes, rsi_period)
    adx_values = wilder_adx(highs, lows, closes, ADX_PERIOD)
    adr_values = rolling_adr(highs, lows, ADR_PERIOD)
    result: list[IndicatorPoint | None] = []
    for rsi, adx, adr in zip(rsi_values, adx_values, adr_values, strict=True):
        if rsi is None or adx is None or adr is None:
            result.append(None)
        else:
            result.append(IndicatorPoint(rsi=rsi, adx=adx, adr=adr))
    return result
