import csv
import io
import json
from pathlib import Path

import pytest

import parameter_extract.deployment as deployment_module
from parameter_extract.deployment import (
    AUDITED_CCBOT_COMMIT,
    TARGET_CCBOT_REPOSITORY,
    TEAM_CSV_FIELDS,
    run_deployment_export,
    verify_deployment_manifest,
)
from parameter_extract.manifest import sha256_file
from parameter_extract.models import StrategySpec
from parameter_extract.promotion import candidate_fingerprint
from parameter_extract.selection import _selection_set_fingerprint


def _strategy_tp(entry: float = 30.0) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=entry,
        adx_min=18.0,
        adx_max=42.0,
        exit_mode="tp",
        tp_price_pct=0.65,
    )


def _strategy_rsi(entry: float = 31.0) -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=15,
        rsi_entry=entry,
        adx_min=20.0,
        adx_max=45.0,
        exit_mode="rsi",
        rsi_exit=68.0,
    )


def _selection() -> dict:
    source_portfolio_sha = "a" * 64
    strategies = (_strategy_tp(), _strategy_rsi())
    selected = [
        {
            "priority": index + 1,
            "original_priority": index + 1,
            "family_id": f"family-{index + 1:04d}",
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
        for index, strategy in enumerate(strategies)
    ]
    selected_fp = _selection_set_fingerprint(
        source_portfolio_sha=source_portfolio_sha,
        slot_count=2,
        selected=selected,
    )
    return {
        "source_portfolio_result_sha256": source_portfolio_sha,
        "selected_set_fingerprint_sha256": selected_fp,
        "symbol": "BTCUSDT",
        "slot_count": 2,
        "selected": selected,
    }


def _exchange(selection: dict) -> dict:
    return {
        "status": "EXCHANGE_RISK_PASS",
        "exchange_liquidation_validated": True,
        "teams_export_ready": True,
        "source_selected_set_fingerprint_sha256": selection[
            "selected_set_fingerprint_sha256"
        ],
        "symbol": "BTCUSDT",
        "slot_count": 2,
        "allocation_pct": 50.0,
        "reserve_pct": 4.0,
        "provisional_deployment_leverage": 10,
        "exchange_snapshot_fingerprint_sha256": "b" * 64,
    }


def _write_inputs(tmp_path: Path, *, enabled: bool = False, first_team_id: int = 101):
    selection = _selection()
    selection_path = tmp_path / "selection-result.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    exchange = _exchange(selection)
    exchange_path = tmp_path / "exchange-risk-result.json"
    exchange_path.write_text(json.dumps(exchange), encoding="utf-8")
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "BTC deployment",
                "source_selection_result_sha256": sha256_file(selection_path),
                "source_exchange_risk_result_sha256": sha256_file(exchange_path),
                "target_ccbot_repository": TARGET_CCBOT_REPOSITORY,
                "target_ccbot_commit_sha": AUDITED_CCBOT_COMMIT,
                "first_team_id": first_team_id,
                "enabled": enabled,
            }
        ),
        encoding="utf-8",
    )
    return selection_path, exchange_path, deployment_path


