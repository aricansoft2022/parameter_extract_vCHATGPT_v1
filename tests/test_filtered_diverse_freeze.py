import json
from pathlib import Path

import pytest

from parameter_extract.filtered_diverse_freeze import (
    freeze_diverse_discovery_result_by_exit_mode,
)
from parameter_extract.manifest import sha256_file
from parameter_extract.promotion import verify_candidate_set


def _row(period: int, mode: str, variant: int) -> dict:
    entry = 18.0 + (period - 14) * 3.0 + variant * 2.0
    worst = 10.0 + (period - 14) * 2.0 - variant * 3.0
    median = 20.0 + (period - 14) * 3.0 + variant * 4.0
    mae = -8.0 - variant * 10.0 - (period - 14)
    drawdown = -2.0 - variant * 2.0
    windows = [
        {
            "name": f"d{index + 1}",
            "return_pct": worst + index,
            "trade_count": 30 + index + variant,
            "worst_mae_pct": mae - index,
            "drawdown_pct": drawdown - index * 0.25,
            "max_holding_minutes": 600.0 + variant * 500.0 + index * 30.0,
            "open_at_end": False,
        }
        for index in range(4)
    ]
    return {
        "stage": "coarse" if variant == 0 else "refined",
        "strategy": {
            "symbol": "BTCUSDT",
            "rsi_period": period,
            "rsi_entry": entry,
            "adx_min": 10.0 + variant * 10.0,
            "adx_max": 50.0 + (period - 14) * 5.0 + variant * 5.0,
            "exit_mode": mode,
            "rsi_exit": 80.0 if mode == "rsi" else None,
            "tp_price_pct": 0.31 + variant * 0.18 if mode == "tp" else None,
        },
        "aggregate": {
            "window_count": 4,
            "positive_window_fraction": 1.0,
            "total_trades": sum(window["trade_count"] for window in windows),
            "compounded_window_return_pct": median * 4.0,
            "median_window_return_pct": median,
            "worst_window_return_pct": worst,
            "worst_mae_pct": mae,
            "max_drawdown_pct": drawdown,
            "max_holding_minutes": max(window["max_holding_minutes"] for window in windows),
            "open_at_end_windows": 0,
        },
        "windows": windows,
    }


def _payload() -> dict:
    frontier = [
        _row(period, mode, variant)
        for period in range(14, 20)
        for mode in ("tp", "rsi")
        for variant in (0, 1)
    ]
    return {
        "schema_version": 1,
        "kind": "parameter_extract.discovery_search",
        "search_fingerprint_sha256": "1" * 64,
        "study_fingerprint_sha256": "2" * 64,
        "dataset_fingerprint_sha256": "3" * 64,
        "execution": {
            "name": "expected_live",
            "entry_timing": "next_open",
            "taker_fee_bps": 5.0,
            "buy_slippage_bps": 2.0,
            "sell_slippage_bps": 2.0,
        },
        "symbol": "BTCUSDT",
        "phase_used": "discovery",
        "validation_accessed": False,
        "holdout_accessed": False,
        "pareto_objectives": [
            "worst_window_return_pct:max",
            "median_window_return_pct:max",
            "worst_mae_pct:max",
            "max_drawdown_pct:max",
        ],
        "pareto_candidates": len(frontier),
        "frontier": frontier,
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_tp_filter_freezes_only_tp_and_pins_original_discovery(tmp_path: Path):
    source = tmp_path / "discovery.json"
    _write(source, _payload())

    result = freeze_diverse_discovery_result_by_exit_mode(source, count=8, exit_mode="tp")

    assert result["candidate_count"] == 8
    assert verify_candidate_set(result) == []
    assert {row["strategy"]["exit_mode"] for row in result["candidates"]} == {"tp"}

    selection = result["source"]["prevalidation_selection"]
    assert selection["source_frontier_count"] == 24
    assert selection["eligible_frontier_count"] == 12
    assert selection["eligibility_filter"] == {"exit_mode": "tp"}
    assert selection["selected_exit_mode_counts"] == {"tp": 8}
    assert set(selection["selected_rsi_period_counts"]) == {"14", "15", "16", "17", "18", "19"}
    assert selection["validation_accessed"] is False
    assert selection["holdout_accessed"] is False
    assert result["source"]["search_result_sha256"] == sha256_file(source)


def test_filter_rejects_count_above_eligible_frontier(tmp_path: Path):
    source = tmp_path / "discovery.json"
    _write(source, _payload())

    with pytest.raises(ValueError, match="eligibility contains only 12"):
        freeze_diverse_discovery_result_by_exit_mode(source, count=13, exit_mode="tp")


def test_filter_rejects_invalid_exit_mode(tmp_path: Path):
    source = tmp_path / "discovery.json"
    _write(source, _payload())

    with pytest.raises(ValueError, match="exit_mode must be"):
        freeze_diverse_discovery_result_by_exit_mode(source, count=8, exit_mode="other")
