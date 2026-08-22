from types import SimpleNamespace

import pytest

import parameter_extract.prepared_search as prepared_search_module
from parameter_extract.models import StrategySpec
from parameter_extract.prepared_search import _runtime_parity_check


def _strategy() -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=45.0,
        adx_min=0.0,
        adx_max=100.0,
        exit_mode="tp",
        tp_price_pct=1.0,
    )


def test_runtime_parity_gate_aborts_on_any_candidate_mismatch(monkeypatch):
    candidate = _strategy()
    context = SimpleNamespace()
    prepared = SimpleNamespace()

    monkeypatch.setattr(
        prepared_search_module,
        "_evaluate_candidate",
        lambda *args, **kwargs: {"stage": "coarse", "aggregate": {"x": 1}},
    )
    monkeypatch.setattr(
        prepared_search_module,
        "_evaluate_prepared_candidate",
        lambda *args, **kwargs: {"stage": "coarse", "aggregate": {"x": 2}},
    )

    with pytest.raises(RuntimeError, match="runtime parity failed"):
        _runtime_parity_check(
            context,
            prepared,
            [candidate],
            sample_size=3,
        )


def test_runtime_parity_gate_reports_checked_sample_count(monkeypatch):
    candidate = _strategy()
    context = SimpleNamespace()
    prepared = SimpleNamespace()
    row = {"stage": "coarse", "aggregate": {"x": 1}}

    monkeypatch.setattr(
        prepared_search_module,
        "_evaluate_candidate",
        lambda *args, **kwargs: row,
    )
    monkeypatch.setattr(
        prepared_search_module,
        "_evaluate_prepared_candidate",
        lambda *args, **kwargs: row,
    )

    assert (
        _runtime_parity_check(
            context,
            prepared,
            [candidate],
            sample_size=3,
        )
        == 1
    )