def test_deployment_export_writes_exact_ccbot_contract(tmp_path: Path, monkeypatch):
    selection_path, exchange_path, deployment_path = _write_inputs(tmp_path)
    monkeypatch.setattr(deployment_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(deployment_module, "verify_exchange_risk_result", lambda payload: [])
    csv_path = tmp_path / "teams.csv"
    manifest_path = tmp_path / "deployment-manifest.json"

    manifest = run_deployment_export(
        selection_path,
        exchange_path,
        deployment_path,
        teams_csv_path=csv_path,
        manifest_path=manifest_path,
    )

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == TEAM_CSV_FIELDS
        rows = list(reader)
    assert [row["id"] for row in rows] == ["101", "102"]
    assert [row["priority"] for row in rows] == ["1", "2"]
    assert [row["enabled"] for row in rows] == ["false", "false"]
    assert {row["leverage"] for row in rows} == {"10"}
    assert rows[0]["exit_mode"] == "tp"
    assert rows[0]["rsi_exit"] == ""
    assert rows[0]["tp_price_pct"] == "0.65"
    assert rows[1]["exit_mode"] == "rsi"
    assert rows[1]["rsi_exit"] == "68.0"
    assert rows[1]["tp_price_pct"] == ""

    assert manifest["team_count"] == 2
    assert manifest["priority_source"] == "selection_compact_priority"
    assert manifest["exchange_liquidation_validated"] is True
    assert manifest["import_requires_ccbot_dry_run"] is True
    assert manifest["existing_live_team_id_collisions_checked"] is False
    assert verify_deployment_manifest(manifest, csv_bytes=csv_path.read_bytes()) == []
    json.dumps(manifest, allow_nan=False)


def test_deployment_export_refuses_nonpass_exchange_result(tmp_path: Path, monkeypatch):
    selection_path, exchange_path, deployment_path = _write_inputs(tmp_path)
    exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
    exchange["status"] = "BLOCK"
    exchange["exchange_liquidation_validated"] = False
    exchange["teams_export_ready"] = False
    exchange_path.write_text(json.dumps(exchange), encoding="utf-8")
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    deployment["source_exchange_risk_result_sha256"] = sha256_file(exchange_path)
    deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
    monkeypatch.setattr(deployment_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(deployment_module, "verify_exchange_risk_result", lambda payload: [])

    with pytest.raises(ValueError, match="EXCHANGE_RISK_PASS"):
        run_deployment_export(
            selection_path,
            exchange_path,
            deployment_path,
            teams_csv_path=tmp_path / "teams.csv",
            manifest_path=tmp_path / "deployment-manifest.json",
        )


def test_deployment_export_refuses_selected_set_drift(tmp_path: Path, monkeypatch):
    selection_path, exchange_path, deployment_path = _write_inputs(tmp_path)
    exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
    exchange["source_selected_set_fingerprint_sha256"] = "d" * 64
    exchange_path.write_text(json.dumps(exchange), encoding="utf-8")
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    deployment["source_exchange_risk_result_sha256"] = sha256_file(exchange_path)
    deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
    monkeypatch.setattr(deployment_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(deployment_module, "verify_exchange_risk_result", lambda payload: [])

    with pytest.raises(ValueError, match="different selected set"):
        run_deployment_export(
            selection_path,
            exchange_path,
            deployment_path,
            teams_csv_path=tmp_path / "teams.csv",
            manifest_path=tmp_path / "deployment-manifest.json",
        )


def test_deployment_manifest_detects_csv_or_row_mutation(tmp_path: Path, monkeypatch):
    selection_path, exchange_path, deployment_path = _write_inputs(tmp_path, enabled=True)
    monkeypatch.setattr(deployment_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(deployment_module, "verify_exchange_risk_result", lambda payload: [])
    csv_path = tmp_path / "teams.csv"
    manifest_path = tmp_path / "deployment-manifest.json"
    manifest = run_deployment_export(
        selection_path,
        exchange_path,
        deployment_path,
        teams_csv_path=csv_path,
        manifest_path=manifest_path,
    )

    bad_csv = csv_path.read_bytes().replace(b"true", b"false", 1)
    problems = verify_deployment_manifest(manifest, csv_bytes=bad_csv)
    assert any("supplied teams CSV bytes" in problem for problem in problems)

    mutated = json.loads(json.dumps(manifest))
    mutated["rows"][0]["rsi_entry"] = 33.0
    problems = verify_deployment_manifest(mutated)
    assert any("team rows do not match" in problem for problem in problems)
    assert any("manifest fingerprint" in problem for problem in problems)


def test_deployment_contract_rejects_unaudited_ccbot_commit(tmp_path: Path):
    selection_path, exchange_path, deployment_path = _write_inputs(tmp_path)
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    deployment["target_ccbot_commit_sha"] = "1" * 40
    deployment_path.write_text(json.dumps(deployment), encoding="utf-8")

    with pytest.raises(ValueError, match="not the ccbot commit audited"):
        run_deployment_export(
            selection_path,
            exchange_path,
            deployment_path,
            teams_csv_path=tmp_path / "teams.csv",
            manifest_path=tmp_path / "deployment-manifest.json",
        )
