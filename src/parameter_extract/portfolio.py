from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .families import (
    FamilySpec,
    FamilyThresholds,
    ParameterScales,
    _representative_sort_key,
    family_fingerprint,
)
from .manifest import sha256_file
from .models import Candle, FundingEvent, StrategySpec
from .promotion import candidate_fingerprint
from .replay import (
    OpenPosition,
    Trade,
    _Position,
    _apply_funding_for_candle,
    _close_trade,
    _exit_for_candle,
    _open_at_signal_close,
    _open_position,
    _update_excursions,
)
from .signals import ONE_MINUTE_MS, Signal, generate_signals
from .study import StudyContext, WindowSpec, load_study_context, study_fingerprint

PORTFOLIO_SCHEMA_VERSION = 1
REPRESENTATIVE_POLICY = "robustness_stability_v1"


@dataclass(frozen=True, slots=True)
class PortfolioSpec:
    name: str
    source_families_result_sha256: str
    slot_count: int
    priority_family_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("portfolio name cannot be empty")
        _validate_digest(self.source_families_result_sha256)
        if self.slot_count < 1:
            raise ValueError("slot_count must be positive")
        if not self.priority_family_ids:
            raise ValueError("priority_family_ids cannot be empty")
        if len(self.priority_family_ids) != len(set(self.priority_family_ids)):
            raise ValueError("priority_family_ids must be unique")


@dataclass(slots=True)
class _Pending:
    signal: Signal
    slot_no: int


@dataclass(slots=True)
class _PortfolioPosition:
    position: _Position
    slot_no: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    family_id: str
    fingerprint: str
    strategy: StrategySpec
    priority: int


@dataclass(frozen=True, slots=True)
class _Closed:
    family_id: str
    candidate_fingerprint_sha256: str
    slot_no: int
    trade: Trade


def load_portfolio_json(path: str | Path) -> PortfolioSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != PORTFOLIO_SCHEMA_VERSION:
        raise ValueError("unsupported portfolio schema_version")
    return PortfolioSpec(
        name=str(payload["name"]),
        source_families_result_sha256=str(
            payload["source_families_result_sha256"]
        ).lower(),
        slot_count=int(payload["slot_count"]),
        priority_family_ids=tuple(str(value) for value in payload["priority_family_ids"]),
    )


