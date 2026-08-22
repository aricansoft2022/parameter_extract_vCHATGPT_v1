from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .crossing_index import IndexedDiscovery, indexed_signals_for_strategy
from .metrics import summarize
from .models import StrategySpec
from .replay import replay_signals
from .signals import Signal
from .study import StudyContext

EntrySignalKey = tuple[int, float, float, float]
EntrySignalWindows = tuple[tuple[Signal, ...], ...]


@dataclass(slots=True)
class EntrySignalCache:
    indexed: IndexedDiscovery
    _signals_by_key: dict[EntrySignalKey, EntrySignalWindows]
    hits: int = 0
    misses: int = 0

    @classmethod
    def create(cls, indexed: IndexedDiscovery) -> "EntrySignalCache":
        return cls(indexed=indexed, _signals_by_key={})

    @staticmethod
    def key(strategy: StrategySpec) -> EntrySignalKey:
        return (
            strategy.rsi_period,
            strategy.rsi_entry,
            strategy.adx_min,
            strategy.adx_max,
        )

    def signals(self, strategy: StrategySpec) -> EntrySignalWindows:
        key = self.key(strategy)
        cached = self._signals_by_key.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        generated = tuple(
            indexed_signals_for_strategy(window, strategy)
            for window in self.indexed.windows
        )
        self._signals_by_key[key] = generated
        self.misses += 1
        return generated

    def prime(self, rows: Mapping[EntrySignalKey, Sequence[Sequence[Signal]]]) -> None:
        """Install exact precomputed signal windows without changing request counters."""
        expected_windows = len(self.indexed.windows)
        prepared_periods = set(self.indexed.prepared.rsi_periods)
        for key, windows in rows.items():
            if len(key) != 4 or key[0] not in prepared_periods:
                raise ValueError("primed entry-signal key is invalid for this indexed discovery")
            if len(windows) != expected_windows:
                raise ValueError("primed entry-signal windows do not match discovery window count")
            normalized: EntrySignalWindows = tuple(tuple(window) for window in windows)
            existing = self._signals_by_key.get(key)
            if existing is not None and existing != normalized:
                raise ValueError("primed entry-signal data conflicts with existing cache value")
            self._signals_by_key[key] = normalized

    def contains(self, key: EntrySignalKey) -> bool:
        return key in self._signals_by_key

    def clear(self) -> None:
        self._signals_by_key.clear()
        self.hits = 0
        self.misses = 0

    @property
    def unique_keys(self) -> int:
        return len(self._signals_by_key)


def evaluate_cached_discovery(
    context: StudyContext,
    cache: EntrySignalCache,
    strategy: StrategySpec,
) -> list[dict[str, object]]:
    indexed = cache.indexed
    prepared = indexed.prepared
    if strategy.symbol != context.spec.symbol or strategy.symbol != prepared.symbol:
        raise ValueError("strategy symbol does not match cached discovery")
    if prepared.study_name != context.spec.name:
        raise ValueError("cached discovery belongs to a different study")
    if prepared.dataset_fingerprint_sha256 != context.spec.dataset_fingerprint_sha256:
        raise ValueError("cached discovery belongs to a different dataset")
    if indexed.validation_accessed or indexed.holdout_accessed:
        raise RuntimeError("cached discovery phase isolation failed")

    signal_windows = cache.signals(strategy)
    rows: list[dict[str, object]] = []
    for window, signals in zip(indexed.windows, signal_windows, strict=True):
        points = window.points_by_period[strategy.rsi_period]
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
