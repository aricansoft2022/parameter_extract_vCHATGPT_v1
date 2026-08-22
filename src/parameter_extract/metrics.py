from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass

from .replay import ReplayResult


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    trade_count: int
    sample_status: str
    total_return_pct: float
    win_rate_pct: float | None
    profit_factor: float | None
    median_trade_pct: float | None
    best_trade_pct: float | None
    worst_trade_pct: float | None
    max_closed_equity_drawdown_pct: float
    median_holding_minutes: float | None
    p95_holding_minutes: float | None
    max_holding_minutes: float | None
    worst_mae_pct: float | None
    median_mae_pct: float | None
    median_mfe_pct: float | None
    exposure_pct: float
    open_at_end: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def summarize(result: ReplayResult, *, min_trades: int = 30) -> ReplayMetrics:
    returns = [trade.net_return_pct for trade in result.trades]
    holding = [trade.holding_minutes for trade in result.trades]
    maes = [trade.mae_pct for trade in result.trades]
    mfes = [trade.mfe_pct for trade in result.trades]

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        drawdown = (equity / peak - 1.0) * 100.0
        max_drawdown = min(max_drawdown, drawdown)

    positives = sum(value for value in returns if value > 0.0)
    negatives = -sum(value for value in returns if value < 0.0)
    if positives == 0.0 and negatives == 0.0:
        profit_factor = None
    elif negatives == 0.0:
        profit_factor = math.inf
    else:
        profit_factor = positives / negatives

    dataset_ms = 0
    if result.dataset_start_ms is not None and result.dataset_end_ms is not None:
        dataset_ms = max(0, result.dataset_end_ms - result.dataset_start_ms)
    exposure_ms = sum(
        max(0, trade.exit_time_ms - trade.entry_time_ms) for trade in result.trades
    )
    if result.open_position is not None and result.dataset_end_ms is not None:
        exposure_ms += max(0, result.dataset_end_ms - result.open_position.entry_time_ms)
    exposure_pct = 0.0 if dataset_ms == 0 else min(100.0, exposure_ms / dataset_ms * 100.0)

    return ReplayMetrics(
        trade_count=len(returns),
        sample_status="OK" if len(returns) >= min_trades else "INSUFFICIENT_SAMPLE",
        total_return_pct=(equity - 1.0) * 100.0,
        win_rate_pct=None if not returns else sum(value > 0.0 for value in returns) / len(returns) * 100.0,
        profit_factor=profit_factor,
        median_trade_pct=None if not returns else statistics.median(returns),
        best_trade_pct=None if not returns else max(returns),
        worst_trade_pct=None if not returns else min(returns),
        max_closed_equity_drawdown_pct=max_drawdown,
        median_holding_minutes=None if not holding else statistics.median(holding),
        p95_holding_minutes=_percentile(holding, 95.0),
        max_holding_minutes=None if not holding else max(holding),
        worst_mae_pct=None if not maes else min(maes),
        median_mae_pct=None if not maes else statistics.median(maes),
        median_mfe_pct=None if not mfes else statistics.median(mfes),
        exposure_pct=exposure_pct,
        open_at_end=result.open_position is not None,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100.0) * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
