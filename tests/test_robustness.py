import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.robustness as robustness_module
from parameter_extract.manifest import sha256_file
from parameter_extract.models import ExecutionModel
from parameter_extract.robustness import run_robustness


def _validation_result() -> dict:
    execution = {
        "name": "expected_live",
        "entry_timing": "next_open",
        "taker_fee_bps": 4.0,
        "buy_slippage_bps": 2.0,
        "sell_slippage_bps": 2.0,
    }
    aggregate = {
        "window_count": 1,
        "positive_window_fraction": 1.0,
        "total_trades": 3,
        "compounded_window_return_pct": 4.0,
        "median_window_return_pct": 4.0,
        "worst_window_return_pct": 4.0,
        "worst_mae_pct": -2.0,
        "max_drawdown_pct": -1.0,
        "max_holding_minutes": 50.0,
        "open_at_end_windows": 0,
    }
    strategy = {
        "symbol": "BTCUSDT",
        "rsi_period": 14,
        "rsi_entry": 30.0,
        "adx_min": 10.0,
        "adx_max": 30.0,
        "exit_mode": "tp",
        "rsi_exit": None,
        "tp_price_pct": 1.0,
    }
    return {
        "schema_version": 1,
        "kind": "parameter_extract.validation_result",
        "validation": "gate",
        "validation_fingerprint_sha256": "e" * 64,
        "validation_spec": {
            "name": "gate",
            "source_candidate_set_fingerprint_sha256": "d" * 64,
            "gates": {
                "min_total_trades": 1,
                "min_positive_window_fraction": 0.5,
                "min_median_window_return_pct": 0.0,
                "min_worst_window_return_pct": -2.0,
                "min_worst_mae_pct": -5.0,
                "max_open_at_end_windows": 0,
            },
        },
        "source_candidate_set_fingerprint_sha256": "d" * 64,
        "study_fingerprint_sha256": "b" * 64,
        "dataset_fingerprint_sha256": "a" * 64,
        "symbol": "BTCUSDT",
        "execution": execution,
        "parameters_retuned": False,
        "discovery_accessed": False,
        "validation_accessed": True,
        "holdout_accessed": False,
        "candidate_count": 1,
        "promoted_count": 1,
        "rejected_count": 0,
        "candidates": [
            {
                "candidate_fingerprint_sha256": "c" * 64,
                "strategy": strategy,
                "discovery": {"aggregate": aggregate, "windows": []},
                "validation": {"aggregate": aggregate, "windows": []},
                "promotion_status": "PASS",
                "rejection_reasons": [],
            }
        ],
    }


def _write_contracts(tmp_path: Path) -> tuple[Path, Path]:
    validation_path = tmp_path / "validation-result.json"
    validation_path.write_text(json.dumps(_validation_result()), encoding="utf-8")
    robustness = {
        "schema_version": 1,
        "name": "axis robustness",
        "source_validation_result_sha256": sha256_file(validation_path),
        "steps": {
            "include_rsi_period": True,
            "rsi_entry": 0.5,
            "adx_min": 1.0,
            "adx_max": 1.0,
            "tp_price_pct": 0.1,
            "rsi_exit": 0.5,
        },
        "gates": {
            "min_neighbor_count": 6,
            "min_validation_pass_fraction": 0.75,
            "min_discovery_positive_fraction": 0.75,
            "max_center_validation_advantage_pct": 5.0,
        },
        "max_neighbor_evaluations": 20,
    }
    robustness_path = tmp_path / "robustness.json"
    robustness_path.write_text(json.dumps(robustness), encoding="utf-8")
    return validation_path, robustness_path


def test_axis_neighbors_are_diagnostic_only_and_never_touch_holdout(tmp_path: Path, monkeypatch):
    validation_path, robustness_path = _write_contracts(tmp_path)
    context = SimpleNamespace(
        spec=SimpleNamespace(
            name="study",
            symbol="BTCUSDT",
            dataset_fingerprint_sha256="a" * 64,
            execution=ExecutionModel.expected_live(),
        )
    )
    calls = []

    def fake_evaluate(_context, strategy, *, phases, reveal_holdout=False):
        calls.append((strategy, tuple(phases), reveal_holdout))
        metrics = {
            "total_return_pct": 3.5,
            "trade_count": 3,
            "worst_mae_pct": -2.5,
            "max_closed_equity_drawdown_pct": -1.5,
            "max_holding_minutes": 45.0,
            "open_at_end": False,
        }
        return {
            "phases_evaluated": ["discovery", "validation"],
            "holdout_revealed": False,
            "windows": [
                {"phase": "discovery", "name": "d1", "metrics": metrics},
                {"phase": "validation", "name": "v1", "metrics": metrics},
            ],
        }

    monkeypatch.setattr(robustness_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(robustness_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(robustness_module, "evaluate_strategy", fake_evaluate)

    result = run_robustness(
        "study.json",
        validation_path,
        robustness_path,
        data_directory=tmp_path,
    )
    assert result["parameters_retuned"] is False
    assert result["neighbor_strategies_promotable"] is False
    assert result["holdout_accessed"] is False
    assert result["center_count"] == 1
    assert result["robust_count"] == 1
    center = result["centers"][0]
    assert center["status"] == "ROBUST"
    assert center["center_strategy"]["rsi_entry"] == 30.0
    assert center["center_parameters_retuned"] is False
    assert center["neighbor_strategies_promotable"] is False
    assert center["metrics"]["neighbor_count"] == 9
    assert calls and all(phases == ("discovery", "validation") for _, phases, _ in calls)
    assert all(reveal is False for _, _, reveal in calls)
    assert all(strategy.rsi_entry in {29.5, 30.0, 30.5} for strategy, _, _ in calls)


def test_robustness_contract_is_pinned_to_exact_validation_file(tmp_path: Path, monkeypatch):
    validation_path, robustness_path = _write_contracts(tmp_path)
    validation_path.write_text(json.dumps({**_validation_result(), "validation": "changed"}), encoding="utf-8")
    context = SimpleNamespace(
        spec=SimpleNamespace(
            name="study",
            symbol="BTCUSDT",
            dataset_fingerprint_sha256="a" * 64,
            execution=ExecutionModel.expected_live(),
        )
    )
    monkeypatch.setattr(robustness_module, "load_study_context", lambda *a, **k: context)
    with pytest.raises(ValueError, match="different validation-result file"):
        run_robustness(
            "study.json",
            validation_path,
            robustness_path,
            data_directory=tmp_path,
        )
