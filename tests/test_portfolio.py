import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import parameter_extract.portfolio as portfolio_module
from parameter_extract.families import (
    FamilySpec,
    FamilyThresholds,
    ParameterScales,
    family_fingerprint,
)
from parameter_extract.indicators import IndicatorPoint
from parameter_extract.models import Candle, ExecutionModel, StrategySpec
from parameter_extract.portfolio import (
    _Candidate,
    _run_portfolio_window,
    run_portfolio,
    verify_families_result,
)
from parameter_extract.promotion import candidate_fingerprint
from parameter_extract.signals import Signal
from parameter_extract.study import WindowSpec


def _strategy(entry: float = 30.0) -> dict:
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


def _candle(index: int, *, high: float = 100.5) -> Candle:
    open_time = index * 60_000
    return Candle(
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=100.0,
        high=high,
        low=99.5,
        close=100.0,
        volume=1.0,
    )


def _signal(index: int) -> Signal:
    point = IndicatorPoint(rsi=31.0, adx=20.0, adr=2.0)
    return Signal(
        candle_index=index,
        timestamp_ms=index * 60_000 + 59_999,
        reference_price=100.0,
        previous=point,
        current=point,
    )


def test_pending_entry_reserves_slot_and_blocked_signal_is_not_queued(monkeypatch):
    strategy_a = StrategySpec(**_strategy(30.0))
    strategy_b = StrategySpec(**_strategy(31.0))
    candidates = (
        _Candidate("F0001", candidate_fingerprint(strategy_a), strategy_a, 1),
        _Candidate("F0002", candidate_fingerprint(strategy_b), strategy_b, 2),
    )
    candles = [_candle(0), _candle(1, high=102.0), _candle(2, high=102.0)]
    signals = {
        "F0001": (_signal(0),),
        "F0002": (_signal(0), _signal(1)),
    }
    points = {"F0001": [None] * 3, "F0002": [None] * 3}

    def fake_prepare(window, context, found_candidates):
        assert found_candidates == candidates
        return candles, (), signals, points

    monkeypatch.setattr(portfolio_module, "_prepare_window", fake_prepare)
    context = SimpleNamespace(
        spec=SimpleNamespace(execution=ExecutionModel.expected_live())
    )
    result = _run_portfolio_window(
        "validation",
        WindowSpec("v1", 0, 180_000),
        context,
        candidates,
        slot_count=1,
    )

    rows = {row["family_id"]: row for row in result["candidates"]}
    assert rows["F0001"]["accepted_entry_count"] == 1
    assert rows["F0002"]["blocked_no_slot_count"] == 1
    assert rows["F0002"]["accepted_entry_count"] == 1
    assert result["accepted_entry_count"] == 2
    assert result["blocked_no_slot_count"] == 1
    assert result["closed_trade_count"] == 2
    assert [trade["family_id"] for trade in result["trades"]] == ["F0001", "F0002"]
    assert result["slot_utilization_pct"] == pytest.approx(2 / 3 * 100.0)


def _families_result() -> dict:
    thresholds = FamilyThresholds(
        signal_tolerance_minutes=1.0,
        min_raw_signal_dice=0.8,
        min_accepted_signal_dice=0.8,
        min_exposure_jaccard=0.8,
        max_parameter_distance=1.0,
    )
    scales = ParameterScales(
        rsi_period=1.0,
        rsi_entry=1.0,
        adx_min=2.0,
        adx_max=2.0,
        tp_price_pct=0.25,
        rsi_exit=2.0,
    )
    spec = FamilySpec(
        name="families",
        source_robustness_result_sha256="e" * 64,
        thresholds=thresholds,
        parameter_scales=scales,
        max_pair_evaluations=10,
    )
    strong = _strategy(30.0)
    weak = _strategy(30.5)
    strong_fp = candidate_fingerprint(strong)
    weak_fp = candidate_fingerprint(weak)
    strong_metrics = {
        "validation_pass_fraction": 0.95,
        "discovery_stable_neighbor_fraction": 0.95,
        "worst_neighbor_validation_return_pct": 2.0,
        "center_validation_advantage_pct": 0.2,
        "center_validation_compounded_return_pct": 4.0,
    }
    weak_metrics = {
        "validation_pass_fraction": 0.80,
        "discovery_stable_neighbor_fraction": 0.80,
        "worst_neighbor_validation_return_pct": 1.0,
        "center_validation_advantage_pct": 0.4,
        "center_validation_compounded_return_pct": 3.0,
    }
    return {
        "schema_version": 1,
        "kind": "parameter_extract.strategy_families",
        "family": spec.name,
        "family_fingerprint_sha256": family_fingerprint(spec),
        "family_spec": {
            "name": spec.name,
            "source_robustness_result_sha256": spec.source_robustness_result_sha256,
            "thresholds": {
                "signal_tolerance_minutes": thresholds.signal_tolerance_minutes,
                "min_raw_signal_dice": thresholds.min_raw_signal_dice,
                "min_accepted_signal_dice": thresholds.min_accepted_signal_dice,
                "min_exposure_jaccard": thresholds.min_exposure_jaccard,
                "max_parameter_distance": thresholds.max_parameter_distance,
            },
            "parameter_scales": {
                "rsi_period": scales.rsi_period,
                "rsi_entry": scales.rsi_entry,
                "adx_min": scales.adx_min,
                "adx_max": scales.adx_max,
                "tp_price_pct": scales.tp_price_pct,
                "rsi_exit": scales.rsi_exit,
            },
            "max_pair_evaluations": spec.max_pair_evaluations,
        },
        "representative_policy": "robustness_stability_v1",
        "source_robustness_result_sha256": "e" * 64,
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
        "representatives_are_existing_robust_centers": True,
        "discovery_accessed": True,
        "validation_accessed": True,
        "holdout_accessed": False,
        "robust_center_count": 2,
        "pair_evaluations": 1,
        "family_count": 1,
        "deduplicated_center_count": 1,
        "families": [
            {
                "family_id": "F0001",
                "representative_candidate_fingerprint_sha256": strong_fp,
                "representative_strategy": strong,
                "representative_selection": "robustness_stability_v1",
                "member_count": 2,
                "members": [
                    {
                        "candidate_fingerprint_sha256": strong_fp,
                        "strategy": strong,
                        "robustness_metrics": strong_metrics,
                    },
                    {
                        "candidate_fingerprint_sha256": weak_fp,
                        "strategy": weak,
                        "robustness_metrics": weak_metrics,
                    },
                ],
                "within_family_pairs": [],
            }
        ],
        "representatives": [
            {
                "family_id": "F0001",
                "candidate_fingerprint_sha256": strong_fp,
                "strategy": strong,
                "parameters_retuned": False,
            }
        ],
        "pairwise": [
            {
                "left": strong_fp,
                "right": weak_fp,
                "same_exit_mode": True,
                "raw_signal_dice": 0.9,
                "accepted_signal_dice": 0.9,
                "exposure_jaccard": 0.9,
                "parameter_distance": 0.2,
                "parameter_distance_similarity": 0.8,
                "family_score": 0.875,
                "same_family": True,
            }
        ],
    }


def test_family_verifier_recomputes_representative_policy():
    payload = _families_result()
    assert verify_families_result(payload) == []
    weak = payload["families"][0]["members"][1]
    payload["families"][0]["representative_candidate_fingerprint_sha256"] = weak[
        "candidate_fingerprint_sha256"
    ]
    payload["families"][0]["representative_strategy"] = weak["strategy"]
    payload["representatives"][0]["candidate_fingerprint_sha256"] = weak[
        "candidate_fingerprint_sha256"
    ]
    payload["representatives"][0]["strategy"] = weak["strategy"]
    problems = verify_families_result(payload)
    assert any("deterministic policy" in problem for problem in problems)


def test_portfolio_requires_explicit_priority_permutation(tmp_path: Path, monkeypatch):
    families_path = tmp_path / "families-result.json"
    payload = _families_result()
    families_path.write_text(json.dumps(payload), encoding="utf-8")
    portfolio_path = tmp_path / "portfolio.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "portfolio",
                "source_families_result_sha256": portfolio_module.sha256_file(families_path),
                "slot_count": 1,
                "priority_family_ids": ["F9999"],
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
            discovery=(),
            validation=(),
        )
    )
    monkeypatch.setattr(portfolio_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(portfolio_module, "study_fingerprint", lambda _spec: "b" * 64)
    with pytest.raises(ValueError, match="must contain every representative"):
        run_portfolio(
            "study.json",
            families_path,
            portfolio_path,
            data_directory=tmp_path,
        )
