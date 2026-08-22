from types import SimpleNamespace

import pytest

from parameter_extract.models import StrategySpec
from parameter_extract.study import collect_strategy_evidence


def _strategy() -> StrategySpec:
    return StrategySpec(
        symbol="BTCUSDT",
        rsi_period=14,
        rsi_entry=30.0,
        adx_min=10.0,
        adx_max=30.0,
        exit_mode="tp",
        tp_price_pct=1.0,
    )


def test_behavioral_evidence_forbids_holdout_entirely():
    context = SimpleNamespace(spec=SimpleNamespace(symbol="BTCUSDT"))
    with pytest.raises(ValueError, match="discovery and validation only"):
        collect_strategy_evidence(context, _strategy(), phases=("holdout",))


def test_behavioral_evidence_rejects_duplicate_phase_requests():
    context = SimpleNamespace(spec=SimpleNamespace(symbol="BTCUSDT"))
    with pytest.raises(ValueError, match="phases must be unique"):
        collect_strategy_evidence(
            context,
            _strategy(),
            phases=("discovery", "discovery"),
        )
