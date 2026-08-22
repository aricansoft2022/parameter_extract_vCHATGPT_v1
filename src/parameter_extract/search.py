from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .models import StrategySpec
from .study import evaluate_strategy, load_study_context, study_fingerprint

SEARCH_SCHEMA_VERSION = 1
PARETO_OBJECTIVES = (
    "worst_window_return_pct:max",
    "median_window_return_pct:max",
    "worst_mae_pct:max",
    "max_drawdown_pct:max",
)


@dataclass(frozen=True, slots=True)
class NumericRange:
    start: float
    stop: float
    step: float

    def __post_init__(self) -> None:
        if self.step <= 0.0:
            raise ValueError("range step must be positive")
        if self.stop < self.start:
            raise ValueError("range stop must be >= start")


@dataclass(frozen=True, slots=True)
class SearchGates:
    min_total_trades: int
    min_positive_window_fraction: float
    min_worst_window_return_pct: float | None = None
    min_worst_mae_pct: float | None = None

    def __post_init__(self) -> None:
        if self.min_total_trades < 1:
            raise ValueError("min_total_trades must be positive")
        if not 0.0 <= self.min_positive_window_fraction <= 1.0:
            raise ValueError("min_positive_window_fraction must be inside [0, 1]")


@dataclass(frozen=True, slots=True)
class RefinementSpec:
    enabled: bool = True
    step_divisor: int = 2
    radius_steps: int = 1
    max_seeds: int = 20
    max_candidates: int = 50_000

    def __post_init__(self) -> None:
        if self.step_divisor < 2:
            raise ValueError("refinement step_divisor must be at least 2")
        if self.radius_steps < 1 or self.max_seeds < 1 or self.max_candidates < 1:
            raise ValueError("refinement limits must be positive")


@dataclass(frozen=True, slots=True)
class SearchSpec:
    name: str
    rsi_periods: tuple[int, ...]
    rsi_entry: NumericRange
    adx_min: NumericRange
    adx_max: NumericRange
    exit_modes: tuple[str, ...]
    tp_price_pct: NumericRange | None
    rsi_exit: NumericRange | None
    min_adx_width: float
    gates: SearchGates
    refinement: RefinementSpec

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("search name cannot be empty")
        if not self.rsi_periods:
            raise ValueError("search requires at least one RSI period")
        if len(set(self.rsi_periods)) != len(self.rsi_periods):
            raise ValueError("rsi_period list contains duplicates")
        if any(period not in {14, 15, 16, 17, 18, 19} for period in self.rsi_periods):
            raise ValueError("RSI periods must be between 14 and 19")
        if not self.exit_modes or set(self.exit_modes) - {"tp", "rsi"}:
            raise ValueError("exit_modes may contain only tp and rsi")
        if len(set(self.exit_modes)) != len(self.exit_modes):
            raise ValueError("exit_modes contains duplicates")
        if "tp" in self.exit_modes and self.tp_price_pct is None:
            raise ValueError("TP search requires tp_price_pct range")
        if "rsi" in self.exit_modes and self.rsi_exit is None:
            raise ValueError("RSI search requires rsi_exit range")
        if self.min_adx_width <= 0.0:
            raise ValueError("min_adx_width must be positive")


