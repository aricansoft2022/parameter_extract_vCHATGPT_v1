from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from math import inf
from typing import Sequence

from .crossing_index import IndexedDiscovery, IndexedWindow
from .entry_signal_cache import EntrySignalCache
from .metrics import summarize
from .models import Candle, ExecutionModel, FundingEvent, StrategySpec
from .replay import OpenPosition, ReplayResult, Trade
from .signals import ONE_MINUTE_MS, Signal
from .study import StudyContext


class _MaxTree:
    __slots__ = ("_n", "_size", "_tree")

    def __init__(self, values: Sequence[float]) -> None:
        self._n = len(values)
        size = 1
        while size < max(1, self._n):
            size *= 2
        self._size = size
        tree = [-inf] * (2 * size)
        tree[size : size + self._n] = values
        for node in range(size - 1, 0, -1):
            tree[node] = max(tree[node * 2], tree[node * 2 + 1])
        self._tree = tree

    def range_max(self, start: int, end: int) -> float:
        """Return max over inclusive [start, end]."""
        if start > end:
            return -inf
        left = start + self._size
        right = end + self._size + 1
        result = -inf
        while left < right:
            if left & 1:
                result = max(result, self._tree[left])
                left += 1
            if right & 1:
                right -= 1
                result = max(result, self._tree[right])
            left //= 2
            right //= 2
        return result

    def first_ge(self, start: int, threshold: float) -> int | None:
        return self._first(start, threshold, strict=False, node=1, left=0, right=self._size)

    def first_gt(self, start: int, threshold: float) -> int | None:
        return self._first(start, threshold, strict=True, node=1, left=0, right=self._size)

    def _first(
        self,
        start: int,
        threshold: float,
        *,
        strict: bool,
        node: int,
        left: int,
        right: int,
    ) -> int | None:
        if right <= start or left >= self._n:
            return None
        maximum = self._tree[node]
        if maximum <= threshold if strict else maximum < threshold:
            return None
        if right - left == 1:
            return left
        middle = (left + right) // 2
        found = self._first(
            start,
            threshold,
            strict=strict,
            node=node * 2,
            left=left,
            right=middle,
        )
        if found is not None:
            return found
        return self._first(
            start,
            threshold,
            strict=strict,
            node=node * 2 + 1,
            left=middle,
            right=right,
        )


class _MinTree:
    __slots__ = ("_size", "_tree")

    def __init__(self, values: Sequence[float]) -> None:
        size = 1
        while size < max(1, len(values)):
            size *= 2
        self._size = size
        tree = [inf] * (2 * size)
        tree[size : size + len(values)] = values
        for node in range(size - 1, 0, -1):
            tree[node] = min(tree[node * 2], tree[node * 2 + 1])
        self._tree = tree

    def range_min(self, start: int, end: int) -> float:
        if start > end:
            return inf
        left = start + self._size
        right = end + self._size + 1
        result = inf
        while left < right:
            if left & 1:
                result = min(result, self._tree[left])
                left += 1
            if right & 1:
                right -= 1
                result = min(result, self._tree[right])
            left //= 2
            right //= 2
        return result


@dataclass(frozen=True, slots=True)
class _FundingAtCandle:
    candle_index: int
    event: FundingEvent


@dataclass(frozen=True, slots=True)
class ExitQueryWindow:
    indexed: IndexedWindow
    high_tree: _MaxTree
    low_tree: _MinTree
    rsi_tree_by_period: dict[int, _MaxTree]
    funding: tuple[_FundingAtCandle, ...]


@dataclass(frozen=True, slots=True)
class ExitQueryDiscovery:
    indexed: IndexedDiscovery
    windows: tuple[ExitQueryWindow, ...]
    validation_accessed: bool = False
    holdout_accessed: bool = False


def build_exit_query_index(indexed: IndexedDiscovery) -> ExitQueryDiscovery:
    windows: list[ExitQueryWindow] = []
    for window in indexed.windows:
        candles = window.prepared.candles
        rsi_trees: dict[int, _MaxTree] = {}
        for period, points in window.points_by_period.items():
            rsi_trees[period] = _MaxTree(
                [(-inf if point is None else point.rsi) for point in points]
            )
        windows.append(
            ExitQueryWindow(
                indexed=window,
                high_tree=_MaxTree([candle.high for candle in candles]),
                low_tree=_MinTree([candle.low for candle in candles]),
                rsi_tree_by_period=rsi_trees,
                funding=_index_funding(candles, window.prepared.funding),
            )
        )
    return ExitQueryDiscovery(indexed=indexed, windows=tuple(windows))


