from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .indicators import IndicatorPoint, rsi_exit_reached
from .models import Candle, ExecutionModel, FundingEvent, StrategySpec
from .signals import ONE_MINUTE_MS, Signal, generate_signals


@dataclass(frozen=True, slots=True)
class Trade:
    signal_time_ms: int
    entry_time_ms: int
    entry_price: float
    exit_signal_time_ms: int
    exit_time_ms: int
    exit_price: float
    exit_reason: str
    gross_return_pct: float
    fee_return_pct: float
    funding_return_pct: float
    net_return_pct: float
    mae_pct: float
    mfe_pct: float
    holding_minutes: float


@dataclass(frozen=True, slots=True)
class OpenPosition:
    signal_time_ms: int
    entry_time_ms: int
    entry_price: float
    last_price: float
    unrealized_gross_return_pct: float
    funding_return_pct: float
    mae_pct: float
    mfe_pct: float
    holding_minutes: float


@dataclass(frozen=True, slots=True)
class ReplayResult:
    trades: tuple[Trade, ...]
    raw_signal_count: int
    accepted_signal_count: int
    skipped_while_open: int
    skipped_pending_entry: int
    cancelled_on_gap: int
    open_position: OpenPosition | None
    execution_model: str
    dataset_start_ms: int | None
    dataset_end_ms: int | None


@dataclass(slots=True)
class _Position:
    signal: Signal
    entry_time_ms: int
    entry_price: float
    entry_candle_index: int
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    funding_return_pct: float = 0.0


@dataclass(slots=True)
class _PendingEntry:
    signal: Signal


def run_strategy(
    candles: Sequence[Candle],
    strategy: StrategySpec,
    *,
    execution: ExecutionModel | None = None,
    funding: Sequence[FundingEvent] = (),
) -> ReplayResult:
    signals, points = generate_signals(candles, strategy)
    return replay_signals(
        candles,
        strategy,
        signals,
        points,
        execution=execution or ExecutionModel.expected_live(),
        funding=funding,
    )


def replay_signals(
    candles: Sequence[Candle],
    strategy: StrategySpec,
    signals: Sequence[Signal],
    points: Sequence[IndicatorPoint | None],
    *,
    execution: ExecutionModel,
    funding: Sequence[FundingEvent] = (),
) -> ReplayResult:
    """Replay one team's raw signals through a deterministic one-position execution model.

    This is intentionally the slow, obvious truth path. Future search accelerators must
    reproduce this result for the same candidate before they are trusted.
    """
    if len(points) != len(candles):
        raise ValueError("points length must match candles length")
    if not candles:
        return ReplayResult(
            trades=(), raw_signal_count=len(signals), accepted_signal_count=0,
            skipped_while_open=0, skipped_pending_entry=0, cancelled_on_gap=0,
            open_position=None, execution_model=execution.name,
            dataset_start_ms=None, dataset_end_ms=None,
        )

    signal_by_index = {signal.candle_index: signal for signal in signals}
    if len(signal_by_index) != len(signals):
        raise ValueError("at most one signal per candle is supported for one strategy")
    ordered_funding = sorted(funding, key=lambda event: event.timestamp_ms)

    trades: list[Trade] = []
    position: _Position | None = None
    pending: _PendingEntry | None = None
    accepted = 0
    skipped_open = 0
    skipped_pending = 0
    cancelled_gap = 0

    for index, candle in enumerate(candles):
        gap_before = (
            index > 0
            and candle.open_time_ms != candles[index - 1].open_time_ms + ONE_MINUTE_MS
        )
        if gap_before and pending is not None:
            pending = None
            cancelled_gap += 1

        if pending is not None and position is None:
            expected_index = pending.signal.candle_index + 1
            if index == expected_index and not gap_before:
                position = _open_position(pending.signal, candle, index, execution)
                pending = None
                accepted += 1

        if position is not None:
            _update_excursions(position, candle, strategy)
            exit_payload = _exit_for_candle(position, candle, points[index], strategy, execution)
            _apply_funding_for_candle(
                position,
                ordered_funding,
                candle,
                conservative_tp_exit=(exit_payload is not None and strategy.exit_mode == "tp"),
            )
            if exit_payload is not None:
                exit_price, reason = exit_payload
                trade = _close_trade(position, candle, exit_price, reason, execution)
                trades.append(trade)
                position = None

        signal = signal_by_index.get(index)
        if signal is None:
            continue
        if position is not None:
            skipped_open += 1
            continue
        if pending is not None:
            skipped_pending += 1
            continue
        if execution.entry_timing == "signal_close":
            position = _open_at_signal_close(signal, candle, index, execution)
            accepted += 1
        else:
            pending = _PendingEntry(signal=signal)

    if pending is not None:
        # The dataset ended before a next-open fill could exist. Do not invent one.
        pending = None

    open_position = None
    if position is not None:
        last = candles[-1]
        open_position = OpenPosition(
            signal_time_ms=position.signal.timestamp_ms,
            entry_time_ms=position.entry_time_ms,
            entry_price=position.entry_price,
            last_price=last.close,
            unrealized_gross_return_pct=_pct(last.close / position.entry_price - 1.0),
            funding_return_pct=position.funding_return_pct,
            mae_pct=position.mae_pct,
            mfe_pct=position.mfe_pct,
            holding_minutes=max(0.0, (last.close_time_ms - position.entry_time_ms) / 60_000.0),
        )

    return ReplayResult(
        trades=tuple(trades),
        raw_signal_count=len(signals),
        accepted_signal_count=accepted,
        skipped_while_open=skipped_open,
        skipped_pending_entry=skipped_pending,
        cancelled_on_gap=cancelled_gap,
        open_position=open_position,
        execution_model=execution.name,
        dataset_start_ms=candles[0].open_time_ms,
        dataset_end_ms=candles[-1].close_time_ms,
    )