def load_search_json(path: str | Path) -> SearchSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SEARCH_SCHEMA_VERSION:
        raise ValueError("unsupported search schema_version")
    ranges = payload["ranges"]
    gates = payload["gates"]
    refinement = payload.get("refinement") or {}
    return SearchSpec(
        name=str(payload["name"]),
        rsi_periods=tuple(int(value) for value in ranges["rsi_period"]),
        rsi_entry=_range(ranges["rsi_entry"]),
        adx_min=_range(ranges["adx_min"]),
        adx_max=_range(ranges["adx_max"]),
        exit_modes=tuple(str(value) for value in payload["exit_modes"]),
        tp_price_pct=(
            None if ranges.get("tp_price_pct") is None else _range(ranges["tp_price_pct"])
        ),
        rsi_exit=None if ranges.get("rsi_exit") is None else _range(ranges["rsi_exit"]),
        min_adx_width=float(payload.get("min_adx_width", 4.0)),
        gates=SearchGates(
            min_total_trades=int(gates["min_total_trades"]),
            min_positive_window_fraction=float(gates["min_positive_window_fraction"]),
            min_worst_window_return_pct=_optional_float(
                gates.get("min_worst_window_return_pct")
            ),
            min_worst_mae_pct=_optional_float(gates.get("min_worst_mae_pct")),
        ),
        refinement=RefinementSpec(
            enabled=bool(refinement.get("enabled", True)),
            step_divisor=int(refinement.get("step_divisor", 2)),
            radius_steps=int(refinement.get("radius_steps", 1)),
            max_seeds=int(refinement.get("max_seeds", 20)),
            max_candidates=int(refinement.get("max_candidates", 50_000)),
        ),
    )


