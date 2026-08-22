from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Sequence

from .entry_signal_cache import EntrySignalCache, EntrySignalKey
from .models import StrategySpec
from .signals import Signal


@dataclass(frozen=True, slots=True)
class BulkPrimeStats:
    requested_strategies: int
    requested_unique_keys: int
    already_cached_keys: int
    installed_keys: int
    event_visits: int
    band_membership_checks: int
    keywise_event_scan_upper_bound: int

    def plus(self, other: "BulkPrimeStats") -> "BulkPrimeStats":
        return BulkPrimeStats(
            requested_strategies=self.requested_strategies + other.requested_strategies,
            requested_unique_keys=self.requested_unique_keys + other.requested_unique_keys,
            already_cached_keys=self.already_cached_keys + other.already_cached_keys,
            installed_keys=self.installed_keys + other.installed_keys,
            event_visits=self.event_visits + other.event_visits,
            band_membership_checks=(
                self.band_membership_checks + other.band_membership_checks
            ),
            keywise_event_scan_upper_bound=(
                self.keywise_event_scan_upper_bound
                + other.keywise_event_scan_upper_bound
            ),
        )


EMPTY_BULK_STATS = BulkPrimeStats(0, 0, 0, 0, 0, 0, 0)


def bulk_prime_entry_signals(
    cache: EntrySignalCache,
    strategies: Sequence[StrategySpec],
) -> BulkPrimeStats:
    """Invert exact crossing-event membership into all missing entry keys in one pass.

    Exit parameters are ignored by construction. Every target entry key receives one signal
    tuple per discovery window, including explicit empty tuples for keys with no signals.
    """
    requested_keys: list[EntrySignalKey] = []
    seen: set[EntrySignalKey] = set()
    for strategy in strategies:
        key = EntrySignalCache.key(strategy)
        if key in seen:
            continue
        seen.add(key)
        requested_keys.append(key)

    already_cached = sum(cache.contains(key) for key in requested_keys)
    missing = [key for key in requested_keys if not cache.contains(key)]
    if not missing:
        return BulkPrimeStats(
            requested_strategies=len(strategies),
            requested_unique_keys=len(requested_keys),
            already_cached_keys=already_cached,
            installed_keys=0,
            event_visits=0,
            band_membership_checks=0,
            keywise_event_scan_upper_bound=0,
        )

    keys_by_period: dict[int, list[EntrySignalKey]] = {}
    for key in missing:
        period = key[0]
        if period not in cache.indexed.prepared.rsi_periods:
            raise ValueError(f"entry key RSI period {period} was not indexed")
        keys_by_period.setdefault(period, []).append(key)

    windows_by_key: dict[EntrySignalKey, list[tuple[Signal, ...]]] = {
        key: [] for key in missing
    }
    event_visits = 0
    band_checks = 0
    keywise_upper = 0

    for window in cache.indexed.windows:
        per_window: dict[EntrySignalKey, list[Signal]] = {key: [] for key in missing}
        for period, period_keys in keys_by_period.items():
            entries = sorted({key[1] for key in period_keys})
            bands_by_entry: dict[float, list[EntrySignalKey]] = {
                entry: [] for entry in entries
            }
            for key in period_keys:
                bands_by_entry[key[1]].append(key)

            events = window.events_by_period[period]
            event_visits += len(events)
            keywise_upper += len(events) * len(period_keys)
            for event in events:
                start = bisect_right(entries, event.previous.rsi)
                stop = bisect_left(entries, event.current.rsi)
                if start >= stop:
                    continue
                signal = Signal(
                    candle_index=event.candle_index,
                    timestamp_ms=event.timestamp_ms,
                    reference_price=event.reference_price,
                    previous=event.previous,
                    current=event.current,
                )
                adx = event.current.adx
                for entry in entries[start:stop]:
                    for key in bands_by_entry[entry]:
                        band_checks += 1
                        if key[2] < adx < key[3]:
                            per_window[key].append(signal)

        for key in missing:
            windows_by_key[key].append(tuple(per_window[key]))

    cache.prime(windows_by_key)
    return BulkPrimeStats(
        requested_strategies=len(strategies),
        requested_unique_keys=len(requested_keys),
        already_cached_keys=already_cached,
        installed_keys=len(missing),
        event_visits=event_visits,
        band_membership_checks=band_checks,
        keywise_event_scan_upper_bound=keywise_upper,
    )
