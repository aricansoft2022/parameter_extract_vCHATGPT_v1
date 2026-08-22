import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.selection as selection_module
from parameter_extract.manifest import sha256_file
from parameter_extract.models import ExecutionModel, StrategySpec
from parameter_extract.portfolio import _Candidate
from parameter_extract.promotion import candidate_fingerprint
from parameter_extract.selection import _marginal_evidence, run_selection


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


def _aggregate(return_pct: float, *, drawdown: float, blocked: int) -> dict:
    return {
        "fixed_baseline_total_return_pct": return_pct,
        "worst_within_window_closed_drawdown_pct": drawdown,
        "blocked_no_slot_count": blocked,
    }


def _full_windows() -> list[dict]:
    candidate_rows = [
        {
            "family_id": "F0001",
            "accepted_entry_count": 3,
            "blocked_no_slot_count": 0,
            "raw_signal_count": 4,
        },
        {
            "family_id": "F0002",
            "accepted_entry_count": 2,
            "blocked_no_slot_count": 1,
            "raw_signal_count": 5,
        },
    ]
    return [
        {"tag": "full", "phase": "discovery", "candidates": candidate_rows},
        {"tag": "full", "phase": "validation", "candidates": candidate_rows},
    ]


def _aggregates_for(tag: str) -> dict[str, dict]:
    if tag == "full":
        return {
            "discovery": _aggregate(10.0, drawdown=-2.0, blocked=1),
            "validation": _aggregate(8.0, drawdown=-3.0, blocked=1),
        }
    if tag == "without_a":
        return {
            "discovery": _aggregate(6.0, drawdown=-1.0, blocked=0),
            "validation": _aggregate(3.0, drawdown=-2.0, blocked=0),
        }
    if tag == "without_b":
        return {
            "discovery": _aggregate(11.0, drawdown=-2.0, blocked=0),
            "validation": _aggregate(9.0, drawdown=-3.0, blocked=0),
        }
    raise AssertionError(tag)


def test_selection_is_one_pass_leave_one_out_and_preserves_priority(tmp_path: Path, monkeypatch):
    strategy_a = _strategy(30.0)
    strategy_b = _strategy(31.0)
    fp_a = candidate_fingerprint(strategy_a)
    fp_b = candidate_fingerprint(strategy_b)
    families = {
        "representatives": [
            {"family_id": "F0001", "candidate_fingerprint_sha256": fp_a, "strategy": strategy_a},
            {"family_id": "F0002", "candidate_fingerprint_sha256": fp_b, "strategy": strategy_b},
        ]
    }
    families_path = tmp_path / "families-result.json"
    families_path.write_text(json.dumps(families), encoding="utf-8")
    family_sha = sha256_file(families_path)

    execution = ExecutionModel.expected_live()
    full_windows = _full_windows()
    full_aggregates = _aggregates_for("full")
    portfolio = {
        "source_families_result_sha256": family_sha,
        "study_fingerprint_sha256": "b" * 64,
        "dataset_fingerprint_sha256": "a" * 64,
        "symbol": "BTCUSDT",
        "execution": asdict(execution),
        "portfolio_spec": {
            "name": "portfolio",
            "source_families_result_sha256": family_sha,
            "slot_count": 1,
            "priority_family_ids": ["F0001", "F0002"],
        },
        "windows": full_windows,
        "phase_aggregates": full_aggregates,
    }
    portfolio_path = tmp_path / "portfolio-result.json"
    portfolio_path.write_text(json.dumps(portfolio), encoding="utf-8")

    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "selection",
                "source_portfolio_result_sha256": sha256_file(portfolio_path),
                "gates": {
                    "min_discovery_marginal_return_pct": 0.0,
                    "min_validation_marginal_return_pct": 0.0,
                    "min_validation_accepted_entries": 1,
                    "max_validation_drawdown_worsening_pct": 1.5,
                    "max_validation_contention_added_fraction": 0.25,
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
            execution=execution,
        )
    )
    monkeypatch.setattr(selection_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(selection_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(selection_module, "verify_families_result", lambda payload: [])
    monkeypatch.setattr(selection_module, "verify_portfolio_result", lambda payload: [])

    calls: list[tuple[str, ...]] = []

    def fake_replay(_context, candidates, *, slot_count):
        ids = tuple(row.family_id for row in candidates)
        calls.append(ids)
        if ids == ("F0001", "F0002"):
            return full_windows
        if ids == ("F0002",):
            return [{"tag": "without_a", "phase": "discovery"}]
        if ids == ("F0001",):
            return [{"tag": "without_b", "phase": "discovery"}]
        raise AssertionError(ids)

    def fake_phase_aggregates(windows, *, slot_count):
        return _aggregates_for(windows[0]["tag"])

    monkeypatch.setattr(selection_module, "_replay_candidate_set", fake_replay)
    monkeypatch.setattr(selection_module, "_phase_aggregates", fake_phase_aggregates)

    result = run_selection(
        "study.json",
        families_path,
        portfolio_path,
        selection_path,
        data_directory=tmp_path,
    )

    statuses = {row["family_id"]: row["status"] for row in result["marginal_evidence"]}
    assert statuses == {"F0001": "KEEP", "F0002": "DROP"}
    assert result["selected_count"] == 1
    assert result["dropped_count"] == 1
    assert result["selected"][0]["family_id"] == "F0001"
    assert result["selected"][0]["original_priority"] == 1
    assert result["selected"][0]["priority"] == 1
    assert result["priority_reoptimized"] is False
    assert result["iterative_subset_search"] is False
    assert result["holdout_accessed"] is False
    # One full replay, one leave-one-out per candidate, one final selected-set replay.
    assert calls == [
        ("F0001", "F0002"),
        ("F0002",),
        ("F0001",),
        ("F0001",),
    ]


def test_contention_metric_excludes_candidates_own_blocked_signals():
    strategy_a = StrategySpec(**_strategy(30.0))
    candidate = _Candidate("F0001", candidate_fingerprint(strategy_a), strategy_a, 1)
    full_windows = [
        {
            "phase": "validation",
            "candidates": [
                {
                    "family_id": "F0001",
                    "accepted_entry_count": 1,
                    "blocked_no_slot_count": 100,
                    "raw_signal_count": 101,
                },
                {
                    "family_id": "F0002",
                    "accepted_entry_count": 2,
                    "blocked_no_slot_count": 0,
                    "raw_signal_count": 10,
                },
            ],
        }
    ]
    full = {
        "discovery": _aggregate(1.0, drawdown=-1.0, blocked=100),
        "validation": _aggregate(1.0, drawdown=-1.0, blocked=100),
    }
    leaveout = {
        "discovery": _aggregate(0.0, drawdown=-1.0, blocked=0),
        "validation": _aggregate(0.0, drawdown=-1.0, blocked=0),
    }
    evidence = _marginal_evidence(candidate, full_windows, full, leaveout)
    assert evidence["validation_candidate_blocked_no_slot_count"] == 100
    assert evidence["validation_other_family_blocked_with_candidate_count"] == 0
    assert evidence["validation_contention_added_count"] == 0
    assert evidence["validation_contention_added_fraction"] == 0.0
