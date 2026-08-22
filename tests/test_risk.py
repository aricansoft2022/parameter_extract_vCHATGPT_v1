import json
from pathlib import Path

import pytest

import parameter_extract.risk as risk_module
from parameter_extract.manifest import sha256_file
from parameter_extract.promotion import candidate_fingerprint
from parameter_extract.risk import run_risk, verify_risk_result
from parameter_extract.selection import _selection_set_fingerprint


def _strategy(entry: float) -> dict:
    return {
        "symbol": "BTCUSDT",
        "rsi_period": 14,
        "rsi_entry": entry,
        "adx_min": 10.0,
        "adx_max": 30.0,
        "exit_mode": "tp",
        "rsi_exit": None,
        "tp_price_pct": 1.0,
    }


def _selected() -> list[dict]:
    strategy_a = _strategy(30.0)
    strategy_b = _strategy(31.0)
    return [
        {
            "priority": 1,
            "original_priority": 1,
            "family_id": "F0001",
            "candidate_fingerprint_sha256": candidate_fingerprint(strategy_a),
            "strategy": strategy_a,
        },
        {
            "priority": 2,
            "original_priority": 3,
            "family_id": "F0002",
            "candidate_fingerprint_sha256": candidate_fingerprint(strategy_b),
            "strategy": strategy_b,
        },
    ]


def _trade(family: str, fp: str, mae: float, holding: float) -> dict:
    return {
        "family_id": family,
        "candidate_fingerprint_sha256": fp,
        "mae_pct": mae,
        "holding_minutes": holding,
    }


def _selection() -> dict:
    selected = _selected()
    source_portfolio_sha = "d" * 64
    selected_fp = _selection_set_fingerprint(
        source_portfolio_sha=source_portfolio_sha,
        slot_count=2,
        selected=selected,
    )
    fp_a = selected[0]["candidate_fingerprint_sha256"]
    fp_b = selected[1]["candidate_fingerprint_sha256"]
    return {
        "selected_set_fingerprint_sha256": selected_fp,
        "source_portfolio_result_sha256": source_portfolio_sha,
        "study_fingerprint_sha256": "a" * 64,
        "dataset_fingerprint_sha256": "b" * 64,
        "symbol": "BTCUSDT",
        "execution": {"name": "expected_live"},
        "slot_count": 2,
        "selected": selected,
        "selected_portfolio_windows": [
            {
                "phase": "discovery",
                "name": "d1",
                "trades": [
                    _trade("F0001", fp_a, -1.0, 30.0),
                    _trade("F0002", fp_b, -2.0, 45.0),
                ],
            },
            {
                "phase": "validation",
                "name": "v1",
                "trades": [
                    _trade("F0001", fp_a, -3.0, 60.0),
                    _trade("F0002", fp_b, -1.5, 40.0),
                ],
            },
        ],
    }


def _holdout(selection_sha: str, selection: dict, *, status: str = "PASS") -> dict:
    selected = selection["selected"]
    fp_a = selected[0]["candidate_fingerprint_sha256"]
    fp_b = selected[1]["candidate_fingerprint_sha256"]
    return {
        "source_selection_result_sha256": selection_sha,
        "source_portfolio_result_sha256": selection["source_portfolio_result_sha256"],
        "source_selected_set_fingerprint_sha256": selection[
            "selected_set_fingerprint_sha256"
        ],
        "study_fingerprint_sha256": selection["study_fingerprint_sha256"],
        "dataset_fingerprint_sha256": selection["dataset_fingerprint_sha256"],
        "symbol": selection["symbol"],
        "execution": selection["execution"],
        "slot_count": selection["slot_count"],
        "selected": selection["selected"],
        "status": status,
        "windows": [
            {
                "phase": "holdout",
                "name": "h1",
                "trades": [
                    _trade("F0001", fp_a, -2.5, 50.0),
                    _trade("F0002", fp_b, -2.0, 35.0),
                ],
            }
        ],
    }


def _write_inputs(
    tmp_path: Path,
    *,
    allocation_pct: float = 50.0,
    holdout_status: str = "PASS",
):
    selection = _selection()
    selection_path = tmp_path / "selection-result.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    holdout_path = tmp_path / "holdout-result.json"
    holdout_path.write_text(
        json.dumps(
            _holdout(
                sha256_file(selection_path),
                selection,
                status=holdout_status,
            )
        ),
        encoding="utf-8",
    )
    risk_path = tmp_path / "risk.json"
    risk_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "deployment budget",
                "source_holdout_result_sha256": sha256_file(holdout_path),
                "max_leverage_cap": 10,
                "mae_stress_multiplier": 1.5,
                "extra_adverse_move_pct": 0.5,
                "required_headroom_pct": 2.0,
                "allocation_pct": allocation_pct,
                "reserve_pct": 4.0,
                "min_total_closed_trades": 6,
                "min_closed_trades_per_family": 3,
                "max_stressed_adverse_move_pct": 8.0,
            }
        ),
        encoding="utf-8",
    )
    return selection_path, holdout_path, risk_path


