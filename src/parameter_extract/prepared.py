from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from .indicators import IndicatorPoint, rolling_adr, wilder_adx, wilder_rsi
from .metrics import summarize
from .models import Candle, FundingEvent, StrategySpec
from .replay import replay_signals
from .signals import Signal, contiguous_segments
from .study import StudyContext, WindowSpec


@dataclass(frozen=True, slots=True)
class PreparedWindow:
    """Discovery-window inputs whose indicator state is reusable across candidates.

    The local candle slice is identical to the truth study window: warm-up candles are
    present for indicator state, but raw entry signals are admitted only inside the named
    discovery window. Recursive indicator state is reset at every real one-minute gap.
    """

    name: str
    start_ms: int
    end_ms: int
    warmup_candles: int
    candles: tuple[Candle, ...]
    funding: tuple[FundingEvent, ...]
    segment_starts: frozenset[int]
    adx: tuple[float | None, ...]
    adr: tuple[float | None, ...]
    rsi_by_period: dict[int, tuple[float | None, ...]]

    def points(self, period: int) -> tuple[IndicatorPoint | None, ...]:
        rsi = self.rsi_by_period.get(period)
        if rsi is None:
            raise ValueError(f"RSI period {period} was not prepared")
        points: list[IndicatorPoint | None] = []
        for rsi_value, adx_value, adr_value in zip(rsi, self.adx, self.adr, strict=True):
            if rsi_value is None or adx_value is None or adr_value is None:
                points.append(None)
            else:
                points.append(
                    IndicatorPoint(
                        rsi=rsi_value,
                        adx=adx_value,
                        adr=adr_value,
                    )
                )
        return tuple(points)


@dataclass(frozen=True, slots=True)
class PreparedDiscovery:
    study_name: str
    symbol: str
    dataset_fingerprint_sha256: str
    rsi_periods: tuple[int, ...]
    windows: tuple[PreparedWindow, ...]
    validation_accessed: bool = False
    holdout_accessed: bool = False


def prepare_discovery(
    context: StudyContext,
    *,
    rsi_periods: Sequence[int],
) -> PreparedDiscovery:
    periods = tuple(int(period) for period in rsi_periods)
    if not periods:
        raise ValueError("prepared discovery requires at least one RSI period")
    if len(periods) != len(set(periods)):
        raise ValueError("prepared RSI periods must be unique")
    if any(period not in {14, 15, 16, 17, 18, 19} for period in periods):
        raise ValueError("prepared RSI periods must be between 14 and 19")

    windows = tuple(
        _prepare_window(
            window,
            context.candles,
            context.funding,
            warmup_candles=context.spec.warmup_candles,
            rsi_periods=periods,
        )
        for window in context.spec.discovery
    )
    return PreparedDiscovery(
        study_name=context.spec.name,
        symbol=context.spec.symbol,
        dataset_fingerprint_sha256=context.spec.dataset_fingerprint_sha256,
        rsi_periods=periods,
        windows=windows,
    )


def evaluate_prepared_discovery(
    context: StudyContext,
    prepared: PreparedDiscovery,
    strategy: StrategySpec,
) -> list[dict[str, object]]:
    """Return the same compact discovery-window rows used by search._evaluate_candidate."""

    if strategy.symbol != context.spec.symbol or strategy.symbol != prepared.symbol:
        raise ValueError("strategy symbol does not match prepared discovery")
    if prepared.study_name != context.spec.name:
        raise ValueError("prepared discovery belongs to a different study")
    if prepared.dataset_fingerprint_sha256 != context.spec.dataset_fingerprint_sha256:
        raise ValueError("prepared discovery belongs to a different dataset")
    if prepared.validation_accessed or prepared.holdout_accessed:
        raise RuntimeError("prepared discovery phase isolation failed")
    if strategy.rsi_period not in prepared.rsi_periods:
        raise ValueError("strategy RSI period was not prepared")

    rows: list[dict[str, object]] = []
    for window in prepared.windows:
        points = window.points(strategy.rsi_period)
        signals = _signals_for_strategy(window, strategy, points)
        replay = replay_signals(
            window.candles,
            strategy,
            signals,
            points,
            execution=context.spec.execution,
            funding=window.funding,
        )
        normalized = replace(
            replay,
            dataset_start_ms=window.start_ms,
            dataset_end_ms=window.end_ms - 1,
        )
        metrics = summarize(normalized, min_trades=context.spec.min_trades).as_dict()
        rows.append(
            {
                "name": window.name,
                "return_pct": metrics["total_return_pct"],
                "trade_count": metrics["trade_count"],
                "worst_mae_pct": metrics["worst_mae_pct"],
                "drawdown_pct": metrics["max_closed_equity_drawdown_pct"],
                "max_holding_minutes": metrics["max_holding_minutes"],
                "open_at_end": metrics["open_at_end"],
            }
        )
    return rows


