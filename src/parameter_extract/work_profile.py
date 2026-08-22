from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .exit_query import ExitQueryWindow


@dataclass(slots=True)
class QueryWorkProfile:
    candidate_evaluations: int = 0
    candidate_window_replays: int = 0
    accepted_positions: int = 0
    closed_trades: int = 0
    open_positions: int = 0
    exit_lookup_requests: int = 0
    excursion_range_requests: int = 0
    funding_event_checks: int = 0
    closed_trade_signal_bisects: int = 0

    def record(
        self,
        compact_windows: Sequence[dict[str, Any]],
        query_windows: Sequence[ExitQueryWindow],
    ) -> None:
        if len(compact_windows) != len(query_windows):
            raise ValueError("work-profile windows do not match exit-query windows")
        self.candidate_evaluations += 1
        self.candidate_window_replays += len(compact_windows)
        for row, window in zip(compact_windows, query_windows, strict=True):
            closed = int(row["trade_count"])
            is_open = bool(row["open_at_end"])
            accepted = closed + int(is_open)
            self.accepted_positions += accepted
            self.closed_trades += closed
            self.open_positions += int(is_open)
            self.exit_lookup_requests += accepted
            self.excursion_range_requests += accepted
            # `_funding_return` currently iterates the complete indexed funding tuple once
            # for every accepted position, then applies range/timestamp/mode filters.
            self.funding_event_checks += accepted * len(window.funding)
            # One bisect locates the next reusable raw signal after every closed trade.
            self.closed_trade_signal_bisects += closed

    def as_dict(self) -> dict[str, int]:
        return asdict(self)