def _open_position(
    signal: Signal, candle: Candle, candle_index: int, execution: ExecutionModel
) -> _Position:
    entry_price = candle.open * (1.0 + execution.buy_slippage_bps / 10_000.0)
    return _Position(
        signal=signal,
        entry_time_ms=candle.open_time_ms,
        entry_price=entry_price,
        entry_candle_index=candle_index,
    )


def _open_at_signal_close(
    signal: Signal, candle: Candle, candle_index: int, execution: ExecutionModel
) -> _Position:
    entry_price = candle.close * (1.0 + execution.buy_slippage_bps / 10_000.0)
    return _Position(
        signal=signal,
        entry_time_ms=candle.close_time_ms,
        entry_price=entry_price,
        entry_candle_index=candle_index,
    )


def _update_excursions(position: _Position, candle: Candle, strategy: StrategySpec) -> None:
    if candle.close_time_ms <= position.entry_time_ms:
        return
    low_return = _pct(candle.low / position.entry_price - 1.0)
    high = candle.high
    if strategy.exit_mode == "tp" and strategy.tp_price_pct is not None:
        target = position.entry_price * (1.0 + strategy.tp_price_pct / 100.0)
        high = min(high, target)
    high_return = _pct(high / position.entry_price - 1.0)
    position.mae_pct = min(position.mae_pct, low_return)
    position.mfe_pct = max(position.mfe_pct, high_return)


def _exit_for_candle(
    position: _Position,
    candle: Candle,
    point: IndicatorPoint | None,
    strategy: StrategySpec,
    execution: ExecutionModel,
) -> tuple[float, str] | None:
    if candle.close_time_ms <= position.entry_time_ms:
        return None
    if strategy.exit_mode == "tp":
        assert strategy.tp_price_pct is not None
        target = position.entry_price * (1.0 + strategy.tp_price_pct / 100.0)
        if candle.high >= target:
            fill = target * (1.0 - execution.sell_slippage_bps / 10_000.0)
            return fill, "TP_PRICE_REACHED"
        return None

    assert strategy.rsi_exit is not None
    rsi = None if point is None else point.rsi
    if rsi_exit_reached(rsi, strategy.rsi_exit):
        fill = candle.close * (1.0 - execution.sell_slippage_bps / 10_000.0)
        return fill, "RSI_EXIT_REACHED"
    return None


def _close_trade(
    position: _Position,
    candle: Candle,
    exit_price: float,
    reason: str,
    execution: ExecutionModel,
) -> Trade:
    gross = _pct(exit_price / position.entry_price - 1.0)
    fee_rate = execution.taker_fee_bps / 10_000.0
    fee = _pct(-fee_rate * (1.0 + exit_price / position.entry_price))
    net = gross + fee + position.funding_return_pct
    return Trade(
        signal_time_ms=position.signal.timestamp_ms,
        entry_time_ms=position.entry_time_ms,
        entry_price=position.entry_price,
        exit_signal_time_ms=candle.close_time_ms,
        exit_time_ms=candle.close_time_ms,
        exit_price=exit_price,
        exit_reason=reason,
        gross_return_pct=gross,
        fee_return_pct=fee,
        funding_return_pct=position.funding_return_pct,
        net_return_pct=net,
        mae_pct=position.mae_pct,
        mfe_pct=position.mfe_pct,
        holding_minutes=max(0.0, (candle.close_time_ms - position.entry_time_ms) / 60_000.0),
    )


def _apply_funding_for_candle(
    position: _Position,
    funding: Sequence[FundingEvent],
    candle: Candle,
    *,
    conservative_tp_exit: bool,
) -> None:
    """Apply funding events that occurred while the position was definitely open.

    For an RSI exit the position is known to survive until candle close. For a TP exit
    OHLC does not reveal whether the target or a same-minute funding timestamp happened
    first. In that ambiguous TP candle we charge positive funding (a cost) but do not
    grant negative funding (a benefit). That deliberately biases the truth engine away
    from optimistic performance.
    """
    for event in funding:
        if not (candle.open_time_ms <= event.timestamp_ms <= candle.close_time_ms):
            continue
        if not position.entry_time_ms < event.timestamp_ms:
            continue
        if conservative_tp_exit and event.rate < 0.0:
            continue
        mark = event.mark_price if event.mark_price is not None else candle.close
        position.funding_return_pct += _pct(-(mark / position.entry_price) * event.rate)


def _pct(fraction: float) -> float:
    return fraction * 100.0
