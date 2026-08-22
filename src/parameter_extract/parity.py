from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Candle, StrategySpec
from .signals import generate_signals

PARITY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ParityReport:
    ok: bool
    source_repository: str
    source_commit: str
    checked_points: int
    expected_signals: int
    problems: tuple[str, ...]


def fixture_candles(payload: dict[str, Any]) -> list[Candle]:
    definition = payload["candles"]
    if isinstance(definition, list):
        return [Candle(**row) for row in definition]
    if not isinstance(definition, dict) or definition.get("kind") != "synthetic_wave_v1":
        raise ValueError("unsupported parity candle fixture")
    count = int(definition["count"])
    base_time = int(definition["base_time_ms"])
    base_price = float(definition.get("base_price", 100.0))
    amplitude = float(definition.get("amplitude", 4.0))
    divisor = float(definition.get("period_divisor", 4.2))
    trend = float(definition.get("trend_per_bar", 0.015))
    open_phase = float(definition.get("open_phase", 0.6))
    spread_base = float(definition.get("spread_base", 0.35))
    spread_growth = float(definition.get("spread_growth", 0.003))
    volume_base = float(definition.get("volume_base", 100.0))
    candles: list[Candle] = []
    for index in range(count):
        close = base_price + amplitude * math.sin(index / divisor) + trend * index
        open_value = (
            base_price
            + amplitude * math.sin((index - open_phase) / divisor)
            + trend * (index - open_phase)
        )
        spread = spread_base + spread_growth * index
        candles.append(
            Candle(
                open_time_ms=base_time + index * 60_000,
                close_time_ms=base_time + index * 60_000 + 59_999,
                open=open_value,
                high=max(open_value, close) + spread,
                low=min(open_value, close) - spread,
                close=close,
                volume=volume_base + index,
            )
        )
    return candles


def check_parity_fixture(path: str | Path, *, abs_tol: float = 1e-12) -> ParityReport:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != PARITY_SCHEMA_VERSION:
        raise ValueError("unsupported parity fixture schema_version")
    source = payload.get("source") or {}
    repository = str(source.get("repository") or "")
    commit = str(source.get("commit") or "")
    if not repository or len(commit) != 40:
        raise ValueError("parity fixture must name a source repository and full 40-char commit")

    strategy = StrategySpec(**payload["strategy"])
    candles = fixture_candles(payload)
    signals, points = generate_signals(candles, strategy)
    expected = payload["expected"]
    problems: list[str] = []

    expected_signals = expected.get("signals", [])
    actual_signal_pairs = [(item.candle_index, item.timestamp_ms) for item in signals]
    expected_signal_pairs = [
        (int(item["candle_index"]), int(item["timestamp_ms"])) for item in expected_signals
    ]
    if actual_signal_pairs != expected_signal_pairs:
        problems.append(f"signals differ: expected {expected_signal_pairs}, got {actual_signal_pairs}")

    checked = 0
    for row in expected.get("points", []):
        index = int(row["index"])
        actual = points[index]
        checked += 1
        if actual is None:
            problems.append(f"point {index}: expected defined, got None")
            continue
        for field in ("rsi", "adx", "adr"):
            expected_value = float(row[field])
            actual_value = float(getattr(actual, field))
            if not math.isclose(actual_value, expected_value, rel_tol=0.0, abs_tol=abs_tol):
                problems.append(
                    f"point {index} {field}: expected {expected_value:.17g}, got {actual_value:.17g}"
                )

    return ParityReport(
        ok=not problems,
        source_repository=repository,
        source_commit=commit,
        checked_points=checked,
        expected_signals=len(expected_signal_pairs),
        problems=tuple(problems),
    )