def replay_signals_query(
    window: ExitQueryWindow,
    strategy: StrategySpec,
    signals: Sequence[Signal],
    *,
    execution: ExecutionModel,
) -> ReplayResult:
    """Exact jump replay using range/first-crossing queries instead of a candle loop.

    Signal acceptance order, next-open gap cancellation, same-exit-candle signal reuse,
    funding conservatism, excursions and censored open positions mirror ``replay_signals``.
    """
    candles = window.indexed.prepared.candles
    if not candles:
        return ReplayResult(
            trades=(),
            raw_signal_count=len(signals),
            accepted_signal_count=0,
            skipped_while_open=0,
            skipped_pending_entry=0,
            cancelled_on_gap=0,
            open_position=None,
            execution_model=execution.name,
            dataset_start_ms=None,
            dataset_end_ms=None,
        )

    ordered = sorted(signals, key=lambda signal: signal.candle_index)
    indices = [signal.candle_index for signal in ordered]
    if len(set(indices)) != len(indices):
        raise ValueError("at most one signal per candle is supported for one strategy")

    trades: list[Trade] = []
    accepted = 0
    skipped_open = 0
    cancelled_gap = 0
    open_position: OpenPosition | None = None
    cursor = 0

    while cursor < len(ordered):
        signal = ordered[cursor]
        signal_index = signal.candle_index
        if not 0 <= signal_index < len(candles):
            cursor += 1
            continue

        if execution.entry_timing == "next_open":
            entry_index = signal_index + 1
            if entry_index >= len(candles):
                break
            if (
                candles[entry_index].open_time_ms
                != candles[signal_index].open_time_ms + ONE_MINUTE_MS
            ):
                cancelled_gap += 1
                cursor += 1
                continue
            entry_time_ms = candles[entry_index].open_time_ms
            entry_price = candles[entry_index].open * (
                1.0 + execution.buy_slippage_bps / 10_000.0
            )
            excursion_start = entry_index
        else:
            entry_index = signal_index
            entry_time_ms = candles[entry_index].close_time_ms
            entry_price = candles[entry_index].close * (
                1.0 + execution.buy_slippage_bps / 10_000.0
            )
            excursion_start = entry_index + 1

        accepted += 1
        exit_index = _first_exit_index(
            window,
            strategy,
            entry_price=entry_price,
            start_index=excursion_start,
        )
        range_end = len(candles) - 1 if exit_index is None else exit_index
        mae_pct, mfe_pct = _excursions(
            window,
            strategy,
            entry_price=entry_price,
            start_index=excursion_start,
            end_index=range_end,
        )
        funding_return_pct = _funding_return(
            window,
            strategy,
            entry_time_ms=entry_time_ms,
            entry_price=entry_price,
            start_index=entry_index,
            end_index=range_end,
            exit_index=exit_index,
        )

        if exit_index is None:
            last = candles[-1]
            open_position = OpenPosition(
                signal_time_ms=signal.timestamp_ms,
                entry_time_ms=entry_time_ms,
                entry_price=entry_price,
                last_price=last.close,
                unrealized_gross_return_pct=_pct(last.close / entry_price - 1.0),
                funding_return_pct=funding_return_pct,
                mae_pct=mae_pct,
                mfe_pct=mfe_pct,
                holding_minutes=max(0.0, (last.close_time_ms - entry_time_ms) / 60_000.0),
            )
            skipped_open += len(ordered) - cursor - 1
            break

        exit_candle = candles[exit_index]
        exit_price, reason = _exit_price(
            exit_candle,
            strategy,
            execution,
            entry_price=entry_price,
        )
        gross = _pct(exit_price / entry_price - 1.0)
        fee_rate = execution.taker_fee_bps / 10_000.0
        fee = _pct(-fee_rate * (1.0 + exit_price / entry_price))
        trades.append(
            Trade(
                signal_time_ms=signal.timestamp_ms,
                entry_time_ms=entry_time_ms,
                entry_price=entry_price,
                exit_signal_time_ms=exit_candle.close_time_ms,
                exit_time_ms=exit_candle.close_time_ms,
                exit_price=exit_price,
                exit_reason=reason,
                gross_return_pct=gross,
                fee_return_pct=fee,
                funding_return_pct=funding_return_pct,
                net_return_pct=gross + fee + funding_return_pct,
                mae_pct=mae_pct,
                mfe_pct=mfe_pct,
                holding_minutes=max(
                    0.0,
                    (exit_candle.close_time_ms - entry_time_ms) / 60_000.0,
                ),
            )
        )

        next_cursor = bisect_left(indices, exit_index, lo=cursor + 1)
        skipped_open += next_cursor - cursor - 1
        cursor = next_cursor

    return ReplayResult(
        trades=tuple(trades),
        raw_signal_count=len(signals),
        accepted_signal_count=accepted,
        skipped_while_open=skipped_open,
        skipped_pending_entry=0,
        cancelled_on_gap=cancelled_gap,
        open_position=open_position,
        execution_model=execution.name,
        dataset_start_ms=candles[0].open_time_ms,
        dataset_end_ms=candles[-1].close_time_ms,
    )