def portfolio_fingerprint(spec: PortfolioSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_portfolio(
    study_path: str | Path,
    families_result_path: str | Path,
    portfolio_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    families_path = Path(families_result_path)
    families_result = json.loads(families_path.read_text(encoding="utf-8"))
    spec = load_portfolio_json(portfolio_path)
    actual_families_sha = sha256_file(families_path)
    if spec.source_families_result_sha256 != actual_families_sha:
        raise ValueError("portfolio contract is pinned to a different families-result file")

    problems = verify_families_result(families_result)
    if problems:
        raise ValueError("families-result verification failed: " + "; ".join(problems))
    if families_result.get("study_fingerprint_sha256") != study_fingerprint(context.spec):
        raise ValueError("families result belongs to a different study contract")
    if families_result.get("dataset_fingerprint_sha256") != context.spec.dataset_fingerprint_sha256:
        raise ValueError("families result belongs to a different dataset")
    if families_result.get("symbol") != context.spec.symbol:
        raise ValueError("families-result symbol does not match the study")
    if families_result.get("execution") != asdict(context.spec.execution):
        raise ValueError("families-result execution assumptions do not match the study")

    representatives_by_id = {
        row["family_id"]: row for row in families_result["representatives"]
    }
    expected_family_ids = set(representatives_by_id)
    if set(spec.priority_family_ids) != expected_family_ids:
        missing = sorted(expected_family_ids - set(spec.priority_family_ids))
        extra = sorted(set(spec.priority_family_ids) - expected_family_ids)
        raise ValueError(
            "priority_family_ids must contain every representative exactly once; "
            f"missing={missing}, extra={extra}"
        )

    candidates = tuple(
        _Candidate(
            family_id=family_id,
            fingerprint=representatives_by_id[family_id][
                "candidate_fingerprint_sha256"
            ],
            strategy=StrategySpec(**representatives_by_id[family_id]["strategy"]),
            priority=index + 1,
        )
        for index, family_id in enumerate(spec.priority_family_ids)
    )

    windows: list[dict[str, Any]] = []
    for phase, phase_windows in (
        ("discovery", context.spec.discovery),
        ("validation", context.spec.validation),
    ):
        for window in phase_windows:
            windows.append(
                _run_portfolio_window(
                    phase,
                    window,
                    context,
                    candidates,
                    slot_count=spec.slot_count,
                )
            )

    aggregate = _aggregate_portfolio_windows(windows, slot_count=spec.slot_count)
    phase_aggregates = {
        phase: _aggregate_portfolio_windows(
            [row for row in windows if row["phase"] == phase],
            slot_count=spec.slot_count,
        )
        for phase in ("discovery", "validation")
    }
    return {
        "schema_version": 1,
        "kind": "parameter_extract.portfolio_replay",
        "portfolio": spec.name,
        "portfolio_fingerprint_sha256": portfolio_fingerprint(spec),
        "portfolio_spec": asdict(spec),
        "source_families_result_sha256": actual_families_sha,
        "study_fingerprint_sha256": study_fingerprint(context.spec),
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "symbol": context.spec.symbol,
        "execution": asdict(context.spec.execution),
        "strategy_parameters_retuned": False,
        "priority_optimized": False,
        "priority_source": "explicit_portfolio_contract",
        "leverage_applied": False,
        "return_basis": "equal_fixed_unlevered_slot_baseline",
        "slot_utilization_assumption": "occupied_at_candle_open_counts_full_minute",
        "discovery_accessed": True,
        "validation_accessed": True,
        "holdout_accessed": False,
        "slot_count": spec.slot_count,
        "representative_count": len(candidates),
        "priorities": [
            {
                "priority": row.priority,
                "family_id": row.family_id,
                "candidate_fingerprint_sha256": row.fingerprint,
                "strategy": asdict(row.strategy),
            }
            for row in candidates
        ],
        "aggregate": aggregate,
        "phase_aggregates": phase_aggregates,
        "windows": windows,
    }


def verify_families_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported families-result schema_version")
    if payload.get("kind") != "parameter_extract.strategy_families":
        problems.append("families-result kind is invalid")
    if payload.get("parameters_retuned") is not False:
        problems.append("families result does not prove frozen parameters")
    if payload.get("representatives_are_existing_robust_centers") is not True:
        problems.append("family representatives are not proven existing robust centers")
    if payload.get("representative_policy") != REPRESENTATIVE_POLICY:
        problems.append("family representative policy is missing or unsupported")
    if payload.get("discovery_accessed") is not True:
        problems.append("families result does not indicate discovery access")
    if payload.get("validation_accessed") is not True:
        problems.append("families result does not indicate validation access")
    if payload.get("holdout_accessed") is not False:
        problems.append("families result indicates holdout access")

    spec_payload = payload.get("family_spec")
    if not isinstance(spec_payload, dict):
        problems.append("family_spec is missing or invalid")
    else:
        try:
            thresholds = FamilyThresholds(**spec_payload["thresholds"])
            scales = ParameterScales(**spec_payload["parameter_scales"])
            family_spec = FamilySpec(
                name=str(spec_payload["name"]),
                source_robustness_result_sha256=str(
                    spec_payload["source_robustness_result_sha256"]
                ),
                thresholds=thresholds,
                parameter_scales=scales,
                max_pair_evaluations=int(spec_payload["max_pair_evaluations"]),
            )
            if payload.get("family_fingerprint_sha256") != family_fingerprint(family_spec):
                problems.append("family fingerprint does not match family_spec")
            if (
                payload.get("source_robustness_result_sha256")
                != family_spec.source_robustness_result_sha256
            ):
                problems.append("family source robustness-result SHA is inconsistent")
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"family_spec is invalid: {exc}")

    families = payload.get("families")
    representatives = payload.get("representatives")
    pairwise = payload.get("pairwise")
    if not isinstance(families, list):
        problems.append("families list is missing or invalid")
        return problems
    if not isinstance(representatives, list):
        problems.append("representatives list is missing or invalid")
        return problems
    if not isinstance(pairwise, list):
        problems.append("pairwise list is missing or invalid")
        return problems
    if payload.get("family_count") != len(families):
        problems.append("family_count does not match family list length")
    if len(representatives) != len(families):
        problems.append("representative count does not match family count")

    robust_center_count = int(payload.get("robust_center_count", -1))
    if robust_center_count < 1:
        problems.append("robust_center_count must be positive")
    if payload.get("deduplicated_center_count") != robust_center_count - len(families):
        problems.append("deduplicated_center_count is inconsistent")
    expected_pairs = robust_center_count * (robust_center_count - 1) // 2
    if payload.get("pair_evaluations") != expected_pairs or len(pairwise) != expected_pairs:
        problems.append("pairwise evidence count is inconsistent")

    pair_lookup: dict[frozenset[str], dict[str, Any]] = {}
    pair_endpoints: set[str] = set()
    for index, row in enumerate(pairwise):
        if not isinstance(row, dict):
            problems.append(f"pair {index}: invalid row")
            continue
        left = row.get("left")
        right = row.get("right")
        if not isinstance(left, str) or not isinstance(right, str) or left == right:
            problems.append(f"pair {index}: invalid endpoints")
            continue
        key = frozenset((left, right))
        if key in pair_lookup:
            problems.append(f"pair {index}: duplicate pair endpoints")
        pair_lookup[key] = row
        pair_endpoints.update((left, right))
        for metric in (
            "raw_signal_dice",
            "accepted_signal_dice",
            "exposure_jaccard",
            "parameter_distance",
            "parameter_distance_similarity",
            "family_score",
        ):
            value = row.get(metric)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                problems.append(f"pair {index}: {metric} is not finite numeric evidence")

    representative_by_family: dict[str, dict[str, Any]] = {}
    seen_rep_fps: set[str] = set()
    for index, row in enumerate(representatives):
        try:
            family_id = str(row["family_id"])
            strategy = StrategySpec(**row["strategy"])
            fingerprint = candidate_fingerprint(strategy)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"representative {index}: invalid row: {exc}")
            continue
        if row.get("candidate_fingerprint_sha256") != fingerprint:
            problems.append(f"representative {index}: strategy fingerprint mismatch")
        if row.get("parameters_retuned") is not False:
            problems.append(f"representative {index}: parameters_retuned is not false")
        if family_id in representative_by_family:
            problems.append(f"representative {index}: duplicate family_id")
        if fingerprint in seen_rep_fps:
            problems.append(f"representative {index}: duplicate candidate fingerprint")
        representative_by_family[family_id] = row
        seen_rep_fps.add(fingerprint)

    all_member_fps: set[str] = set()
    for index, family in enumerate(families):
        if not isinstance(family, dict):
            problems.append(f"family {index}: invalid row")
            continue
        family_id = family.get("family_id")
        members = family.get("members")
        if not isinstance(family_id, str) or not isinstance(members, list) or not members:
            problems.append(f"family {index}: missing id or members")
            continue
        if family.get("representative_selection") != REPRESENTATIVE_POLICY:
            problems.append(f"family {index}: representative policy mismatch")
        if family.get("member_count") != len(members):
            problems.append(f"family {index}: member_count mismatch")
        rep = representative_by_family.get(family_id)
        if rep is None:
            problems.append(f"family {index}: missing representative row")
        member_fps: set[str] = set()
        sortable_members: list[dict[str, Any]] = []
        for member_index, member in enumerate(members):
            try:
                strategy = StrategySpec(**member["strategy"])
                fingerprint = candidate_fingerprint(strategy)
                metrics = member["robustness_metrics"]
                if not isinstance(metrics, dict):
                    raise TypeError("robustness_metrics must be an object")
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(
                    f"family {index} member {member_index}: invalid member: {exc}"
                )
                continue
            if member.get("candidate_fingerprint_sha256") != fingerprint:
                problems.append(
                    f"family {index} member {member_index}: strategy fingerprint mismatch"
                )
            if fingerprint in member_fps or fingerprint in all_member_fps:
                problems.append(
                    f"family {index} member {member_index}: duplicate membership fingerprint"
                )
            member_fps.add(fingerprint)
            all_member_fps.add(fingerprint)
            sortable_members.append(
                {
                    "candidate_fingerprint_sha256": fingerprint,
                    "metrics": metrics,
                }
            )
        representative_fp = family.get(
            "representative_candidate_fingerprint_sha256"
        )
        if representative_fp not in member_fps:
            problems.append(f"family {index}: representative is not a family member")
        if rep is not None and rep.get("candidate_fingerprint_sha256") != representative_fp:
            problems.append(f"family {index}: representative rows disagree")
        if rep is not None and rep.get("strategy") != family.get("representative_strategy"):
            problems.append(f"family {index}: representative strategies disagree")
        if sortable_members:
            expected_rep = sorted(sortable_members, key=_representative_sort_key)[0][
                "candidate_fingerprint_sha256"
            ]
            if representative_fp != expected_rep:
                problems.append(f"family {index}: representative violates deterministic policy")

        member_list = sorted(member_fps)
        for left_index, left in enumerate(member_list):
            for right in member_list[left_index + 1 :]:
                pair = pair_lookup.get(frozenset((left, right)))
                if pair is None:
                    problems.append(f"family {index}: missing within-family pair evidence")
                elif pair.get("same_family") is not True:
                    problems.append(f"family {index}: contains a non-family pair")

    if len(all_member_fps) != robust_center_count:
        problems.append("family members do not cover robust_center_count exactly")
    if pair_endpoints - all_member_fps:
        problems.append("pairwise evidence references unknown family members")
    return problems


