import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.families as families_module
from parameter_extract.families import (
    _interval_jaccard,
    _parameter_distance,
    _tolerant_dice,
    run_family_clustering,
    verify_robustness_result,
)
from parameter_extract.manifest import sha256_file
from parameter_extract.models import ExecutionModel, StrategySpec
from parameter_extract.promotion import candidate_fingerprint
from parameter_extract.robustness import (
    NeighborhoodSteps,
    RobustnessGates,
    RobustnessSpec,
    robustness_fingerprint,
)


def _strategy(rsi_entry: float, adx_max: float, tp: float) -> dict:
    return {
        "symbol": "BTCUSDT",
        "rsi_period": 14,
        "rsi_entry": rsi_entry,
        "adx_min": 10.0,
        "adx_max": adx_max,
        "exit_mode": "tp",
        "rsi_exit": None,
        "tp_price_pct": tp,
    }


def _robust_center(strategy: dict, *, strength: float) -> dict:
    center_fp = candidate_fingerprint(strategy)
    neighbor_strategy = {**strategy, "rsi_entry": strategy["rsi_entry"] + 0.25}
    neighbor_fp = candidate_fingerprint(neighbor_strategy)
    return {
        "candidate_fingerprint_sha256": center_fp,
        "center_strategy": strategy,
        "center_parameters_retuned": False,
        "neighbor_strategies_promotable": False,
        "status": "ROBUST",
        "failure_reasons": [],
        "metrics": {
            "neighbor_count": 1,
            "validation_pass_fraction": strength,
            "discovery_stable_neighbor_fraction": strength,
            "center_validation_compounded_return_pct": 5.0 * strength,
            "median_neighbor_validation_compounded_return_pct": 4.5 * strength,
            "center_validation_advantage_pct": 0.5 * strength,
            "worst_neighbor_validation_return_pct": 2.0 * strength,
        },
        "neighbors": [
            {
                "axis": "rsi_entry",
                "direction": "plus",
                "diagnostic_only": True,
                "strategy": neighbor_strategy,
                "candidate_fingerprint_sha256": neighbor_fp,
                "discovery": {},
                "validation": {},
                "validation_gate_pass": True,
                "validation_gate_failures": [],
            }
        ],
    }


def _robustness_result() -> dict:
    steps = NeighborhoodSteps(
        include_rsi_period=True,
        rsi_entry=0.5,
        adx_min=1.0,
        adx_max=1.0,
        tp_price_pct=0.1,
        rsi_exit=0.5,
    )
    gates = RobustnessGates(
        min_neighbor_count=1,
        min_validation_pass_fraction=0.5,
        min_discovery_stable_neighbor_fraction=0.5,
        min_neighbor_discovery_positive_window_fraction=0.5,
        max_center_validation_advantage_pct=5.0,
    )
    spec = RobustnessSpec(
        name="robustness",
        source_validation_result_sha256="e" * 64,
        steps=steps,
        gates=gates,
        max_neighbor_evaluations=100,
    )
    centers = [
        _robust_center(_strategy(30.0, 30.0, 1.0), strength=0.95),
        _robust_center(_strategy(30.5, 31.0, 1.0), strength=0.85),
        _robust_center(_strategy(40.0, 50.0, 2.0), strength=0.90),
    ]
    return {
        "schema_version": 1,
        "kind": "parameter_extract.neighborhood_robustness",
        "robustness": spec.name,
        "robustness_fingerprint_sha256": robustness_fingerprint(spec),
        "robustness_spec": {
            "name": spec.name,
            "source_validation_result_sha256": spec.source_validation_result_sha256,
            "steps": {
                "include_rsi_period": steps.include_rsi_period,
                "rsi_entry": steps.rsi_entry,
                "adx_min": steps.adx_min,
                "adx_max": steps.adx_max,
                "tp_price_pct": steps.tp_price_pct,
                "rsi_exit": steps.rsi_exit,
            },
            "gates": {
                "min_neighbor_count": gates.min_neighbor_count,
                "min_validation_pass_fraction": gates.min_validation_pass_fraction,
                "min_discovery_stable_neighbor_fraction": (
                    gates.min_discovery_stable_neighbor_fraction
                ),
                "min_neighbor_discovery_positive_window_fraction": (
                    gates.min_neighbor_discovery_positive_window_fraction
                ),
                "max_center_validation_advantage_pct": (
                    gates.max_center_validation_advantage_pct
                ),
            },
            "max_neighbor_evaluations": spec.max_neighbor_evaluations,
        },
        "source_validation_result_sha256": "e" * 64,
        "study_fingerprint_sha256": "b" * 64,
        "dataset_fingerprint_sha256": "a" * 64,
        "symbol": "BTCUSDT",
        "execution": {
            "name": "expected_live",
            "entry_timing": "next_open",
            "taker_fee_bps": 4.0,
            "buy_slippage_bps": 2.0,
            "sell_slippage_bps": 2.0,
        },
        "parameters_retuned": False,
        "neighbor_strategies_promotable": False,
        "discovery_accessed": True,
        "validation_accessed": True,
        "holdout_accessed": False,
        "center_count": 3,
        "neighbor_evaluations": 3,
        "robust_count": 3,
        "fragile_count": 0,
        "centers": centers,
    }


