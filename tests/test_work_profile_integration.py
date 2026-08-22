import json
import math
from pathlib import Path

import parameter_extract.bulk_search as bulk_search_module
import parameter_extract.query_search as query_search_module
from parameter_extract.bulk_search import run_bulk_search
from parameter_extract.models import Candle, ExecutionModel, FundingEvent
from parameter_extract.query_search import run_query_search
from parameter_extract.study import StudyContext, StudySpec, WindowSpec


def _context() -> StudyContext:
    base = 1_760_000_000_000
    candles: list[Candle] = []
    for index in range(540):
        open_time = base + index * 60_000
        close = 100.0 + 5.0 * math.sin(index / 4.7) + 0.008 * index
        open_value = 100.0 + 5.0 * math.sin((index - 0.5) / 4.7) + 0.008 * (index - 0.5)
        spread = 0.45 + 0.08 * (1.0 + math.sin(index / 8.0))
        candles.append(
            Candle(
                open_time_ms=open_time,
                close_time_ms=open_time + 59_999,
                open=open_value,
                high=max(open_value, close) + spread,
                low=min(open_value, close) - spread,
                close=close,
                volume=100.0 + index,
            )
        )
    spec = StudySpec(
        name="work profile integration",
        symbol="BTCUSDT",
        dataset_manifest="unused.json",
        dataset_fingerprint_sha256="a" * 64,
        execution=ExecutionModel.expected_live(),
        discovery=(WindowSpec("d", candles[300].open_time_ms, candles[400].open_time_ms),),
        validation=(WindowSpec("v", candles[400].open_time_ms, candles[470].open_time_ms),),
        holdout=(WindowSpec("h", candles[470].open_time_ms, candles[530].open_time_ms),),
        warmup_candles=300,
        min_trades=1,
    )
    funding = (
        FundingEvent(
            timestamp_ms=candles[350].open_time_ms + 30_000,
            rate=0.0001,
            mark_price=candles[350].close,
        ),
    )
    return StudyContext(spec=spec, candles=tuple(candles), funding=funding)


def _search_payload() -> dict:
    return {
        "schema_version": 1,
        "name": "profile fixture",
        "exit_modes": ["tp", "rsi"],
        "min_adx_width": 4.0,
        "ranges": {
            "rsi_period": [14],
            "rsi_entry": {"start": 30.0, "stop": 60.0, "step": 15.0},
            "adx_min": {"start": 0.0, "stop": 10.0, "step": 10.0},
            "adx_max": {"start": 90.0, "stop": 100.0, "step": 10.0},
            "tp_price_pct": {"start": 0.2, "stop": 0.4, "step": 0.2},
            "rsi_exit": {"start": 55.0, "stop": 65.0, "step": 10.0},
        },
        "gates": {
            "min_total_trades": 1,
            "min_positive_window_fraction": 0.0,
        },
        "refinement": {
            "enabled": False,
            "step_divisor": 2,
            "radius_steps": 1,
            "max_seeds": 1,
            "max_candidates": 500,
        },
    }


def test_query_and_bulk_work_profiles_cover_only_real_candidate_evaluations(
    tmp_path: Path,
    monkeypatch,
):
    context = _context()
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_search_payload()), encoding="utf-8")
    monkeypatch.setattr(query_search_module, "load_study_context", lambda *a, **k: context)
    monkeypatch.setattr(bulk_search_module, "load_study_context", lambda *a, **k: context)

    query = run_query_search("study.json", search_path, data_directory=tmp_path)
    bulk = run_bulk_search("study.json", search_path, data_directory=tmp_path)

    assert query["query_work_profile"] == bulk["query_work_profile"]
    profile = query["query_work_profile"]
    assert profile["candidate_evaluations"] == query["evaluated_candidates"]
    assert profile["candidate_window_replays"] == query["evaluated_candidates"]
    assert profile["accepted_positions"] == (
        profile["closed_trades"] + profile["open_positions"]
    )
    assert profile["exit_lookup_requests"] == profile["accepted_positions"]
    assert profile["excursion_range_requests"] == profile["accepted_positions"]
    assert profile["closed_trade_signal_bisects"] == profile["closed_trades"]
    assert 0 <= profile["funding_range_bisects"] <= 2 * profile["accepted_positions"]
    assert 0 <= profile["funding_event_checks"] < profile["accepted_positions"]
    assert query["runtime_parity_passed"] is True
    assert bulk["runtime_parity_passed"] is True
