from __future__ import annotations

import math

from parameter_extract.indicators import IndicatorPoint
from parameter_extract.models import Candle, ExecutionModel, FundingEvent, StrategySpec
from parameter_extract.replay import replay_signals
from parameter_extract.signals import Signal


def _candle(index: int, *, open_: float, high: float, low: float, close: float) -> Candle:
    start = index * 60_000
    return Candle(
        open_time_ms=start,
        close_time_ms=start + 59_999,
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def _tp_strategy(tp: float = 1.0) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT", rsi_period=14, rsi_entry=30, adx_min=20, adx_max=40,
        exit_mode="tp", tp_price_pct=tp,
    )


def _rsi_strategy() -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT", rsi_period=14, rsi_entry=30, adx_min=20, adx_max=40,
        exit_mode="rsi", rsi_exit=70,
    )


def _signal(index: int, candle: Candle) -> Signal:
    return Signal(
        candle_index=index,
        timestamp_ms=candle.close_time_ms,
        reference_price=candle.close,
        previous=IndicatorPoint(29.0, 30.0, 1.0),
        current=IndicatorPoint(31.0, 30.0, 1.1),
    )


def _execution(entry_timing: str = "next_open", fee: float = 0.0) -> ExecutionModel:
    return ExecutionModel(
        name="test", entry_timing=entry_timing, taker_fee_bps=fee,
        buy_slippage_bps=0, sell_slippage_bps=0,
    )


def test_next_open_separates_signal_from_entry_and_tp_uses_actual_entry_price():
    candles = [
        _candle(0, open_=99, high=100, low=98, close=99),
        _candle(1, open_=100, high=102, low=99, close=101.5),
    ]
    result = replay_signals(
        candles, _tp_strategy(1.0), [_signal(0, candles[0])], [None, None],
        execution=_execution(),
    )
    trade = result.trades[0]
    assert trade.signal_time_ms == candles[0].close_time_ms
    assert trade.entry_time_ms == candles[1].open_time_ms
    assert math.isclose(trade.entry_price, 100.0)
    assert math.isclose(trade.exit_price, 101.0)
    assert math.isclose(trade.gross_return_pct, 1.0)


def test_fees_and_funding_reduce_net_return():
    candles = [
        _candle(0, open_=100, high=100.5, low=99.5, close=100),
        _candle(1, open_=100, high=102, low=99, close=101.5),
    ]
    funding = [FundingEvent(timestamp_ms=90_000, rate=0.001, mark_price=100.5)]
    result = replay_signals(
        candles, _tp_strategy(1.0), [_signal(0, candles[0])], [None, None],
        execution=_execution(fee=4), funding=funding,
    )
    trade = result.trades[0]
    assert trade.fee_return_pct < 0
    assert trade.funding_return_pct < 0
    assert trade.net_return_pct < trade.gross_return_pct


def test_signal_while_position_is_open_is_skipped():
    candles = [
        _candle(0, open_=100, high=100.2, low=99.8, close=100),
        _candle(1, open_=100, high=100.3, low=99.7, close=100),
        _candle(2, open_=100, high=102, low=99.5, close=101.5),
    ]
    result = replay_signals(
        candles,
        _tp_strategy(1.0),
        [_signal(0, candles[0]), _signal(1, candles[1])],
        [None, None, None],
        execution=_execution(),
    )
    assert len(result.trades) == 1
    assert result.skipped_while_open == 1


def test_pending_next_open_is_cancelled_if_market_data_has_a_gap():
    first = _candle(0, open_=100, high=100.2, low=99.8, close=100)
    gap = Candle(
        open_time_ms=2 * 60_000,
        close_time_ms=2 * 60_000 + 59_999,
        open=100,
        high=102,
        low=99,
        close=101,
    )
    result = replay_signals(
        [first, gap], _tp_strategy(), [_signal(0, first)], [None, None], execution=_execution()
    )
    assert not result.trades
    assert result.open_position is None
    assert result.cancelled_on_gap == 1


def test_rsi_exit_uses_completed_candle_and_strict_threshold():
    candles = [
        _candle(0, open_=100, high=100.2, low=99.8, close=100),
        _candle(1, open_=100, high=100.5, low=99.5, close=100.2),
        _candle(2, open_=100.2, high=101, low=100, close=100.8),
        _candle(3, open_=100.8, high=101.5, low=100.5, close=101.2),
    ]
    points = [
        None,
        IndicatorPoint(60, 30, 1),
        IndicatorPoint(70.0, 30, 1),
        IndicatorPoint(70.1, 30, 1),
    ]
    result = replay_signals(
        candles, _rsi_strategy(), [_signal(0, candles[0])], points, execution=_execution()
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_time_ms == candles[3].close_time_ms
    assert result.trades[0].exit_reason == "RSI_EXIT_REACHED"


def test_open_at_end_is_censored_not_force_closed():
    candles = [
        _candle(0, open_=100, high=100.2, low=99.8, close=100),
        _candle(1, open_=100, high=100.5, low=99.5, close=100.2),
    ]
    result = replay_signals(
        candles, _tp_strategy(10), [_signal(0, candles[0])], [None, None], execution=_execution()
    )
    assert not result.trades
    assert result.open_position is not None


def test_funding_at_next_candle_open_counts_if_position_was_already_open():
    candles = [
        _candle(0, open_=100, high=100.2, low=99.8, close=100),
        _candle(1, open_=100, high=100.4, low=99.7, close=100.1),
        _candle(2, open_=100.1, high=102, low=100, close=101.5),
    ]
    funding = [FundingEvent(timestamp_ms=60_000, rate=0.001, mark_price=100.0)]
    result = replay_signals(
        candles, _tp_strategy(1.0), [_signal(0, candles[0])], [None, None, None],
        execution=_execution(entry_timing="signal_close"), funding=funding,
    )
    assert result.trades[0].funding_return_pct < 0


def test_negative_funding_is_not_credited_on_ambiguous_tp_exit_candle():
    candles = [
        _candle(0, open_=100, high=100.2, low=99.8, close=100),
        _candle(1, open_=100, high=102, low=99.5, close=101.5),
    ]
    funding = [FundingEvent(timestamp_ms=90_000, rate=-0.001, mark_price=100.5)]
    result = replay_signals(
        candles, _tp_strategy(1.0), [_signal(0, candles[0])], [None, None],
        execution=_execution(), funding=funding,
    )
    assert result.trades[0].funding_return_pct == 0.0
