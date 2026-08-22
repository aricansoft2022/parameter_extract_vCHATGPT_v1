from __future__ import annotations

import math

from parameter_extract.metrics import summarize
from parameter_extract.replay import ReplayResult, Trade


def _trade(net: float, *, entry: int, exit_: int, mae: float) -> Trade:
    return Trade(
        signal_time_ms=entry,
        entry_time_ms=entry,
        entry_price=100,
        exit_signal_time_ms=exit_,
        exit_time_ms=exit_,
        exit_price=100 * (1 + net / 100),
        exit_reason="TEST",
        gross_return_pct=net,
        fee_return_pct=0,
        funding_return_pct=0,
        net_return_pct=net,
        mae_pct=mae,
        mfe_pct=max(net, 0),
        holding_minutes=(exit_ - entry) / 60_000,
    )


def test_summary_compounds_and_flags_small_samples():
    result = ReplayResult(
        trades=(
            _trade(10, entry=0, exit_=60_000, mae=-2),
            _trade(-5, entry=60_000, exit_=120_000, mae=-6),
        ),
        raw_signal_count=2,
        accepted_signal_count=2,
        skipped_while_open=0,
        skipped_pending_entry=0,
        cancelled_on_gap=0,
        open_position=None,
        execution_model="test",
        dataset_start_ms=0,
        dataset_end_ms=120_000,
    )
    metrics = summarize(result, min_trades=3)
    assert math.isclose(metrics.total_return_pct, 4.5)
    assert metrics.sample_status == "INSUFFICIENT_SAMPLE"
    assert metrics.worst_mae_pct == -6
    assert metrics.win_rate_pct == 50.0