def _family_contract(tmp_path: Path, robustness_path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "name": "families",
        "source_robustness_result_sha256": sha256_file(robustness_path),
        "thresholds": {
            "signal_tolerance_minutes": 1.0,
            "min_raw_signal_dice": 0.8,
            "min_accepted_signal_dice": 0.8,
            "min_exposure_jaccard": 0.8,
            "max_parameter_distance": 1.0,
        },
        "parameter_scales": {
            "rsi_period": 1.0,
            "rsi_entry": 1.0,
            "adx_min": 2.0,
            "adx_max": 2.0,
            "tp_price_pct": 0.25,
            "rsi_exit": 2.0,
        },
        "max_pair_evaluations": 20,
    }
    path = tmp_path / "families.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _context():
    return SimpleNamespace(
        spec=SimpleNamespace(
            name="study",
            symbol="BTCUSDT",
            dataset_fingerprint_sha256="a" * 64,
            execution=ExecutionModel.expected_live(),
        )
    )


def test_family_clustering_uses_behavior_and_keeps_existing_representative(
    tmp_path: Path, monkeypatch
):
    robustness_path = tmp_path / "robustness-result.json"
    robustness_path.write_text(json.dumps(_robustness_result()), encoding="utf-8")
    family_path = _family_contract(tmp_path, robustness_path)

    def fake_evidence(_context, strategy, *, phases):
        if strategy.rsi_entry < 35.0:
            offset = 0 if strategy.rsi_entry == 30.0 else 30_000
            windows = [
                {
                    "raw_signal_times_ms": [60_000 + offset, 180_000 + offset, 300_000 + offset],
                    "accepted_signal_times_ms": [60_000 + offset, 300_000 + offset],
                    "position_intervals_ms": [
                        [60_000 + offset, 120_000 + offset],
                        [300_000 + offset, 360_000 + offset],
                    ],
                }
            ]
        else:
            windows = [
                {
                    "raw_signal_times_ms": [1_000_000, 1_300_000],
                    "accepted_signal_times_ms": [1_000_000],
                    "position_intervals_ms": [[1_000_000, 1_200_000]],
                }
            ]
        return {
            "phases_evaluated": ["discovery", "validation"],
            "holdout_accessed": False,
            "windows": windows,
        }

    monkeypatch.setattr(families_module, "load_study_context", lambda *a, **k: _context())
    monkeypatch.setattr(families_module, "study_fingerprint", lambda _spec: "b" * 64)
    monkeypatch.setattr(families_module, "collect_strategy_evidence", fake_evidence)

    result = run_family_clustering(
        "study.json",
        robustness_path,
        family_path,
        data_directory=tmp_path,
    )
    assert result["parameters_retuned"] is False
    assert result["representatives_are_existing_robust_centers"] is True
    assert result["holdout_accessed"] is False
    assert result["robust_center_count"] == 3
    assert result["family_count"] == 2
    assert result["deduplicated_center_count"] == 1

    first_family = result["families"][0]
    assert first_family["member_count"] == 2
    expected_representative = candidate_fingerprint(_strategy(30.0, 30.0, 1.0))
    assert first_family["representative_candidate_fingerprint_sha256"] == expected_representative
    assert first_family["representative_strategy"] == _strategy(30.0, 30.0, 1.0)
    assert all(row["parameters_retuned"] is False for row in result["representatives"])

    matching_pair = next(row for row in result["pairwise"] if row["same_family"])
    assert matching_pair["raw_signal_dice"] == pytest.approx(1.0)
    assert matching_pair["accepted_signal_dice"] == pytest.approx(1.0)
    assert matching_pair["exposure_jaccard"] > 0.3


def test_similarity_primitives_are_bounded_and_interpretable():
    assert _tolerant_dice([0, 60_000], [30_000, 90_000], 30_000) == 1.0
    assert _tolerant_dice([0], [120_000], 30_000) == 0.0
    assert _interval_jaccard([(0, 100)], [(50, 150)]) == pytest.approx(1 / 3)
    scales = families_module.ParameterScales(1, 1, 1, 1, 0.5, 1)
    left = StrategySpec(**_strategy(30.0, 30.0, 1.0))
    right = StrategySpec(**_strategy(31.0, 30.0, 1.0))
    assert _parameter_distance(left, right, scales) > 0.0


def test_robustness_result_mutation_is_detected():
    payload = _robustness_result()
    assert verify_robustness_result(payload) == []
    payload["centers"][0]["center_strategy"]["rsi_entry"] = 31.25
    problems = verify_robustness_result(payload)
    assert any("strategy fingerprint mismatch" in problem for problem in problems)


def test_family_contract_is_pinned_to_exact_robustness_file(tmp_path: Path, monkeypatch):
    robustness_path = tmp_path / "robustness-result.json"
    robustness_path.write_text(json.dumps(_robustness_result()), encoding="utf-8")
    family_path = _family_contract(tmp_path, robustness_path)
    robustness_path.write_text(
        json.dumps({**_robustness_result(), "robustness": "changed"}), encoding="utf-8"
    )
    monkeypatch.setattr(families_module, "load_study_context", lambda *a, **k: _context())
    with pytest.raises(ValueError, match="different robustness-result file"):
        run_family_clustering(
            "study.json",
            robustness_path,
            family_path,
            data_directory=tmp_path,
        )