def search_fingerprint(spec: SearchSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_search(
    study_path: str | Path,
    search_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    """Run correctness-first coarse/refined search on discovery windows only.

    This is intentionally not the future high-throughput factorized engine. Every candidate
    still goes through the truth replay so the selection semantics can be trusted first.
    """
    context = load_study_context(study_path, data_directory=data_directory)
    spec = load_search_json(search_path)
    coarse = list(_coarse_candidates(context.spec.symbol, spec))
    if len(coarse) > spec.refinement.max_candidates:
        raise ValueError(
            f"coarse grid has {len(coarse)} candidates, above max_candidates="
            f"{spec.refinement.max_candidates}; widen steps before running"
        )

    evaluated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for strategy in coarse:
        key = _strategy_key(strategy)
        if key in seen:
            continue
        seen.add(key)
        evaluated.append(_evaluate_candidate(context, strategy, stage="coarse"))

    gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
    frontier = _pareto_frontier(gated)

    refined_count = 0
    if spec.refinement.enabled and frontier:
        seeds = sorted(frontier, key=_seed_priority, reverse=True)[: spec.refinement.max_seeds]
        for seed in seeds:
            strategy = StrategySpec(**seed["strategy"])
            for refined in _refined_candidates(context.spec.symbol, spec, strategy):
                key = _strategy_key(refined)
                if key in seen:
                    continue
                if len(seen) >= spec.refinement.max_candidates:
                    raise ValueError(
                        "refinement reached max_candidates before finishing; reduce max_seeds, "
                        "radius_steps or coarse grid size"
                    )
                seen.add(key)
                evaluated.append(_evaluate_candidate(context, refined, stage="refined"))
                refined_count += 1
        gated = [row for row in evaluated if _passes_gates(row["aggregate"], spec.gates)]
        frontier = _pareto_frontier(gated)

    frontier = sorted(frontier, key=_seed_priority, reverse=True)
    return {
        "schema_version": 1,
        "kind": "parameter_extract.discovery_search",
        "search": spec.name,
        "search_fingerprint_sha256": search_fingerprint(spec),
        "search_spec": asdict(spec),
        "study": context.spec.name,
        "study_fingerprint_sha256": study_fingerprint(context.spec),
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "execution": asdict(context.spec.execution),
        "symbol": context.spec.symbol,
        "phase_used": "discovery",
        "validation_accessed": False,
        "holdout_accessed": False,
        "pareto_objectives": list(PARETO_OBJECTIVES),
        "frontier_order_note": (
            "Frontier rows are ordered only to choose refinement/reporting order; "
            "the order is not a scalar fitness ranking."
        ),
        "coarse_candidates": len(coarse),
        "refined_candidates": refined_count,
        "evaluated_candidates": len(evaluated),
        "passed_gates": len(gated),
        "pareto_candidates": len(frontier),
        "frontier": frontier,
    }


def _evaluate_candidate(context: Any, strategy: StrategySpec, *, stage: str) -> dict[str, Any]:
    result = evaluate_strategy(context, strategy, phases=("discovery",))
    if result["phases_evaluated"] != ["discovery"] or result["holdout_revealed"]:
        raise RuntimeError("search phase isolation failed")
    compact_windows = []
    for row in result["windows"]:
        metrics = row["metrics"]
        compact_windows.append(
            {
                "name": row["name"],
                "return_pct": metrics["total_return_pct"],
                "trade_count": metrics["trade_count"],
                "worst_mae_pct": metrics["worst_mae_pct"],
                "drawdown_pct": metrics["max_closed_equity_drawdown_pct"],
                "max_holding_minutes": metrics["max_holding_minutes"],
                "open_at_end": metrics["open_at_end"],
            }
        )
    return {
        "stage": stage,
        "strategy": asdict(strategy),
        "aggregate": _aggregate(compact_windows),
        "windows": compact_windows,
    }


def _aggregate(windows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["return_pct"]) for row in windows]
    maes = [
        float(row["worst_mae_pct"])
        for row in windows
        if row["worst_mae_pct"] is not None
    ]
    drawdowns = [float(row["drawdown_pct"]) for row in windows]
    holding = [
        float(row["max_holding_minutes"])
        for row in windows
        if row["max_holding_minutes"] is not None
    ]
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value / 100.0
    return {
        "window_count": len(windows),
        "positive_window_fraction": (
            0.0 if not windows else sum(value > 0.0 for value in returns) / len(windows)
        ),
        "total_trades": sum(int(row["trade_count"]) for row in windows),
        "compounded_window_return_pct": (equity - 1.0) * 100.0,
        "median_window_return_pct": statistics.median(returns) if returns else 0.0,
        "worst_window_return_pct": min(returns) if returns else 0.0,
        "worst_mae_pct": min(maes) if maes else None,
        "max_drawdown_pct": min(drawdowns) if drawdowns else 0.0,
        "max_holding_minutes": max(holding) if holding else None,
        "open_at_end_windows": sum(bool(row["open_at_end"]) for row in windows),
    }


def _passes_gates(metrics: dict[str, Any], gates: SearchGates) -> bool:
    if metrics["total_trades"] < gates.min_total_trades:
        return False
    if metrics["positive_window_fraction"] < gates.min_positive_window_fraction:
        return False
    if (
        gates.min_worst_window_return_pct is not None
        and metrics["worst_window_return_pct"] < gates.min_worst_window_return_pct
    ):
        return False
    if gates.min_worst_mae_pct is not None:
        mae = metrics["worst_mae_pct"]
        if mae is None or mae < gates.min_worst_mae_pct:
            return False
    return True


def _pareto_frontier(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    frontier: list[dict[str, Any]] = []
    for row in rows:
        if any(_dominates(existing["aggregate"], row["aggregate"]) for existing in frontier):
            continue
        frontier = [
            existing
            for existing in frontier
            if not _dominates(row["aggregate"], existing["aggregate"])
        ]
        frontier.append(row)
    return frontier


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_values = _objective_values(left)
    right_values = _objective_values(right)
    return all(a >= b for a, b in zip(left_values, right_values, strict=True)) and any(
        a > b for a, b in zip(left_values, right_values, strict=True)
    )


def _objective_values(metrics: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(metrics["worst_window_return_pct"]),
        float(metrics["median_window_return_pct"]),
        float(metrics["worst_mae_pct"] if metrics["worst_mae_pct"] is not None else -1e12),
        float(metrics["max_drawdown_pct"]),
    )


def _seed_priority(row: dict[str, Any]) -> tuple[float, float, float, float, int]:
    """Deterministic refinement order, deliberately not a claimed fitness score."""
    metrics = row["aggregate"]
    return (
        float(metrics["worst_window_return_pct"]),
        float(metrics["median_window_return_pct"]),
        float(metrics["worst_mae_pct"] if metrics["worst_mae_pct"] is not None else -1e12),
        float(metrics["max_drawdown_pct"]),
        int(metrics["total_trades"]),
    )


def _coarse_candidates(symbol: str, spec: SearchSpec) -> Iterable[StrategySpec]:
    rsi_entries = _grid(spec.rsi_entry)
    adx_mins = _grid(spec.adx_min)
    adx_maxes = _grid(spec.adx_max)
    tp_values = [] if spec.tp_price_pct is None else _grid(spec.tp_price_pct)
    rsi_exits = [] if spec.rsi_exit is None else _grid(spec.rsi_exit)
    for period in spec.rsi_periods:
        for entry in rsi_entries:
            for adx_min in adx_mins:
                for adx_max in adx_maxes:
                    if adx_max - adx_min < spec.min_adx_width:
                        continue
                    if "tp" in spec.exit_modes:
                        for tp in tp_values:
                            yield StrategySpec(
                                symbol=symbol,
                                rsi_period=period,
                                rsi_entry=entry,
                                adx_min=adx_min,
                                adx_max=adx_max,
                                exit_mode="tp",
                                tp_price_pct=tp,
                            )
                    if "rsi" in spec.exit_modes:
                        for exit_value in rsi_exits:
                            if exit_value <= entry:
                                continue
                            yield StrategySpec(
                                symbol=symbol,
                                rsi_period=period,
                                rsi_entry=entry,
                                adx_min=adx_min,
                                adx_max=adx_max,
                                exit_mode="rsi",
                                rsi_exit=exit_value,
                            )


def _refined_candidates(
    symbol: str, spec: SearchSpec, seed: StrategySpec
) -> Iterable[StrategySpec]:
    entry_values = _local_grid(spec.rsi_entry, seed.rsi_entry, spec.refinement)
    min_values = _local_grid(spec.adx_min, seed.adx_min, spec.refinement)
    max_values = _local_grid(spec.adx_max, seed.adx_max, spec.refinement)
    if seed.exit_mode == "tp":
        assert spec.tp_price_pct is not None and seed.tp_price_pct is not None
        exit_values = _local_grid(spec.tp_price_pct, seed.tp_price_pct, spec.refinement)
    else:
        assert spec.rsi_exit is not None and seed.rsi_exit is not None
        exit_values = _local_grid(spec.rsi_exit, seed.rsi_exit, spec.refinement)
    for entry in entry_values:
        for adx_min in min_values:
            for adx_max in max_values:
                if adx_max - adx_min < spec.min_adx_width:
                    continue
                for exit_value in exit_values:
                    if seed.exit_mode == "rsi" and exit_value <= entry:
                        continue
                    yield StrategySpec(
                        symbol=symbol,
                        rsi_period=seed.rsi_period,
                        rsi_entry=entry,
                        adx_min=adx_min,
                        adx_max=adx_max,
                        exit_mode=seed.exit_mode,
                        rsi_exit=exit_value if seed.exit_mode == "rsi" else None,
                        tp_price_pct=exit_value if seed.exit_mode == "tp" else None,
                    )


def _grid(spec: NumericRange) -> list[float]:
    start = Decimal(str(spec.start))
    stop = Decimal(str(spec.stop))
    step = Decimal(str(spec.step))
    values: list[float] = []
    current = start
    while current <= stop:
        values.append(float(current))
        current += step
    return values


def _local_grid(base: NumericRange, center: float, refinement: RefinementSpec) -> list[float]:
    fine_step = Decimal(str(base.step)) / Decimal(refinement.step_divisor)
    radius = Decimal(str(base.step)) * Decimal(refinement.radius_steps)
    low = max(Decimal(str(base.start)), Decimal(str(center)) - radius)
    high = min(Decimal(str(base.stop)), Decimal(str(center)) + radius)
    values: list[float] = []
    current = low
    while current <= high:
        values.append(float(current))
        current += fine_step
    return values


def _strategy_key(strategy: StrategySpec) -> tuple[Any, ...]:
    return (
        strategy.rsi_period,
        strategy.rsi_entry,
        strategy.adx_min,
        strategy.adx_max,
        strategy.exit_mode,
        strategy.rsi_exit,
        strategy.tp_price_pct,
    )


def _range(payload: dict[str, Any]) -> NumericRange:
    return NumericRange(
        start=float(payload["start"]),
        stop=float(payload["stop"]),
        step=float(payload["step"]),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