def _run_portfolio_window(
    phase: str,
    window: WindowSpec,
    context: StudyContext,
    candidates: Sequence[_Candidate],
    *,
    slot_count: int,
) -> dict[str, Any]:
    local, local_funding, signals_by_family, points_by_family = _prepare_window(
        window,
        context,
        candidates,
    )
    execution = context.spec.execution
    free_slots = list(range(1, slot_count + 1))
    positions: dict[str, _PortfolioPosition] = {}
    pending: dict[str, _Pending] = {}
    closed: list[_Closed] = []
    blocked_no_slot: dict[str, int] = {row.family_id: 0 for row in candidates}
    skipped_active: dict[str, int] = {row.family_id: 0 for row in candidates}
    cancelled_gap: dict[str, int] = {row.family_id: 0 for row in candidates}
    raw_signals: dict[str, int] = {
        row.family_id: len(signals_by_family[row.family_id]) for row in candidates
    }
    accepted: dict[str, int] = {row.family_id: 0 for row in candidates}
    slot_occupancy_ms = 0

    signal_maps = {
        family_id: {signal.candle_index: signal for signal in signals}
        for family_id, signals in signals_by_family.items()
    }
    candidate_by_id = {row.family_id: row for row in candidates}

    for index, candle in enumerate(local):
        gap_before = (
            index > 0
            and candle.open_time_ms != local[index - 1].open_time_ms + ONE_MINUTE_MS
        )
        if gap_before:
            for family_id, reservation in list(pending.items()):
                free_slots.append(reservation.slot_no)
                free_slots.sort()
                del pending[family_id]
                cancelled_gap[family_id] += 1

        # PENDING_ENTRY already owns a slot. The next-open model changes only fill timing,
        # not the signal-time slot decision.
        for candidate in candidates:
            reservation = pending.get(candidate.family_id)
            if reservation is None:
                continue
            expected_index = reservation.signal.candle_index + 1
            if index == expected_index and not gap_before:
                position = _open_position(
                    reservation.signal,
                    candle,
                    index,
                    execution,
                )
                positions[candidate.family_id] = _PortfolioPosition(
                    position=position,
                    slot_no=reservation.slot_no,
                )
                del pending[candidate.family_id]
                accepted[candidate.family_id] += 1

        # Occupancy is sampled at candle open. A TP may happen earlier than candle close,
        # but OHLC cannot reveal the exact time, so counting the full minute is conservative.
        if candle.open_time_ms < window.end_ms and candle.close_time_ms >= window.start_ms:
            interval_start = max(candle.open_time_ms, window.start_ms)
            interval_end = min(candle.open_time_ms + ONE_MINUTE_MS, window.end_ms)
            if interval_end > interval_start:
                slot_occupancy_ms += len(positions) * (interval_end - interval_start)

        # Existing positions use the exact single-team truth primitives for path, funding
        # and exit accounting. Exits free slots before same-close new signals are considered.
        for family_id, held in list(positions.items()):
            candidate = candidate_by_id[family_id]
            point = points_by_family[family_id][index]
            _update_excursions(held.position, candle, candidate.strategy)
            exit_payload = _exit_for_candle(
                held.position,
                candle,
                point,
                candidate.strategy,
                execution,
            )
            _apply_funding_for_candle(
                held.position,
                local_funding,
                candle,
                conservative_tp_exit=(
                    exit_payload is not None and candidate.strategy.exit_mode == "tp"
                ),
            )
            if exit_payload is None:
                continue
            exit_price, reason = exit_payload
            trade = _close_trade(
                held.position,
                candle,
                exit_price,
                reason,
                execution,
            )
            closed.append(
                _Closed(
                    family_id=family_id,
                    candidate_fingerprint_sha256=candidate.fingerprint,
                    slot_no=held.slot_no,
                    trade=trade,
                )
            )
            free_slots.append(held.slot_no)
            free_slots.sort()
            del positions[family_id]

        # The family priority order is the portfolio contract. No permutation search occurs.
        for candidate in candidates:
            signal = signal_maps[candidate.family_id].get(index)
            if signal is None:
                continue
            if candidate.family_id in positions or candidate.family_id in pending:
                skipped_active[candidate.family_id] += 1
                continue
            if not free_slots:
                blocked_no_slot[candidate.family_id] += 1
                continue
            slot_no = free_slots.pop(0)
            if execution.entry_timing == "signal_close":
                positions[candidate.family_id] = _PortfolioPosition(
                    position=_open_at_signal_close(
                        signal,
                        candle,
                        index,
                        execution,
                    ),
                    slot_no=slot_no,
                )
                accepted[candidate.family_id] += 1
            else:
                # The slot is reserved now, at signal time, just like a live PENDING_ENTRY.
                pending[candidate.family_id] = _Pending(
                    signal=signal,
                    slot_no=slot_no,
                )

    open_rows: list[dict[str, Any]] = []
    for family_id, held in positions.items():
        candidate = candidate_by_id[family_id]
        last = local[-1]
        open_position = OpenPosition(
            signal_time_ms=held.position.signal.timestamp_ms,
            entry_time_ms=held.position.entry_time_ms,
            entry_price=held.position.entry_price,
            last_price=last.close,
            unrealized_gross_return_pct=(
                last.close / held.position.entry_price - 1.0
            )
            * 100.0,
            funding_return_pct=held.position.funding_return_pct,
            mae_pct=held.position.mae_pct,
            mfe_pct=held.position.mfe_pct,
            holding_minutes=max(
                0.0,
                (min(last.close_time_ms, window.end_ms - 1) - held.position.entry_time_ms)
                / 60_000.0,
            ),
        )
        open_rows.append(
            {
                "family_id": family_id,
                "candidate_fingerprint_sha256": candidate.fingerprint,
                "slot_no": held.slot_no,
                "open_position": asdict(open_position),
            }
        )

    pending_at_end = [
        {
            "family_id": family_id,
            "slot_no": reservation.slot_no,
            "signal_time_ms": reservation.signal.timestamp_ms,
        }
        for family_id, reservation in pending.items()
    ]
    candidate_rows = []
    for candidate in candidates:
        family_trades = [row.trade for row in closed if row.family_id == candidate.family_id]
        return_sum = sum(trade.net_return_pct for trade in family_trades)
        candidate_rows.append(
            {
                "priority": candidate.priority,
                "family_id": candidate.family_id,
                "candidate_fingerprint_sha256": candidate.fingerprint,
                "raw_signal_count": raw_signals[candidate.family_id],
                "accepted_entry_count": accepted[candidate.family_id],
                "blocked_no_slot_count": blocked_no_slot[candidate.family_id],
                "skipped_team_active_count": skipped_active[candidate.family_id],
                "cancelled_on_gap_count": cancelled_gap[candidate.family_id],
                "closed_trade_count": len(family_trades),
                "closed_trade_net_return_sum_pct": return_sum,
                "open_at_end": any(row["family_id"] == candidate.family_id for row in open_rows),
                "pending_at_end": any(
                    row["family_id"] == candidate.family_id for row in pending_at_end
                ),
            }
        )

    window_duration_ms = max(0, window.end_ms - window.start_ms)
    capacity_ms = window_duration_ms * slot_count
    return_sum = sum(row.trade.net_return_pct for row in closed)
    closed_drawdown = _closed_trade_drawdown(closed, slot_count=slot_count)
    return {
        "phase": phase,
        "name": window.name,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "slot_count": slot_count,
        "raw_signal_count": sum(raw_signals.values()),
        "accepted_entry_count": sum(accepted.values()),
        "blocked_no_slot_count": sum(blocked_no_slot.values()),
        "skipped_team_active_count": sum(skipped_active.values()),
        "cancelled_on_gap_count": sum(cancelled_gap.values()),
        "closed_trade_count": len(closed),
        "closed_trade_net_return_sum_pct": return_sum,
        "fixed_baseline_portfolio_return_pct": return_sum / slot_count,
        "max_fixed_baseline_closed_drawdown_pct": closed_drawdown,
        "slot_utilization_pct": (
            0.0 if capacity_ms == 0 else min(100.0, slot_occupancy_ms / capacity_ms * 100.0)
        ),
        "open_position_count": len(open_rows),
        "pending_entry_count": len(pending_at_end),
        "candidates": candidate_rows,
        "trades": [
            {
                "family_id": row.family_id,
                "candidate_fingerprint_sha256": row.candidate_fingerprint_sha256,
                "slot_no": row.slot_no,
                **asdict(row.trade),
            }
            for row in closed
        ],
        "open_positions": open_rows,
        "pending_entries": pending_at_end,
    }