def _prepare_window(
    window: WindowSpec,
    candles: Sequence[Candle],
    funding: Sequence[FundingEvent],
    *,
    warmup_candles: int,
    rsi_periods: tuple[int, ...],
) -> PreparedWindow:
    local = _window_candles(window, candles, warmup_candles=warmup_candles)
    local_funding = tuple(
        event
        for event in funding
        if local[0].open_time_ms <= event.timestamp_ms < window.end_ms
    )

    size = len(local)
    adx: list[float | None] = [None] * size
    adr: list[float | None] = [None] * size
    rsi_by_period: dict[int, list[float | None]] = {
        period: [None] * size for period in rsi_periods
    }
    segments = contiguous_segments(local)
    for start, end in segments:
        segment = local[start:end]
        highs = [candle.high for candle in segment]
        lows = [candle.low for candle in segment]
        closes = [candle.close for candle in segment]
        adx[start:end] = wilder_adx(highs, lows, closes)
        adr[start:end] = rolling_adr(highs, lows)
        for period in rsi_periods:
            rsi_by_period[period][start:end] = wilder_rsi(closes, period)

    return PreparedWindow(
        name=window.name,
        start_ms=window.start_ms,
        end_ms=window.end_ms,
        warmup_candles=warmup_candles,
        candles=tuple(local),
        funding=local_funding,
        segment_starts=frozenset(start for start, _ in segments),
        adx=tuple(adx),
        adr=tuple(adr),
        rsi_by_period={period: tuple(values) for period, values in rsi_by_period.items()},
    )


def _signals_for_strategy(
    window: PreparedWindow,
    strategy: StrategySpec,
    points: Sequence[IndicatorPoint | None],
) -> tuple[Signal, ...]:
    signals: list[Signal] = []
    for index in range(1, len(window.candles)):
        if index in window.segment_starts:
            continue
        previous = points[index - 1]
        current = points[index]
        if previous is None or current is None:
            continue
        if not (
            previous.rsi < strategy.rsi_entry
            and current.rsi > strategy.rsi_entry
            and strategy.adx_min < current.adx < strategy.adx_max
            and current.adr > previous.adr
        ):
            continue
        candle = window.candles[index]
        if not (window.start_ms <= candle.close_time_ms < window.end_ms):
            continue
        signals.append(
            Signal(
                candle_index=index,
                timestamp_ms=candle.close_time_ms,
                reference_price=candle.close,
                previous=previous,
                current=current,
            )
        )
    return tuple(signals)


def _window_candles(
    window: WindowSpec,
    candles: Sequence[Candle],
    *,
    warmup_candles: int,
) -> tuple[Candle, ...]:
    start_index = next(
        (index for index, candle in enumerate(candles) if candle.open_time_ms >= window.start_ms),
        None,
    )
    if start_index is None:
        raise ValueError(f"window {window.name!r} starts after the dataset ends")
    if start_index < warmup_candles:
        raise ValueError(
            f"window {window.name!r} has only {start_index} pre-window candles; "
            f"{warmup_candles} required"
        )
    end_index = next(
        (
            index
            for index in range(start_index, len(candles))
            if candles[index].open_time_ms >= window.end_ms
        ),
        len(candles),
    )
    if end_index <= start_index:
        raise ValueError(f"window {window.name!r} contains no candles")
    if end_index == len(candles) and candles[-1].close_time_ms < window.end_ms - 1:
        raise ValueError(f"window {window.name!r} extends beyond the dataset")
    return tuple(candles[start_index - warmup_candles : end_index])
