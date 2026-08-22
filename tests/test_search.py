import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.search as search_module
from parameter_extract.models import ExecutionModel
from parameter_extract.search import load_search_json, run_search


def _search_payload(max_candidates: int = 20):
    return {
        "schema_version": 1,
        "name": "tiny search",
        "exit_modes": ["tp"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14],
            "rsi_entry": {"start": 30.0, "stop": 31.0, "step": 1.0},
            "adx_min": {"start": 10.0, "stop": 10.0, "step": 1.0},
            "adx_max": {"start": 20.0, "stop": 20.0, "step": 1.0},
            "tp_price_pct": {"start": 0.5, "stop": 1.0, "step": 0.5},
            "rsi_exit": None,
        },
        "gates": {
            "min_total_trades": 1,
            "min_positive_window_fraction": 0.0,
        },
        "refinement": {
            "enabled": False,
            "step_divisor": 2,
            "radius_steps": 1,
            "max_seeds": 2,
            "max_candidates": max_candidates,
        },
    }


def _study_spec():
    return SimpleNamespace(
        symbol="BTCUSDT",
        name="study",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
    )


def test_search_never_requests_validation_or_holdout(tmp_path: Path, monkeypatch):
    context = SimpleNamespace(spec=_study_spec())
    seen_phases = []

    def fake_evaluate(_context, strategy, *, phases, reveal_holdout=False):
        seen_phases.append((tuple(phases), reveal_holdout))
        score = strategy.rsi_entry + float(strategy.tp_price_pct or 0.0)
        metric = {
            "total_return_pct": score,
            "trade_count": 2,
            "worst_mae_pct": -1.0,
            "max_closed_equity_drawdown_pct": -0.5,
            "max_holding_minutes": 10.0,
            "open_at_end": False,
        }
        return {
            "phases_evaluated": ["discovery"],
            "holdout_revealed": False,
            "windows": [
                {"name": "d1", "metrics": metric},
                {"name": "d2", "metrics": {**metric, "total_return_pct": score - 0.25}},
            ],
        }

    monkeypatch.setattr(search_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(search_module, "evaluate_strategy", fake_evaluate)
    monkeypatch.setattr(search_module, "study_fingerprint", lambda _spec: "b" * 64)

    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    result = run_search("study.json", search_path, data_directory=tmp_path)

    assert result["phase_used"] == "discovery"
    assert result["validation_accessed"] is False
    assert result["holdout_accessed"] is False
    assert result["coarse_candidates"] == 4
    assert result["evaluated_candidates"] == 4
    assert result["pareto_candidates"] >= 1
    assert result["search_spec"]["gates"]["min_total_trades"] == 1
    assert result["execution"]["entry_timing"] == "next_open"
    assert seen_phases and all(item == (("discovery",), False) for item in seen_phases)


def test_search_refuses_a_grid_above_safety_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        search_module,
        "load_study_context",
        lambda *a, **k: SimpleNamespace(spec=_study_spec()),
    )
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload(max_candidates=3)), encoding="utf-8")
    with pytest.raises(ValueError, match="above max_candidates"):
        run_search("study.json", search_path, data_directory=tmp_path)


def test_pareto_does_not_collapse_risk_return_tradeoff_to_profit_only():
    high_return_high_risk = {
        "aggregate": {
            "worst_window_return_pct": 12.0,
            "median_window_return_pct": 18.0,
            "worst_mae_pct": -15.0,
            "max_drawdown_pct": -12.0,
        }
    }
    lower_return_low_risk = {
        "aggregate": {
            "worst_window_return_pct": 7.0,
            "median_window_return_pct": 10.0,
            "worst_mae_pct": -2.0,
            "max_drawdown_pct": -1.5,
        }
    }
    dominated = {
        "aggregate": {
            "worst_window_return_pct": 5.0,
            "median_window_return_pct": 7.0,
            "worst_mae_pct": -4.0,
            "max_drawdown_pct": -3.0,
        }
    }
    frontier = search_module._pareto_frontier(
        [high_return_high_risk, lower_return_low_risk, dominated]
    )
    assert high_return_high_risk in frontier
    assert lower_return_low_risk in frontier
    assert dominated not in frontier


def test_search_contract_rejects_duplicate_dimensions(tmp_path: Path):
    payload = _search_payload()
    payload["ranges"]["rsi_period"] = [14, 14]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicates"):
        load_search_json(path)
