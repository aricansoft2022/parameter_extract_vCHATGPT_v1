import json
from pathlib import Path

import pytest

import parameter_extract.deployment as deployment_module
import parameter_extract.deployment_lineage as lineage_module
from parameter_extract.deployment import AUDITED_CCBOT_COMMIT, TARGET_CCBOT_REPOSITORY
from parameter_extract.deployment_lineage import run_lineage_deployment_export
from parameter_extract.manifest import sha256_file
from parameter_extract.models import StrategySpec
from parameter_extract.promotion import candidate_fingerprint
from parameter_extract.selection import _selection_set_fingerprint


def _selection() -> dict:
    strategy = StrategySpec(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=30.0,
        adx_min=18.0,
        adx_max=42.0,
        exit_mode="tp",
        tp_price_pct=0.65,
    )
    selected = [
        {
            "priority": 1,
            "original_priority": 1,
            "family_id": "family-0001",
            "candidate_fingerprint_sha256": candidate_fingerprint(strategy),
            "strategy": {
                "symbol": strategy.symbol,
                "rsi_period": strategy.rsi_period,
                "rsi_entry": strategy.rsi_entry,
                "adx_min": strategy.adx_min,
                "adx_max": strategy.adx_max,
                "exit_mode": strategy.exit_mode,
                "rsi_exit": strategy.rsi_exit,
                "tp_price_pct": strategy.tp_price_pct,
            },
        }
    ]
    portfolio_sha = "a" * 64
    return {
        "source_portfolio_result_sha256": portfolio_sha,
        "selected_set_fingerprint_sha256": _selection_set_fingerprint(
            source_portfolio_sha=portfolio_sha,
            slot_count=1,
            selected=selected,
        ),
        "symbol": "BTCUSDT",
        "slot_count": 1,
        "selected": selected,
    }


def _write_lineage(tmp_path: Path):
    selection = _selection()
    selection_path = tmp_path / "selection-result.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")

    risk = {
        "status": "RISK_BUDGET_PASS",
        "source_selection_result_sha256": sha256_file(selection_path),
        "source_selected_set_fingerprint_sha256": selection[
            "selected_set_fingerprint_sha256"
        ],
        "source_holdout_result_sha256": "b" * 64,
        "risk_fingerprint_sha256": "c" * 64,
        "study_fingerprint_sha256": "d" * 64,
        "dataset_fingerprint_sha256": "e" * 64,
        "execution": {"name": "expected_live"},
    }
    risk_path = tmp_path / "risk-result.json"
    risk_path.write_text(json.dumps(risk), encoding="utf-8")

    exchange = {
        "status": "EXCHANGE_RISK_PASS",
        "exchange_liquidation_validated": True,
        "teams_export_ready": True,
        "source_risk_result_sha256": sha256_file(risk_path),
        "source_selected_set_fingerprint_sha256": selection[
            "selected_set_fingerprint_sha256"
        ],
        "symbol": "BTCUSDT",
        "slot_count": 1,
        "allocation_pct": 100.0,
        "reserve_pct": 4.0,
        "provisional_deployment_leverage": 5,
        "exchange_snapshot_fingerprint_sha256": "f" * 64,
    }
    exchange_path = tmp_path / "exchange-risk-result.json"
    exchange_path.write_text(json.dumps(exchange), encoding="utf-8")

    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "lineage deployment",
                "source_selection_result_sha256": sha256_file(selection_path),
                "source_exchange_risk_result_sha256": sha256_file(exchange_path),
                "target_ccbot_repository": TARGET_CCBOT_REPOSITORY,
                "target_ccbot_commit_sha": AUDITED_CCBOT_COMMIT,
                "first_team_id": 1,
                "enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return selection_path, risk_path, exchange_path, deployment_path


def _patch_verifiers(monkeypatch):
    monkeypatch.setattr(lineage_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(lineage_module, "verify_risk_result", lambda payload: [])
    monkeypatch.setattr(lineage_module, "verify_exchange_risk_result", lambda payload: [])
    monkeypatch.setattr(deployment_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(deployment_module, "verify_exchange_risk_result", lambda payload: [])


def test_lineage_export_closes_selection_risk_exchange_chain(tmp_path: Path, monkeypatch):
    selection, risk, exchange, deployment = _write_lineage(tmp_path)
    _patch_verifiers(monkeypatch)
    csv_path = tmp_path / "teams.csv"
    manifest_path = tmp_path / "deployment-manifest.json"

    manifest = run_lineage_deployment_export(
        selection,
        risk,
        exchange,
        deployment,
        teams_csv_path=csv_path,
        manifest_path=manifest_path,
    )

    assert manifest["complete_artifact_lineage_checked"] is True
    assert manifest["source_risk_result_sha256"] == sha256_file(risk)
    assert manifest["source_selection_result_sha256"] == sha256_file(selection)
    assert manifest["source_exchange_risk_result_sha256"] == sha256_file(exchange)
    assert manifest["source_holdout_result_sha256"] == "b" * 64
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored == manifest


def test_lineage_export_rejects_exchange_bound_to_other_risk(tmp_path: Path, monkeypatch):
    selection, risk, exchange, deployment = _write_lineage(tmp_path)
    _patch_verifiers(monkeypatch)
    payload = json.loads(exchange.read_text(encoding="utf-8"))
    payload["source_risk_result_sha256"] = "0" * 64
    exchange.write_text(json.dumps(payload), encoding="utf-8")
    deploy = json.loads(deployment.read_text(encoding="utf-8"))
    deploy["source_exchange_risk_result_sha256"] = sha256_file(exchange)
    deployment.write_text(json.dumps(deploy), encoding="utf-8")

    with pytest.raises(ValueError, match="different risk-result"):
        run_lineage_deployment_export(
            selection,
            risk,
            exchange,
            deployment,
            teams_csv_path=tmp_path / "teams.csv",
            manifest_path=tmp_path / "deployment-manifest.json",
        )
