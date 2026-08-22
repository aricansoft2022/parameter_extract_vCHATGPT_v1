from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Sequence

from .io import load_binance_klines_csv, load_funding_csv, load_strategy_json
from .manifest import verify_manifest
from .metrics import summarize
from .models import Candle, ExecutionModel, FundingEvent, StrategySpec
from .replay import ReplayResult, replay_signals
from .signals import Signal, generate_signals

STUDY_SCHEMA_VERSION = 1
Phase = Literal["discovery", "validation", "holdout"]


@dataclass(frozen=True, slots=True)
class WindowSpec:
    name: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("window name cannot be empty")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError(f"window {self.name!r} has invalid bounds")


@dataclass(frozen=True, slots=True)
class StudySpec:
    name: str
    symbol: str
    dataset_manifest: str
    dataset_fingerprint_sha256: str
    execution: ExecutionModel
    discovery: tuple[WindowSpec, ...]
    validation: tuple[WindowSpec, ...]
    holdout: tuple[WindowSpec, ...]
    warmup_candles: int = 300
    min_trades: int = 30

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("study name cannot be empty")
        if not self.symbol.isupper() or not self.symbol.endswith("USDT"):
            raise ValueError("study symbol must be an upper-case USDT pair")
        _validate_digest(self.dataset_fingerprint_sha256)
        if self.warmup_candles < 30:
            raise ValueError("warmup_candles must be at least 30")
        if self.min_trades < 1:
            raise ValueError("min_trades must be positive")
        if not self.discovery:
            raise ValueError("study requires at least one discovery window")
        if not self.validation:
            raise ValueError("study requires at least one validation window")
        _validate_windows(self.discovery, self.validation, self.holdout)


@dataclass(frozen=True, slots=True)
class StudyContext:
    spec: StudySpec
    candles: tuple[Candle, ...]
    funding: tuple[FundingEvent, ...]


@dataclass(frozen=True, slots=True)
class _WindowReplay:
    signals: tuple[Signal, ...]
    result: ReplayResult


def load_study_json(path: str | Path) -> StudySpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != STUDY_SCHEMA_VERSION:
        raise ValueError("unsupported study schema_version")
    windows = payload.get("windows") or {}
    execution = ExecutionModel(**(payload.get("execution") or {}))
    return StudySpec(
        name=str(payload["name"]),
        symbol=str(payload["symbol"]),
        dataset_manifest=str(payload["dataset_manifest"]),
        dataset_fingerprint_sha256=str(payload["dataset_fingerprint_sha256"]).lower(),
        execution=execution,
        discovery=_windows_from_payload(windows.get("discovery", [])),
        validation=_windows_from_payload(windows.get("validation", [])),
        holdout=_windows_from_payload(windows.get("holdout", [])),
        warmup_candles=int(payload.get("warmup_candles", 300)),
        min_trades=int(payload.get("min_trades", 30)),
    )


