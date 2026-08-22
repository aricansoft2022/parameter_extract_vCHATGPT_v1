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
from .replay import replay_signals
from .signals import generate_signals

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


def load_study_json(path: str | Path) -> StudySpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != STUDY_SCHEMA_VERSION:
        raise ValueError("unsupported study schema_version")
    windows = payload.get("windows") or {}
    execution_payload = payload.get("execution") or {}
    execution = ExecutionModel(**execution_payload)
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


def run_study(
    study_path: str | Path,
    team_path: str | Path,
    *,
    data_directory: str | Path,
    reveal_holdout: bool = False,
) -> dict[str, Any]:
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

    candle_name = file_records["candles"]["path"]
    funding_record = file_records.get("funding")
    root = Path(data_directory)
    candles = load_binance_klines_csv(root / candle_name)
    funding = [] if funding_record is None else load_funding_csv(root / funding_record["path"])
    strategy = load_strategy_json(team_path)
    if strategy.symbol != spec.symbol:
        raise ValueError(
            f"team symbol {strategy.symbol} does not match study symbol {spec.symbol}"
        )

    phases: list[tuple[Phase, WindowSpec]] = []
    phases.extend(("discovery", item) for item in spec.discovery)
    phases.extend(("validation", item) for item in spec.validation)
    if reveal_holdout:
        phases.extend(("holdout", item) for item in spec.holdout)

    windows = [
        _run_window(
            phase,
            window,
            candles,
            funding,
            strategy,
            spec.execution,
            warmup_candles=spec.warmup_candles,
            min_trades=spec.min_trades,
        )
        for phase, window in phases
    ]
    return {
        "schema_version": 1,
        "kind": "parameter_extract.study_result",
        "study": spec.name,
        "study_fingerprint_sha256": study_fingerprint(spec),
        "dataset_fingerprint_sha256": spec.dataset_fingerprint_sha256,
        "symbol": spec.symbol,
        "team": asdict(strategy),
        "execution": asdict(spec.execution),
        "holdout_revealed": reveal_holdout,
        "withheld_holdout_windows": 0 if reveal_holdout else len(spec.holdout),
        "windows": windows,
    }


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
    signals = [
        signal
        for signal in raw_signals
        if window.start_ms <= signal.timestamp_ms < window.end_ms
    ]
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
    return {
        "phase": phase,
        "name": window.name,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "warmup_candles": warmup_candles,
        "raw_signal_count": normalized.raw_signal_count,
        "accepted_signal_count": normalized.accepted_signal_count,
        "skipped_while_open": normalized.skipped_while_open,
        "cancelled_on_gap": normalized.cancelled_on_gap,
        "metrics": summarize(normalized, min_trades=min_trades).as_dict(),
    }


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
