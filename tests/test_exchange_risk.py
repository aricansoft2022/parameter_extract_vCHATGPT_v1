import json
from pathlib import Path

import pytest

import parameter_extract.exchange_risk as exchange_module
from parameter_extract.exchange_risk import (
    Bracket,
    maintenance_margin,
    run_exchange_risk,
    solve_isolated_long_liquidation_price,
    verify_exchange_risk_result,
)
from parameter_extract.manifest import sha256_file


def _brackets() -> list[dict]:
    return [
        {
            "bracket": 1,
            "initialLeverage": 20,
            "notionalFloor": 0,
            "notionalCap": 1000,
            "maintMarginRatio": 0.005,
            "cum": 0.0,
        },
        {
            "bracket": 2,
            "initialLeverage": 10,
            "notionalFloor": 1000,
            "notionalCap": 5000,
            "maintMarginRatio": 0.01,
            "cum": 5.0,
        },
        {
            "bracket": 3,
            "initialLeverage": 5,
            "notionalFloor": 5000,
            "notionalCap": 20000,
            "maintMarginRatio": 0.025,
            "cum": 80.0,
        },
    ]


def _dataclass_brackets() -> tuple[Bracket, ...]:
    return tuple(
        Bracket(
            bracket=row["bracket"],
            initial_leverage=row["initialLeverage"],
            notional_floor=row["notionalFloor"],
            notional_cap=row["notionalCap"],
            maint_margin_ratio=row["maintMarginRatio"],
            cum=row["cum"],
        )
        for row in _brackets()
    )


def _reported(entry: float, qty: float, wallet: float) -> float:
    value, _ = solve_isolated_long_liquidation_price(
        entry_price=entry,
        position_amt=qty,
        isolated_wallet=wallet,
        brackets=_dataclass_brackets(),
    )
    return value


def _snapshot(*, include_parity: bool = True, bracket2_leverage: int = 10) -> dict:
    brackets = _brackets()
    brackets[1]["initialLeverage"] = bracket2_leverage
    cases = []
    if include_parity:
        cases = [
            {
                "name": "small-first-bracket",
                "entry_price": 100.0,
                "position_amt": 10.0,
                "isolated_wallet": 100.0,
                "reported_liquidation_price": _reported(100.0, 10.0, 100.0),
            },
            {
                "name": "second-bracket",
                "entry_price": 100.0,
                "position_amt": 20.0,
                "isolated_wallet": 200.0,
                "reported_liquidation_price": _reported(100.0, 20.0, 200.0),
            },
        ]
    return {
        "schema_version": 1,
        "captured_at_utc": "2026-08-22T14:45:00Z",
        "source": "Binance GET /fapi/v1/leverageBracket + /fapi/v3/positionRisk",
        "symbol": "BTCUSDT",
        "margin_asset": "USDT",
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "auto_add_margin": False,
        "notional_coef": 1.0,
        "brackets": brackets,
        "liquidation_parity_cases": cases,
    }


def _risk_result() -> dict:
    return {
        "status": "RISK_BUDGET_PASS",
        "symbol": "BTCUSDT",
        "slot_count": 2,
        "allocation_pct": 50.0,
        "reserve_pct": 4.0,
        "source_selected_set_fingerprint_sha256": "c" * 64,
        "summary": {
            "provisional_deployment_leverage": 10,
            "required_adverse_budget_pct": 7.0,
        },
    }


def _write_inputs(
    tmp_path: Path,
    *,
    include_parity: bool = True,
    bracket2_leverage: int = 10,
    baseline_max: float = 800.0,
):
    risk_path = tmp_path / "risk-result.json"
    risk_path.write_text(json.dumps(_risk_result()), encoding="utf-8")
    snapshot_path = tmp_path / "exchange-snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            _snapshot(
                include_parity=include_parity,
                bracket2_leverage=bracket2_leverage,
            )
        ),
        encoding="utf-8",
    )
    contract_path = tmp_path / "exchange-risk.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "Binance isolated gate",
                "source_risk_result_sha256": sha256_file(risk_path),
                "source_exchange_snapshot_sha256": sha256_file(snapshot_path),
                "baseline_capital_min_usdt": 100.0,
                "baseline_capital_max_usdt": baseline_max,
                "isolated_wallet_haircut_pct": 0.0,
                "min_liquidation_headroom_over_required_budget_pct": 1.0,
                "min_parity_cases": 2,
                "max_parity_error_bps": 0.01,
            }
        ),
        encoding="utf-8",
    )
    return risk_path, snapshot_path, contract_path


def test_piecewise_liquidation_uses_bracket_cum_and_is_continuous():
    brackets = _dataclass_brackets()
    assert maintenance_margin(1000.0, brackets[0]) == pytest.approx(5.0)
    assert maintenance_margin(1000.0, brackets[1]) == pytest.approx(5.0)
    assert maintenance_margin(5000.0, brackets[1]) == pytest.approx(45.0)
    assert maintenance_margin(5000.0, brackets[2]) == pytest.approx(45.0)

    price, bracket = solve_isolated_long_liquidation_price(
        entry_price=100.0,
        position_amt=20.0,
        isolated_wallet=200.0,
        brackets=brackets,
    )
    assert bracket.bracket == 2
    assert price == pytest.approx((2000.0 - 200.0 - 5.0) / (20.0 * 0.99))


def test_exchange_risk_passes_only_after_parity_and_headroom(tmp_path: Path, monkeypatch):
    risk_path, snapshot_path, contract_path = _write_inputs(tmp_path, baseline_max=800.0)
    monkeypatch.setattr(exchange_module, "verify_risk_result", lambda payload: [])

    result = run_exchange_risk(risk_path, snapshot_path, contract_path)
    assert result["status"] == "EXCHANGE_RISK_PASS"
    assert result["exchange_liquidation_validated"] is True
    assert result["teams_export_ready"] is True
    assert result["liquidation_parity"]["case_count"] == 2
    assert result["liquidation_parity"]["max_error_bps"] == pytest.approx(0.0)
    assert result["worst_case"]["liquidation_distance_pct"] > 7.0
    assert result["liquidation_headroom_over_required_budget_pct"] > 1.0
    assert result["alpha_parameters_retuned"] is False
    assert result["leverage_optimized"] is False
    json.dumps(result, allow_nan=False)
    assert verify_exchange_risk_result(result) == []


def test_exchange_risk_blocks_without_required_real_parity_cases(tmp_path: Path, monkeypatch):
    risk_path, snapshot_path, contract_path = _write_inputs(
        tmp_path,
        include_parity=False,
    )
    monkeypatch.setattr(exchange_module, "verify_risk_result", lambda payload: [])
    result = run_exchange_risk(risk_path, snapshot_path, contract_path)
    assert result["status"] == "BLOCK"
    assert "INSUFFICIENT_LIQUIDATION_PARITY_CASES" in result["failure_reasons"]
    assert "LIQUIDATION_MODEL_PARITY" in result["failure_reasons"]
    assert result["teams_export_ready"] is False


def test_exchange_risk_blocks_if_notional_bracket_disallows_leverage(tmp_path: Path, monkeypatch):
    risk_path, snapshot_path, contract_path = _write_inputs(
        tmp_path,
        bracket2_leverage=5,
        baseline_max=800.0,
    )
    monkeypatch.setattr(exchange_module, "verify_risk_result", lambda payload: [])
    result = run_exchange_risk(risk_path, snapshot_path, contract_path)
    assert result["status"] == "BLOCK"
    assert "INITIAL_LEVERAGE_NOT_ALLOWED_FOR_NOTIONAL" in result["failure_reasons"]
    assert any(
        row["entry_bracket"] == 2 and row["initial_leverage_allowed"] is False
        for row in result["deployment_scenarios"]
    )


def test_exchange_snapshot_rejects_noncontinuous_cum(tmp_path: Path, monkeypatch):
    risk_path = tmp_path / "risk-result.json"
    risk_path.write_text(json.dumps(_risk_result()), encoding="utf-8")
    snapshot = _snapshot()
    snapshot["brackets"][1]["cum"] = 4.0
    snapshot_path = tmp_path / "exchange-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    contract_path = tmp_path / "exchange-risk.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "bad ladder",
                "source_risk_result_sha256": sha256_file(risk_path),
                "source_exchange_snapshot_sha256": sha256_file(snapshot_path),
                "baseline_capital_min_usdt": 100.0,
                "baseline_capital_max_usdt": 800.0,
                "isolated_wallet_haircut_pct": 0.0,
                "min_liquidation_headroom_over_required_budget_pct": 1.0,
                "min_parity_cases": 1,
                "max_parity_error_bps": 1.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(exchange_module, "verify_risk_result", lambda payload: [])
    with pytest.raises(ValueError, match="maintenance margin continuous"):
        run_exchange_risk(risk_path, snapshot_path, contract_path)


def test_exchange_risk_result_detects_snapshot_or_scenario_mutation(tmp_path: Path, monkeypatch):
    risk_path, snapshot_path, contract_path = _write_inputs(tmp_path)
    monkeypatch.setattr(exchange_module, "verify_risk_result", lambda payload: [])
    result = run_exchange_risk(risk_path, snapshot_path, contract_path)

    mutated = json.loads(json.dumps(result))
    mutated["snapshot"]["brackets"][0]["maint_margin_ratio"] = 0.006
    problems = verify_exchange_risk_result(mutated)
    assert any("snapshot fingerprint" in problem for problem in problems)

    mutated = json.loads(json.dumps(result))
    mutated["deployment_scenarios"][0]["liquidation_distance_pct"] = 99.0
    problems = verify_exchange_risk_result(mutated)
    assert any("deployment scenarios" in problem for problem in problems)
