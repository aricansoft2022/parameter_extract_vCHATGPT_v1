import json
from pathlib import Path

import pytest

from parameter_extract.diverse_freeze import freeze_diverse_discovery_result
from parameter_extract.promotion import freeze_discovery_result, verify_candidate_set


def _row(period: int, mode: str, variant: int) -> dict:
    entry = 18.0 + (period - 14) * 4.0 + variant * 2.5
    strategy = {
        "symbol": "BTCUSDT",
        "rsi_period": period,
        "rsi_entry": entry,
        "adx_min": 10.0 + variant * 10.0,
        "adx_max": 45.0 + (period - 14) * 5.0 + variant * 5.0,
        "exit_mode": mode,
        "rsi_exit": 75.0 + variant * 5.0 if mode == "rsi" else None,
        "tp_price_pct": 0.25 + variant * 0.20 if mode == "tp" else None,
    }
    worst = 8.0 + (period - 14) * 3.0 - variant * 4.0 + (2.0 if mode == "tp" else 0.0)
    median = 18.0 + (period - 14) * 4.0 + variant * 5.0
    mae = -8.0 - variant * 12.0 - (period - 14) * 2.0
    drawdown = -2.0 - variant * 3.0 - (period - 14) * 0.5
    windows = []
    for index, name in enumerate(("d1", "d2", "d3", "d4")):
        windows.append(
            {
                "name": name,
                "return_pct": worst + index * 2.0,
                "trade_count": 25 + period - 14 + variant * 3 + index,
                "worst_mae_pct": mae - index,
                "drawdown_pct": drawdown - index * 0.25,
                "max_holding_minutes": 600.0 + variant * 1000.0 + index * 50.0,
                "open_at_end": bool(variant == 1 and index == 3),
            }
        )
    return {
        "stage": "coarse" if variant == 0 else "refined",
        "strategy": strategy,
        "aggregate": {
            "window_count": 4,
            "positive_window_fraction": 1.0,
            "total_trades": sum(row["trade_count"] for row in windows),
            "compounded_window_return_pct": median * 4.0,
            "median_window_return_pct": median,
            "worst_window_return_pct": worst,
            "worst_mae_pct": mae,
            "max_drawdown_pct": drawdown,
            "max_holding_minutes": max(row["max_holding_minutes"] for row in windows),
            "open_at_end_windows": int(variant == 1),
        },
        "windows": windows,
    }


def _discovery_payload() -> dict:
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
        "frontier": frontier,
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_diverse_freeze_is_deterministic_and_is_a_verifiable_subset(tmp_path: Path):
    source = tmp_path / "discovery.json"
    _write(source, _discovery_payload())

    full = freeze_discovery_result(source)
    selected = freeze_diverse_discovery_result(source, count=16)

    assert selected["candidate_count"] == 16
    assert verify_candidate_set(selected) == []
    full_fps = {row["candidate_fingerprint_sha256"] for row in full["candidates"]}
    selected_fps = {row["candidate_fingerprint_sha256"] for row in selected["candidates"]}
    assert selected_fps < full_fps

    metadata = selected["source"]["prevalidation_selection"]
    assert metadata["source_frontier_count"] == 24
    assert metadata["selected_count"] == 16
    assert metadata["parameters_retuned"] is False
    assert metadata["validation_accessed"] is False
    assert metadata["holdout_accessed"] is False
    assert set(metadata["selected_exit_mode_counts"]) == {"rsi", "tp"}
    assert set(metadata["selected_rsi_period_counts"]) == {"14", "15", "16", "17", "18", "19"}

    reversed_source = tmp_path / "discovery-reversed.json"
    payload = _discovery_payload()
    payload["frontier"] = list(reversed(payload["frontier"]))
    _write(reversed_source, payload)
    reversed_selected = freeze_diverse_discovery_result(reversed_source, count=16)

    assert [row["strategy"] for row in selected["candidates"]] == [
        row["strategy"] for row in reversed_selected["candidates"]
    ]


def test_diverse_freeze_rejects_count_above_frontier(tmp_path: Path):
    source = tmp_path / "discovery.json"
    _write(source, _discovery_payload())
    with pytest.raises(ValueError, match="frontier contains only 24"):
        freeze_diverse_discovery_result(source, count=25)


def test_diverse_freeze_rejects_count_too_small_for_declared_coverage(tmp_path: Path):
    source = tmp_path / "discovery.json"
    _write(source, _discovery_payload())
    with pytest.raises(ValueError, match="too small for mandatory discovery coverage"):
        freeze_diverse_discovery_result(source, count=3)