def study_fingerprint(spec: StudySpec) -> str:
    payload = {
        "schema_version": STUDY_SCHEMA_VERSION,
        "name": spec.name,
        "symbol": spec.symbol,
        "dataset_manifest": spec.dataset_manifest,
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "execution": asdict(spec.execution),
        "windows": {
            "discovery": [asdict(item) for item in spec.discovery],
            "validation": [asdict(item) for item in spec.validation],
            "holdout": [asdict(item) for item in spec.holdout],
        },
        "warmup_candles": spec.warmup_candles,
        "min_trades": spec.min_trades,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_study_context(
    study_path: str | Path, *, data_directory: str | Path
) -> StudyContext:
    study_file = Path(study_path)
    spec = load_study_json(study_file)
    manifest_path = (study_file.parent / spec.dataset_manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    found_fingerprint = manifest.get("dataset_fingerprint_sha256")
    if found_fingerprint != spec.dataset_fingerprint_sha256:
        raise ValueError(
            "study dataset fingerprint does not match its manifest: "
            f"expected {spec.dataset_fingerprint_sha256}, found {found_fingerprint}"
        )
    if (manifest.get("candles") or {}).get("integrity_ok") is not True:
        raise ValueError("dataset manifest candle integrity gate is not healthy")
    file_records = manifest["files"]
    for label in ("candles", "funding"):
        record = file_records.get(label)
        if isinstance(record, dict) and record.get("checksum_verified") is False:
            raise ValueError(f"dataset {label} file failed its external checksum")
    problems = verify_manifest(manifest, directory=data_directory)
    if problems:
        raise ValueError("dataset manifest verification failed: " + "; ".join(problems))

    root = Path(data_directory)
    candles = load_binance_klines_csv(root / file_records["candles"]["path"])
    funding_record = file_records.get("funding")
    funding = [] if funding_record is None else load_funding_csv(root / funding_record["path"])
    return StudyContext(spec=spec, candles=tuple(candles), funding=tuple(funding))


def evaluate_strategy(
    context: StudyContext,
    strategy: StrategySpec,
    *,
    phases: Sequence[Phase] = ("discovery", "validation"),
    reveal_holdout: bool = False,
) -> dict[str, Any]:
    spec = context.spec
    _validate_strategy_symbol(spec, strategy)
    requested = _validate_phase_request(phases, reveal_holdout=reveal_holdout)
    phase_windows = _phase_windows(spec)
    windows = [
        _run_window(
            phase,
            window,
            context.candles,
            context.funding,
            strategy,
            spec.execution,
            warmup_candles=spec.warmup_candles,
            min_trades=spec.min_trades,
        )
        for phase in requested
        for window in phase_windows[phase]
    ]
    holdout_revealed = "holdout" in requested
    return {
        "schema_version": 1,
        "kind": "parameter_extract.study_result",
        "study": spec.name,
        "study_fingerprint_sha256": study_fingerprint(spec),
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "symbol": spec.symbol,
        "team": asdict(strategy),
        "execution": asdict(spec.execution),
        "phases_evaluated": list(requested),
        "holdout_revealed": holdout_revealed,
        "withheld_holdout_windows": 0 if holdout_revealed else len(spec.holdout),
        "windows": windows,
    }


def collect_strategy_evidence(
    context: StudyContext,
    strategy: StrategySpec,
    *,
    phases: Sequence[Phase] = ("discovery", "validation"),
) -> dict[str, Any]:
    """Collect behavioral timestamps for similarity analysis without touching holdout.

    This API is deliberately narrower than ``evaluate_strategy``: holdout is not merely
    hidden by default, it is forbidden. It exposes raw signal times, accepted-entry signal
    times and position intervals while preserving the same flat-per-window semantics.
    """
    spec = context.spec
    _validate_strategy_symbol(spec, strategy)
    requested = tuple(phases)
    if not requested:
        raise ValueError("at least one evidence phase is required")
    if len(requested) != len(set(requested)):
        raise ValueError("evidence phases must be unique")
    unknown = set(requested) - {"discovery", "validation"}
    if unknown:
        raise ValueError("behavioral evidence may use discovery and validation only")

    phase_windows = _phase_windows(spec)
    windows: list[dict[str, Any]] = []
    for phase in requested:
        for window in phase_windows[phase]:
            replay = _replay_window(
                window,
                context.candles,
                context.funding,
                strategy,
                spec.execution,
                warmup_candles=spec.warmup_candles,
            )
            accepted_signal_times = [trade.signal_time_ms for trade in replay.result.trades]
            position_intervals: list[list[int]] = [
                [trade.entry_time_ms, max(trade.entry_time_ms + 1, trade.exit_time_ms)]
                for trade in replay.result.trades
            ]
            if replay.result.open_position is not None:
                accepted_signal_times.append(replay.result.open_position.signal_time_ms)
                position_intervals.append(
                    [
                        replay.result.open_position.entry_time_ms,
                        max(replay.result.open_position.entry_time_ms + 1, window.end_ms),
                    ]
                )
            windows.append(
                {
                    "phase": phase,
                    "name": window.name,
                    "start_ms": window.start_ms,
                    "end_ms": window.end_ms,
                    "raw_signal_times_ms": [signal.timestamp_ms for signal in replay.signals],
                    "accepted_signal_times_ms": sorted(accepted_signal_times),
                    "position_intervals_ms": position_intervals,
                }
            )

    return {
        "schema_version": 1,
        "kind": "parameter_extract.strategy_evidence",
        "study_fingerprint_sha256": study_fingerprint(spec),
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "symbol": spec.symbol,
        "strategy": asdict(strategy),
        "phases_evaluated": list(requested),
        "holdout_accessed": False,
        "windows": windows,
    }


def run_study(
    study_path: str | Path,
    team_path: str | Path,
    *,
    data_directory: str | Path,
    reveal_holdout: bool = False,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    strategy = load_strategy_json(team_path)
    phases: tuple[Phase, ...] = (
        ("discovery", "validation", "holdout")
        if reveal_holdout
        else ("discovery", "validation")
    )
    return evaluate_strategy(
        context,
        strategy,
        phases=phases,
        reveal_holdout=reveal_holdout,
    )


def _run_window(
    phase: Phase,
    window: WindowSpec,
    candles: Sequence[Candle],
    funding: Sequence[FundingEvent],
    strategy: StrategySpec,
    execution: ExecutionModel,
    *,
    warmup_candles: int,
    min_trades: int,
) -> dict[str, Any]:
    replay = _replay_window(
        window,
        candles,
        funding,
        strategy,
        execution,
        warmup_candles=warmup_candles,
    )
    return {
        "phase": phase,
        "name": window.name,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "warmup_candles": warmup_candles,
        "raw_signal_count": replay.result.raw_signal_count,
        "accepted_signal_count": replay.result.accepted_signal_count,
        "skipped_while_open": replay.result.skipped_while_open,
        "cancelled_on_gap": replay.result.cancelled_on_gap,
        "metrics": summarize(replay.result, min_trades=min_trades).as_dict(),
    }


def _replay_window(
    window: WindowSpec,
    candles: Sequence[Candle],
    funding: Sequence[FundingEvent],
    strategy: StrategySpec,
    execution: ExecutionModel,
    *,
    warmup_candles: int,
) -> _WindowReplay:
    start_index = next(
        (index for index, candle in enumerate(candles) if candle.open_time_ms >= window.start_ms),
        None,
    )
    if start_index is None:
        raise ValueError(f"window {window.name!r} starts after the dataset ends")
    if start_index < warmup_candles:
        raise ValueError(
            f"window {window.name!r} has only {start_index} pre-window candles; "
            f"{warmup_candles} required"
        )
    end_index = next(
        (
            index
            for index in range(start_index, len(candles))
            if candles[index].open_time_ms >= window.end_ms
        ),
        len(candles),
    )
    if end_index <= start_index:
        raise ValueError(f"window {window.name!r} contains no candles")
    if end_index == len(candles) and candles[-1].close_time_ms < window.end_ms - 1:
        raise ValueError(f"window {window.name!r} extends beyond the dataset")

    local = candles[start_index - warmup_candles : end_index]
    raw_signals, points = generate_signals(local, strategy)
    signals = tuple(
        signal
        for signal in raw_signals
        if window.start_ms <= signal.timestamp_ms < window.end_ms
    )
    local_funding = [
        event
        for event in funding
        if local[0].open_time_ms <= event.timestamp_ms < window.end_ms
    ]
    result = replay_signals(
        local,
        strategy,
        signals,
        points,
        execution=execution,
        funding=local_funding,
    )
    normalized = replace(
        result,
        dataset_start_ms=window.start_ms,
        dataset_end_ms=window.end_ms - 1,
    )
    return _WindowReplay(signals=signals, result=normalized)


def _phase_windows(spec: StudySpec) -> dict[Phase, tuple[WindowSpec, ...]]:
    return {
        "discovery": spec.discovery,
        "validation": spec.validation,
        "holdout": spec.holdout,
    }


def _validate_strategy_symbol(spec: StudySpec, strategy: StrategySpec) -> None:
    if strategy.symbol != spec.symbol:
        raise ValueError(
            f"team symbol {strategy.symbol} does not match study symbol {spec.symbol}"
        )


def _validate_phase_request(
    phases: Sequence[Phase], *, reveal_holdout: bool
) -> tuple[Phase, ...]:
    requested = tuple(phases)
    if not requested:
        raise ValueError("at least one study phase is required")
    if len(requested) != len(set(requested)):
        raise ValueError("study phases must be unique")
    unknown = set(requested) - {"discovery", "validation", "holdout"}
    if unknown:
        raise ValueError(f"unknown study phase(s): {', '.join(sorted(unknown))}")
    if "holdout" in requested and not reveal_holdout:
        raise ValueError("holdout evaluation requires reveal_holdout=True")
    return requested


def _windows_from_payload(rows: list[dict[str, Any]]) -> tuple[WindowSpec, ...]:
    return tuple(
        WindowSpec(
            name=str(row["name"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
        )
        for row in rows
    )


def _validate_windows(*groups: tuple[WindowSpec, ...]) -> None:
    rows = [window for group in groups for window in group]
    names = [window.name for window in rows]
    if len(names) != len(set(names)):
        raise ValueError("window names must be unique across the study")
    ordered = sorted(rows, key=lambda item: (item.start_ms, item.end_ms))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_ms < previous.end_ms:
            raise ValueError(
                f"study windows overlap: {previous.name!r} and {current.name!r}"
            )


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("dataset_fingerprint_sha256 must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("dataset_fingerprint_sha256 is not hexadecimal") from exc