def evaluate_query_discovery(
    context: StudyContext,
    signal_cache: EntrySignalCache,
    query: ExitQueryDiscovery,
    strategy: StrategySpec,
) -> list[dict[str, object]]:
    indexed = signal_cache.indexed
    if query.indexed is not indexed:
        raise ValueError("exit-query index and signal cache do not share the same indexed data")
    prepared = indexed.prepared
    if strategy.symbol != context.spec.symbol or strategy.symbol != prepared.symbol:
        raise ValueError("strategy symbol does not match query discovery")
    if prepared.study_name != context.spec.name:
        raise ValueError("query discovery belongs to a different study")
    if prepared.dataset_fingerprint_sha256 != context.spec.dataset_fingerprint_sha256:
        raise ValueError("query discovery belongs to a different dataset")
    if query.validation_accessed or query.holdout_accessed:
        raise RuntimeError("query discovery phase isolation failed")

    signal_windows = signal_cache.signals(strategy)
    rows: list[dict[str, object]] = []
    for window, signals in zip(query.windows, signal_windows, strict=True):
        replay = replay_signals_query(
            window,
            strategy,
            signals,
            execution=context.spec.execution,
        )
        normalized = replace(
            replay,
            dataset_start_ms=window.indexed.prepared.start_ms,
            dataset_end_ms=window.indexed.prepared.end_ms - 1,
        )
        metrics = summarize(normalized, min_trades=context.spec.min_trades).as_dict()
        rows.append(
            {
                "name": window.indexed.prepared.name,
                "return_pct": metrics["total_return_pct"],
                "trade_count": metrics["trade_count"],
                "worst_mae_pct": metrics["worst_mae_pct"],
                "drawdown_pct": metrics["max_closed_equity_drawdown_pct"],
                "max_holding_minutes": metrics["max_holding_minutes"],
                "open_at_end": metrics["open_at_end"],
            }
        )
    return rows


def _first_exit_index(
    window: ExitQueryWindow,
    strategy: StrategySpec,
    *,
    entry_price: float,
    start_index: int,
) -> int | None:
    if start_index >= len(window.indexed.prepared.candles):
        return None
    if strategy.exit_mode == "tp":
        assert strategy.tp_price_pct is not None
        target = entry_price * (1.0 + strategy.tp_price_pct / 100.0)
        return window.high_tree.first_ge(start_index, target)
    assert strategy.rsi_exit is not None
    tree = window.rsi_tree_by_period.get(strategy.rsi_period)
    if tree is None:
        raise ValueError("strategy RSI period was not indexed for exit queries")
    return tree.first_gt(start_index, strategy.rsi_exit)


def _excursions(
    window: ExitQueryWindow,
    strategy: StrategySpec,
    *,
    entry_price: float,
    start_index: int,
    end_index: int,
) -> tuple[float, float]:
    if start_index > end_index:
        return 0.0, 0.0
    low = window.low_tree.range_min(start_index, end_index)
    high = window.high_tree.range_max(start_index, end_index)
    if strategy.exit_mode == "tp":
        assert strategy.tp_price_pct is not None
        target = entry_price * (1.0 + strategy.tp_price_pct / 100.0)
        high = min(high, target)
    return (
        min(0.0, _pct(low / entry_price - 1.0)),
        max(0.0, _pct(high / entry_price - 1.0)),
    )


def _funding_return(
    window: ExitQueryWindow,
    strategy: StrategySpec,
    *,
    entry_time_ms: int,
    entry_price: float,
    start_index: int,
    end_index: int,
    exit_index: int | None,
) -> float:
    total = 0.0
    for indexed in window.funding:
        if indexed.candle_index < start_index or indexed.candle_index > end_index:
            continue
        event = indexed.event
        if event.timestamp_ms <= entry_time_ms:
            continue
        if (
            strategy.exit_mode == "tp"
            and exit_index is not None
            and indexed.candle_index == exit_index
            and event.rate < 0.0
        ):
            continue
        candle = window.indexed.prepared.candles[indexed.candle_index]
        mark = event.mark_price if event.mark_price is not None else candle.close
        total += _pct(-(mark / entry_price) * event.rate)
    return total


def _exit_price(
    candle: Candle,
    strategy: StrategySpec,
    execution: ExecutionModel,
    *,
    entry_price: float,
) -> tuple[float, str]:
    if strategy.exit_mode == "tp":
        assert strategy.tp_price_pct is not None
        target = entry_price * (1.0 + strategy.tp_price_pct / 100.0)
        return (
            target * (1.0 - execution.sell_slippage_bps / 10_000.0),
            "TP_PRICE_REACHED",
        )
    return (
        candle.close * (1.0 - execution.sell_slippage_bps / 10_000.0),
        "RSI_EXIT_REACHED",
    )


def _index_funding(
    candles: Sequence[Candle],
    funding: Sequence[FundingEvent],
) -> tuple[_FundingAtCandle, ...]:
    if not candles or not funding:
        return ()
    open_times = [candle.open_time_ms for candle in candles]
    indexed: list[_FundingAtCandle] = []
    for event in sorted(funding, key=lambda item: item.timestamp_ms):
        candle_index = bisect_right(open_times, event.timestamp_ms) - 1
        if candle_index < 0:
            continue
        candle = candles[candle_index]
        if candle.open_time_ms <= event.timestamp_ms <= candle.close_time_ms:
            indexed.append(_FundingAtCandle(candle_index=candle_index, event=event))
    return tuple(indexed)


def _pct(fraction: float) -> float:
    return fraction * 100.0