def test_risk_budget_uses_mae_without_optimizing_leverage(tmp_path: Path, monkeypatch):
    selection_path, holdout_path, risk_path = _write_inputs(tmp_path)
    monkeypatch.setattr(risk_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(risk_module, "verify_holdout_result", lambda payload: [])

    result = run_risk(selection_path, holdout_path, risk_path)
    assert result["status"] == "RISK_BUDGET_PASS"
    assert result["summary"]["closed_trade_count"] == 6
    assert result["summary"]["worst_adverse_move_pct"] == 3.0
    assert result["summary"]["stressed_adverse_move_pct"] == 5.0
    assert result["summary"]["required_adverse_budget_pct"] == 7.0
    assert result["summary"]["mae_budget_leverage_ceiling"] == 14
    assert result["summary"]["provisional_deployment_leverage"] == 10
    assert result["leverage_optimized"] is False
    assert result["exchange_liquidation_validated"] is False
    assert result["teams_export_ready"] is False
    assert result["selected_set_changed"] is False
    assert result["selected"] == _selection()["selected"]
    json.dumps(result, allow_nan=False)
    assert verify_risk_result(result) == []


def test_risk_rejects_allocation_that_changes_researched_slot_count(tmp_path: Path, monkeypatch):
    selection_path, holdout_path, risk_path = _write_inputs(tmp_path, allocation_pct=25.0)
    monkeypatch.setattr(risk_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(risk_module, "verify_holdout_result", lambda payload: [])
    with pytest.raises(ValueError, match="implies 4 live slots but research used 2"):
        run_risk(selection_path, holdout_path, risk_path)


def test_risk_requires_passed_sealed_holdout(tmp_path: Path, monkeypatch):
    selection_path, holdout_path, risk_path = _write_inputs(tmp_path, holdout_status="FAIL")
    monkeypatch.setattr(risk_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(risk_module, "verify_holdout_result", lambda payload: [])
    with pytest.raises(ValueError, match="requires a PASS sealed holdout"):
        run_risk(selection_path, holdout_path, risk_path)


def test_risk_blocks_instead_of_dropping_family_with_thin_evidence(tmp_path: Path, monkeypatch):
    selection = _selection()
    fp_a = selection["selected"][0]["candidate_fingerprint_sha256"]
    fp_b = selection["selected"][1]["candidate_fingerprint_sha256"]
    selection["selected_portfolio_windows"][0]["trades"] = [
        _trade("F0001", fp_a, -1.0, 30.0)
    ]
    selection["selected_portfolio_windows"][1]["trades"] = []
    selection_path = tmp_path / "selection-result.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    holdout = _holdout(sha256_file(selection_path), selection)
    holdout["windows"][0]["trades"] = [
        _trade("F0001", fp_a, -2.0, 40.0),
        _trade("F0002", fp_b, -2.0, 40.0),
    ]
    holdout_path = tmp_path / "holdout-result.json"
    holdout_path.write_text(json.dumps(holdout), encoding="utf-8")
    risk_path = tmp_path / "risk.json"
    risk_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "thin block",
                "source_holdout_result_sha256": sha256_file(holdout_path),
                "max_leverage_cap": 5,
                "mae_stress_multiplier": 1.2,
                "extra_adverse_move_pct": 0.5,
                "required_headroom_pct": 2.0,
                "allocation_pct": 50.0,
                "reserve_pct": 4.0,
                "min_total_closed_trades": 3,
                "min_closed_trades_per_family": 2,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(risk_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(risk_module, "verify_holdout_result", lambda payload: [])
    result = run_risk(selection_path, holdout_path, risk_path)
    assert result["status"] == "BLOCK"
    assert any(
        reason.startswith("INSUFFICIENT_FAMILY_RISK_TRADES:F0002")
        for reason in result["failure_reasons"]
    )
    assert result["selected_set_changed"] is False


def test_risk_result_detects_evidence_and_selected_mutation(tmp_path: Path, monkeypatch):
    selection_path, holdout_path, risk_path = _write_inputs(tmp_path)
    monkeypatch.setattr(risk_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(risk_module, "verify_holdout_result", lambda payload: [])
    result = run_risk(selection_path, holdout_path, risk_path)

    mutated = json.loads(json.dumps(result))
    mutated["trade_evidence"][0]["adverse_move_pct"] = 99.0
    problems = verify_risk_result(mutated)
    assert any("adverse move disagrees" in problem for problem in problems)

    mutated = json.loads(json.dumps(result))
    mutated["selected"][0]["strategy"]["rsi_entry"] = 29.0
    problems = verify_risk_result(mutated)
    assert any("strategy fingerprint mismatch" in problem for problem in problems)
    assert any("selected-set fingerprint" in problem for problem in problems)