def _prepare_window(
    window: WindowSpec,
    context: StudyContext,
    candidates: Sequence[_Candidate],
) -> tuple[
    Sequence[Candle],
    Sequence[FundingEvent],
    dict[str, tuple[Signal, ...]],
    dict[str, list[Any]],
]:
    candles = context.candles
    start_index = next(
        (index for index, candle in enumerate(candles) if candle.open_time_ms >= window.start_ms),
        None,
    )
    if start_index is None:
        raise ValueError(f"window {window.name!r} starts after the dataset ends")
    if start_index < context.spec.warmup_candles:
        raise ValueError(
            f"window {window.name!r} has only {start_index} pre-window candles; "
            f"{context.spec.warmup_candles} required"
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

    local = candles[start_index - context.spec.warmup_candles : end_index]
    funding = tuple(
        event
        for event in context.funding
        if local[0].open_time_ms <= event.timestamp_ms < window.end_ms
    )
    signals_by_family: dict[str, tuple[Signal, ...]] = {}
    points_by_family: dict[str, list[Any]] = {}
    for candidate in candidates:
        raw_signals, points = generate_signals(local, candidate.strategy)
        signals_by_family[candidate.family_id] = tuple(
            signal
            for signal in raw_signals
            if window.start_ms <= signal.timestamp_ms < window.end_ms
        )
        points_by_family[candidate.family_id] = points
    return local, funding, signals_by_family, points_by_family


def _closed_trade_drawdown(closed: Sequence[_Closed], *, slot_count: int) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    ordered = sorted(
        closed,
        key=lambda row: (
            row.trade.exit_time_ms,
            row.family_id,
            row.slot_no,
        ),
    )
    for row in ordered:
        cumulative += row.trade.net_return_pct / slot_count
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return drawdown


def _aggregate_portfolio_windows(
    windows: Sequence[dict[str, Any]], *, slot_count: int
) -> dict[str, Any]:
    returns = [float(row["fixed_baseline_portfolio_return_pct"]) for row in windows]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    raw = sum(int(row["raw_signal_count"]) for row in windows)
    blocked = sum(int(row["blocked_no_slot_count"]) for row in windows)
    accepted = sum(int(row["accepted_entry_count"]) for row in windows)
    total_duration = sum(int(row["end_ms"]) - int(row["start_ms"]) for row in windows)
    weighted_utilization = sum(
        float(row["slot_utilization_pct"])
        * (int(row["end_ms"]) - int(row["start_ms"]))
        for row in windows
    )
    return {
        "window_count": len(windows),
        "fixed_baseline_total_return_pct": sum(returns),
        "median_window_return_pct": statistics.median(returns) if returns else 0.0,
        "worst_window_return_pct": min(returns) if returns else 0.0,
        "max_fixed_baseline_window_sequence_drawdown_pct": max_drawdown,
        "worst_within_window_closed_drawdown_pct": (
            min(
                float(row["max_fixed_baseline_closed_drawdown_pct"])
                for row in windows
            )
            if windows
            else 0.0
        ),
        "raw_signal_count": raw,
        "accepted_entry_count": accepted,
        "blocked_no_slot_count": blocked,
        "slot_contention_fraction_of_raw_signals": 0.0 if raw == 0 else blocked / raw,
        "closed_trade_count": sum(int(row["closed_trade_count"]) for row in windows),
        "open_at_end_windows": sum(int(row["open_position_count"]) > 0 for row in windows),
        "pending_at_end_windows": sum(int(row["pending_entry_count"]) > 0 for row in windows),
        "weighted_slot_utilization_pct": (
            0.0 if total_duration == 0 else weighted_utilization / total_duration
        ),
        "slot_count": slot_count,
    }


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc
