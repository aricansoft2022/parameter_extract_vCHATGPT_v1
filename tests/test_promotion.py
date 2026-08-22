import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.promotion as promotion_module
from parameter_extract.models import ExecutionModel
from parameter_extract.promotion import (
    freeze_discovery_result,
    run_validation,
    verify_candidate_set,
)


def _discovery_result() -> dict:
    execution = {
        "name": "expected_live",
        "entry_timing": "next_open",
        "taker_fee_bps": 4.0,
        "buy_slippage_bps": 2.0,
        "sell_slippage_bps": 2.0,
    }
    base_aggregate = {
        "window_count": 2,
        "positive_window_fraction": 1.0,
        "total_trades": 8,
        "compounded_window_return_pct": 5.0,
        "median_window_return_pct": 2.5,
        "worst_window_return_pct": 2.0,
        "worst_mae_pct": -2.0,
        "max_drawdown_pct": -1.0,
        "max_holding_minutes": 60.0,
        "open_at_end_windows": 0,
    }
    frontier = []
    for entry in (30.0, 31.0):
        frontier.append(
            {
                "stage": "coarse",
                "strategy": {
                    "symbol": "BTCUSDT",
                    "rsi_period": 14,
                    "rsi_entry": entry,
                    "adx_min": 10.0,
                    "adx_max": 30.0,
                    "exit_mode": "tp",
                    "rsi_exit": None,
                    "tp_price_pct": 1.0,
                },
                "aggregate": dict(base_aggregate),
                "windows": [{"name": "d1"}, {"name": "d2"}],
            }
        )
    return {
        "schema_version": 1,
        "kind": "parameter_extract.discovery_search",
        "search": "search",
        "search_fingerprint_sha256": "c" * 64,
        "study": "study",
        "study_fingerprint_sha256": "b" * 64,
        "dataset_fingerprint_sha256": "a" * 64,
        "execution": execution,
        "symbol": "BTCUSDT",
        "phase_used": "discovery",
        "validation_accessed": False,
        "holdout_accessed": False,
        "pareto_objectives": ["worst_window_return_pct:max"],
        "frontier": frontier,
    }


def _write_discovery(tmp_path: Path) -> Path:
    path = tmp_path / "discovery-search.json"
    path.write_text(json.dumps(_discovery_result()), encoding="utf-8")
    return path


def test_freeze_creates_self_verifying_candidate_set(tmp_path: Path):
    frozen = freeze_discovery_result(_write_discovery(tmp_path))
    assert frozen["parameters_frozen"] is True
    assert frozen["candidate_count"] == 2
    assert verify_candidate_set(frozen) == []
    assert all(len(row["candidate_fingerprint_sha256"]) == 64 for row in frozen["candidates"])

    frozen["candidates"][0]["strategy"]["rsi_entry"] += 0.5
    problems = verify_candidate_set(frozen)
    assert any("fingerprint mismatch" in problem for problem in problems)


def test_validation_only_uses_frozen_parameters_and_validation_phase(tmp_path: Path, monkeypatch):
    candidate_set = freeze_discovery_result(_write_discovery(tmp_path))
    candidate_path = tmp_path / "candidates.json"
    candidate_path.write_text(json.dumps(candidate_set), encoding="utf-8")

    validation = {
        "schema_version": 1,
        "name": "validation gate",
        "source_candidate_set_fingerprint_sha256": candidate_set[
            "candidate_set_fingerprint_sha256"
        ],
        "gates": {
            "min_total_trades": 2,
            "min_positive_window_fraction": 0.5,
            "min_median_window_return_pct": 0.0,
            "min_worst_window_return_pct": -2.0,
            "min_worst_mae_pct": -5.0,
            "max_open_at_end_windows": 0,
        },
    }
    validation_path = tmp_path / "validation.json"
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    study_spec = SimpleNamespace(
        name="study",
        symbol="BTCUSDT",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
    )
    context = SimpleNamespace(spec=study_spec)
    calls = []

    def fake_evaluate(_context, strategy, *, phases, reveal_holdout=False):
        calls.append((strategy.rsi_entry, tuple(phases), reveal_holdout))
        good = strategy.rsi_entry == 30.0
        return_value = 2.0 if good else -4.0
        mae = -2.0 if good else -8.0
        metrics = {
            "total_return_pct": return_value,
            "trade_count": 2,
            "worst_mae_pct": mae,
            "max_closed_equity_drawdown_pct": -1.0 if good else -6.0,
            "max_holding_minutes": 40.0,
            "open_at_end": False,
        }
        return {
            "phases_evaluated": ["validation"],
            "holdout_revealed": False,
            "windows": [{"name": "v1", "metrics": metrics}],
        }

    monkeypatch.setattr(promotion_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(promotion_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(promotion_module, "evaluate_strategy", fake_evaluate)

    result = run_validation(
        "study.json",
        candidate_path,
        validation_path,
        data_directory=tmp_path,
    )
    assert result["parameters_retuned"] is False
    assert result["discovery_accessed"] is False
    assert result["validation_accessed"] is True
    assert result["holdout_accessed"] is False
    assert result["candidate_count"] == 2
    assert result["promoted_count"] == 1
    assert result["rejected_count"] == 1
    assert {row["promotion_status"] for row in result["candidates"]} == {"PASS", "REJECT"}
    assert all(phases == ("validation",) and reveal is False for _, phases, reveal in calls)
    assert sorted(entry for entry, _, _ in calls) == [30.0, 31.0]


def test_validation_contract_must_pin_exact_candidate_set(tmp_path: Path, monkeypatch):
    candidate_set = freeze_discovery_result(_write_discovery(tmp_path))
    candidate_path = tmp_path / "candidates.json"
    candidate_path.write_text(json.dumps(candidate_set), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    validation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "wrong pin",
                "source_candidate_set_fingerprint_sha256": "d" * 64,
                "gates": {
                    "min_total_trades": 1,
                    "min_positive_window_fraction": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )
    context = SimpleNamespace(
        spec=SimpleNamespace(
            name="study",
            symbol="BTCUSDT",
            dataset_fingerprint_sha256="a" * 64,
            execution=ExecutionModel.expected_live(),
        )
    )
    monkeypatch.setattr(promotion_module, "load_study_context", lambda *a, **k: context)
    with pytest.raises(ValueError, match="different candidate set"):
        run_validation(
            "study.json",
            candidate_path,
            validation_path,
            data_directory=tmp_path,
        )
