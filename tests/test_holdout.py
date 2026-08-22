import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.holdout as holdout_module
from parameter_extract.holdout import run_holdout, verify_holdout_result
from parameter_extract.manifest import sha256_file
from parameter_extract.models import ExecutionModel
from parameter_extract.promotion import candidate_fingerprint
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


def _selection_result() -> dict:
    strategy_a = _strategy(30.0)
    strategy_b = _strategy(31.0)
    source_portfolio_sha = "d" * 64
    selected = [
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
    selected_fp = _selection_set_fingerprint(
        source_portfolio_sha=source_portfolio_sha,
        slot_count=2,
        selected=selected,
    )
    return {
        "study_fingerprint_sha256": "b" * 64,
        "dataset_fingerprint_sha256": "a" * 64,
        "symbol": "BTCUSDT",
        "execution": asdict(ExecutionModel.expected_live()),
        "source_portfolio_result_sha256": source_portfolio_sha,
        "slot_count": 2,
        "selected_set_fingerprint_sha256": selected_fp,
        "selected": selected,
    }


def _window(name: str, return_pct: float, *, trades: int, drawdown: float) -> dict:
    return {
        "phase": "holdout",
        "name": name,
        "start_ms": 1_000,
        "end_ms": 61_000,
        "slot_count": 2,
        "raw_signal_count": trades + 2,
        "accepted_entry_count": trades,
        "blocked_no_slot_count": 1,
        "skipped_team_active_count": 0,
        "cancelled_on_gap_count": 0,
        "closed_trade_count": trades,
        "closed_trade_net_return_sum_pct": return_pct * 2,
        "fixed_baseline_portfolio_return_pct": return_pct,
        "max_fixed_baseline_closed_drawdown_pct": drawdown,
        "slot_utilization_pct": 50.0,
        "open_position_count": 0,
        "pending_entry_count": 0,
        "candidates": [],
        "trades": [],
        "open_positions": [],
        "pending_entries": [],
    }


def _write_contracts(tmp_path: Path) -> tuple[Path, Path]:
    selection = _selection_result()
    selection_path = tmp_path / "selection-result.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    holdout_path = tmp_path / "holdout.json"
    holdout_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "sealed-final",
                "source_selection_result_sha256": sha256_file(selection_path),
                "source_selected_set_fingerprint_sha256": selection[
                    "selected_set_fingerprint_sha256"
                ],
                "gates": {
                    "min_total_closed_trades": 3,
                    "min_positive_window_fraction": 0.5,
                    "min_fixed_baseline_total_return_pct": 0.0,
                    "min_median_window_return_pct": 0.0,
                    "min_worst_window_return_pct": -1.0,
                    "min_worst_within_window_closed_drawdown_pct": -5.0,
                    "max_open_at_end_windows": 0,
                    "max_pending_at_end_windows": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    return selection_path, holdout_path


def _context():
    return SimpleNamespace(
        spec=SimpleNamespace(
            symbol="BTCUSDT",
            dataset_fingerprint_sha256="a" * 64,
            execution=ExecutionModel.expected_live(),
            holdout=(SimpleNamespace(name="h1"), SimpleNamespace(name="h2")),
        )
    )


def test_holdout_uses_only_sealed_windows_and_frozen_selected_order(tmp_path: Path, monkeypatch):
    selection_path, holdout_path = _write_contracts(tmp_path)
    calls = []

    def fake_run(phase, window, _context, candidates, *, slot_count):
        calls.append((phase, window.name, tuple(row.family_id for row in candidates), slot_count))
        return (
            _window(window.name, 2.0, trades=2, drawdown=-1.0)
            if window.name == "h1"
            else _window(window.name, 1.0, trades=2, drawdown=-0.5)
        )

    monkeypatch.setattr(holdout_module, "load_study_context", lambda *a, **k: _context())
    monkeypatch.setattr(holdout_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(holdout_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(holdout_module, "_run_portfolio_window", fake_run)

    result = run_holdout(
        "study.json",
        selection_path,
        holdout_path,
        data_directory=tmp_path,
    )
    assert result["status"] == "PASS"
    assert result["holdout_accessed"] is True
    assert result["evaluator_discovery_accessed"] is False
    assert result["evaluator_validation_accessed"] is False
    assert result["strategy_parameters_retuned"] is False
    assert result["selection_gates_retuned"] is False
    assert result["selected_set_changed"] is False
    assert result["slot_count_changed"] is False
    assert result["priority_reoptimized"] is False
    assert [row["family_id"] for row in result["selected"]] == ["F0001", "F0002"]
    assert [row["original_priority"] for row in result["selected"]] == [1, 3]
    assert calls == [
        ("holdout", "h1", ("F0001", "F0002"), 2),
        ("holdout", "h2", ("F0001", "F0002"), 2),
    ]
    json.dumps(result, allow_nan=False)
    assert verify_holdout_result(result) == []


def test_holdout_contract_pins_exact_selection_file_and_selected_set(tmp_path: Path, monkeypatch):
    selection_path, holdout_path = _write_contracts(tmp_path)
    monkeypatch.setattr(holdout_module, "load_study_context", lambda *a, **k: _context())

    changed = _selection_result()
    changed["slot_count"] = 1
    selection_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="different selection-result file"):
        run_holdout("study.json", selection_path, holdout_path, data_directory=tmp_path)

    selection_path.write_text(json.dumps(_selection_result()), encoding="utf-8")
    holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
    holdout["source_selection_result_sha256"] = sha256_file(selection_path)
    holdout["source_selected_set_fingerprint_sha256"] = "e" * 64
    holdout_path.write_text(json.dumps(holdout), encoding="utf-8")
    monkeypatch.setattr(holdout_module, "verify_selection_result", lambda payload: [])
    with pytest.raises(ValueError, match="different selected-set fingerprint"):
        run_holdout("study.json", selection_path, holdout_path, data_directory=tmp_path)


def test_holdout_result_detects_selected_set_and_phase_mutation(tmp_path: Path, monkeypatch):
    selection_path, holdout_path = _write_contracts(tmp_path)
    monkeypatch.setattr(holdout_module, "load_study_context", lambda *a, **k: _context())
    monkeypatch.setattr(holdout_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(holdout_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(
        holdout_module,
        "_run_portfolio_window",
        lambda phase, window, context, candidates, *, slot_count: _window(
            window.name, 1.0, trades=2, drawdown=-1.0
        ),
    )
    result = run_holdout(
        "study.json", selection_path, holdout_path, data_directory=tmp_path
    )

    mutated = json.loads(json.dumps(result))
    mutated["selected"][0]["strategy"]["rsi_entry"] = 29.0
    problems = verify_holdout_result(mutated)
    assert any("strategy fingerprint mismatch" in p for p in problems)
    assert any("selected-set fingerprint" in p for p in problems)

    mutated = json.loads(json.dumps(result))
    mutated["selected"][1]["original_priority"] = 0
    problems = verify_holdout_result(mutated)
    assert any("original priority" in p for p in problems)

    mutated = json.loads(json.dumps(result))
    mutated["windows"][0]["phase"] = "validation"
    assert any("non-holdout" in p for p in verify_holdout_result(mutated))


def test_holdout_fail_is_final_evaluation_not_retuning_signal(tmp_path: Path, monkeypatch):
    selection_path, holdout_path = _write_contracts(tmp_path)
    monkeypatch.setattr(holdout_module, "load_study_context", lambda *a, **k: _context())
    monkeypatch.setattr(holdout_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(holdout_module, "verify_selection_result", lambda payload: [])
    monkeypatch.setattr(
        holdout_module,
        "_run_portfolio_window",
        lambda phase, window, context, candidates, *, slot_count: _window(
            window.name, -2.0, trades=1, drawdown=-6.0
        ),
    )
    result = run_holdout(
        "study.json", selection_path, holdout_path, data_directory=tmp_path
    )
    assert result["status"] == "FAIL"
    assert "TOTAL_RETURN" in result["failure_reasons"]
    assert "WORST_WITHIN_WINDOW_DRAWDOWN" in result["failure_reasons"]
    assert result["strategy_parameters_retuned"] is False
    assert result["selection_gates_retuned"] is False


def test_holdout_requires_actual_holdout_windows(tmp_path: Path, monkeypatch):
    selection_path, holdout_path = _write_contracts(tmp_path)
    context = _context()
    context.spec.holdout = ()
    monkeypatch.setattr(holdout_module, "load_study_context", lambda *a, **k: context)
    with pytest.raises(ValueError, match="no sealed holdout"):
        run_holdout("study.json", selection_path, holdout_path, data_directory=tmp_path)
