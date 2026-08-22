from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .manifest import sha256_file
from .models import StrategySpec
from .portfolio import (
    PortfolioSpec,
    _Candidate,
    _aggregate_portfolio_windows,
    _run_portfolio_window,
    portfolio_fingerprint,
    verify_families_result,
)
from .promotion import candidate_fingerprint
from .study import StudyContext, load_study_context, study_fingerprint

SELECTION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SelectionGates:
    min_discovery_marginal_return_pct: float
    min_validation_marginal_return_pct: float
    min_validation_accepted_entries: int
    max_validation_drawdown_worsening_pct: float | None = None
    max_validation_contention_added_fraction: float | None = None

    def __post_init__(self) -> None:
        if self.min_validation_accepted_entries < 0:
            raise ValueError("min_validation_accepted_entries cannot be negative")
        if (
            self.max_validation_drawdown_worsening_pct is not None
            and self.max_validation_drawdown_worsening_pct < 0.0
        ):
            raise ValueError("max_validation_drawdown_worsening_pct cannot be negative")
        if self.max_validation_contention_added_fraction is not None and not (
            0.0 <= self.max_validation_contention_added_fraction <= 1.0
        ):
            raise ValueError("max_validation_contention_added_fraction must be inside [0, 1]")


@dataclass(frozen=True, slots=True)
class SelectionSpec:
    name: str
    source_portfolio_result_sha256: str
    gates: SelectionGates

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("selection name cannot be empty")
        _validate_digest(self.source_portfolio_result_sha256)


def load_selection_json(path: str | Path) -> SelectionSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SELECTION_SCHEMA_VERSION:
        raise ValueError("unsupported selection schema_version")
    gates = payload["gates"]
    return SelectionSpec(
        name=str(payload["name"]),
        source_portfolio_result_sha256=str(
            payload["source_portfolio_result_sha256"]
        ).lower(),
        gates=SelectionGates(
            min_discovery_marginal_return_pct=float(
                gates["min_discovery_marginal_return_pct"]
            ),
            min_validation_marginal_return_pct=float(
                gates["min_validation_marginal_return_pct"]
            ),
            min_validation_accepted_entries=int(gates["min_validation_accepted_entries"]),
            max_validation_drawdown_worsening_pct=_optional_float(
                gates.get("max_validation_drawdown_worsening_pct")
            ),
            max_validation_contention_added_fraction=_optional_float(
                gates.get("max_validation_contention_added_fraction")
            ),
        ),
    )


def selection_fingerprint(spec: SelectionSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_selection(
    study_path: str | Path,
    families_result_path: str | Path,
    portfolio_result_path: str | Path,
    selection_path: str | Path,
    *,
    data_directory: str | Path,
) -> dict[str, Any]:
    context = load_study_context(study_path, data_directory=data_directory)
    families_path = Path(families_result_path)
    families_result = json.loads(families_path.read_text(encoding="utf-8"))
    portfolio_path = Path(portfolio_result_path)
    portfolio_result = json.loads(portfolio_path.read_text(encoding="utf-8"))
    spec = load_selection_json(selection_path)
    actual_portfolio_sha = sha256_file(portfolio_path)
    if spec.source_portfolio_result_sha256 != actual_portfolio_sha:
        raise ValueError("selection contract is pinned to a different portfolio-result file")

    family_problems = verify_families_result(families_result)
    if family_problems:
        raise ValueError("families-result verification failed: " + "; ".join(family_problems))
    portfolio_problems = verify_portfolio_result(portfolio_result)
    if portfolio_problems:
        raise ValueError("portfolio-result verification failed: " + "; ".join(portfolio_problems))

    actual_families_sha = sha256_file(families_path)
    if portfolio_result.get("source_families_result_sha256") != actual_families_sha:
        raise ValueError("portfolio result is pinned to a different families-result file")
    if portfolio_result.get("study_fingerprint_sha256") != study_fingerprint(context.spec):
        raise ValueError("portfolio result belongs to a different study contract")
    if portfolio_result.get("dataset_fingerprint_sha256") != context.spec.dataset_fingerprint_sha256:
        raise ValueError("portfolio result belongs to a different dataset")
    if portfolio_result.get("symbol") != context.spec.symbol:
        raise ValueError("portfolio-result symbol does not match the study")
    if portfolio_result.get("execution") != asdict(context.spec.execution):
        raise ValueError("portfolio-result execution assumptions do not match the study")

    portfolio_spec = _portfolio_spec_from_result(portfolio_result)
    representatives = {
        row["family_id"]: row for row in families_result["representatives"]
    }
    candidates = tuple(
        _Candidate(
            family_id=family_id,
            fingerprint=representatives[family_id]["candidate_fingerprint_sha256"],
            strategy=StrategySpec(**representatives[family_id]["strategy"]),
            priority=index + 1,
        )
        for index, family_id in enumerate(portfolio_spec.priority_family_ids)
    )

    full_windows = _replay_candidate_set(
        context,
        candidates,
        slot_count=portfolio_spec.slot_count,
    )
    full_aggregate = _phase_aggregates(
        full_windows,
        slot_count=portfolio_spec.slot_count,
    )
    _verify_replayed_full_matches_source(portfolio_result, full_windows, full_aggregate)

    marginal_rows: list[dict[str, Any]] = []
    selected: list[_Candidate] = []
    rejected: list[_Candidate] = []
    for candidate in candidates:
        without = tuple(row for row in candidates if row.family_id != candidate.family_id)
        leaveout_windows = _replay_candidate_set(
            context,
            without,
            slot_count=portfolio_spec.slot_count,
        )
        leaveout_aggregate = _phase_aggregates(
            leaveout_windows,
            slot_count=portfolio_spec.slot_count,
        )
        evidence = _marginal_evidence(
            candidate,
            full_windows,
            full_aggregate,
            leaveout_aggregate,
        )
        failures = _selection_failures(evidence, spec.gates)
        status = "KEEP" if not failures else "DROP"
        if status == "KEEP":
            selected.append(candidate)
        else:
            rejected.append(candidate)
        marginal_rows.append(
            {
                "family_id": candidate.family_id,
                "candidate_fingerprint_sha256": candidate.fingerprint,
                "strategy": asdict(candidate.strategy),
                "original_priority": candidate.priority,
                "status": status,
                "failure_reasons": failures,
                "marginal": evidence,
            }
        )

    if not selected:
        raise ValueError("selection gates dropped every family representative")

    # Preserve original relative priority. No reordering occurs after marginal evidence.
    selected = sorted(selected, key=lambda row: row.priority)
    selected_windows = _replay_candidate_set(
        context,
        tuple(selected),
        slot_count=portfolio_spec.slot_count,
    )
    selected_aggregates = _phase_aggregates(
        selected_windows,
        slot_count=portfolio_spec.slot_count,
    )

    selection_set = [
        {
            "priority": index + 1,
            "original_priority": row.priority,
            "family_id": row.family_id,
            "candidate_fingerprint_sha256": row.fingerprint,
            "strategy": asdict(row.strategy),
        }
        for index, row in enumerate(selected)
    ]
    selection_set_fingerprint = _selection_set_fingerprint(
        source_portfolio_sha=actual_portfolio_sha,
        slot_count=portfolio_spec.slot_count,
        selected=selection_set,
    )
    return {
        "schema_version": 1,
        "kind": "parameter_extract.portfolio_selection",
        "selection": spec.name,
        "selection_fingerprint_sha256": selection_fingerprint(spec),
        "selection_spec": asdict(spec),
        "source_portfolio_result_sha256": actual_portfolio_sha,
        "source_families_result_sha256": actual_families_sha,
        "study_fingerprint_sha256": study_fingerprint(context.spec),
        "dataset_fingerprint_sha256": context.spec.dataset_fingerprint_sha256,
        "symbol": context.spec.symbol,
        "execution": asdict(context.spec.execution),
        "strategy_parameters_retuned": False,
        "priority_reoptimized": False,
        "selection_algorithm": "one_pass_full_portfolio_leave_one_out_v1",
        "iterative_subset_search": False,
        "leverage_applied": False,
        "discovery_accessed": True,
        "validation_accessed": True,
        "holdout_accessed": False,
        "slot_count": portfolio_spec.slot_count,
        "source_representative_count": len(candidates),
        "selected_count": len(selected),
        "dropped_count": len(rejected),
        "selected_set_fingerprint_sha256": selection_set_fingerprint,
        "selected": selection_set,
        "marginal_evidence": marginal_rows,
        "full_portfolio_phase_aggregates": full_aggregate,
        "selected_portfolio_phase_aggregates": selected_aggregates,
        "selected_portfolio_windows": selected_windows,
    }


def verify_portfolio_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported portfolio-result schema_version")
    if payload.get("kind") != "parameter_extract.portfolio_replay":
        problems.append("portfolio-result kind is invalid")
    if payload.get("strategy_parameters_retuned") is not False:
        problems.append("portfolio result does not prove frozen strategy parameters")
    if payload.get("priority_optimized") is not False:
        problems.append("portfolio result indicates priority optimization")
    if payload.get("priority_source") != "explicit_portfolio_contract":
        problems.append("portfolio priority source is unsupported")
    if payload.get("leverage_applied") is not False:
        problems.append("portfolio result unexpectedly applies leverage")
    if payload.get("holdout_accessed") is not False:
        problems.append("portfolio result indicates holdout access")
    if payload.get("discovery_accessed") is not True:
        problems.append("portfolio result does not indicate discovery access")
    if payload.get("validation_accessed") is not True:
        problems.append("portfolio result does not indicate validation access")

    try:
        spec = _portfolio_spec_from_result(payload)
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"portfolio_spec is invalid: {exc}")
        return problems
    if payload.get("portfolio_fingerprint_sha256") != portfolio_fingerprint(spec):
        problems.append("portfolio fingerprint does not match portfolio_spec")
    if payload.get("source_families_result_sha256") != spec.source_families_result_sha256:
        problems.append("portfolio source families-result SHA is inconsistent")
    if payload.get("slot_count") != spec.slot_count:
        problems.append("slot_count is inconsistent with portfolio_spec")

    priorities = payload.get("priorities")
    if not isinstance(priorities, list):
        problems.append("portfolio priorities are missing or invalid")
        return problems
    if payload.get("representative_count") != len(priorities):
        problems.append("representative_count does not match priorities")
    if [row.get("family_id") for row in priorities] != list(spec.priority_family_ids):
        problems.append("priority rows do not match portfolio contract order")
    seen: set[str] = set()
    for index, row in enumerate(priorities):
        try:
            strategy = StrategySpec(**row["strategy"])
            fingerprint = candidate_fingerprint(strategy)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"priority {index}: invalid strategy: {exc}")
            continue
        if row.get("priority") != index + 1:
            problems.append(f"priority {index}: numeric priority is inconsistent")
        if row.get("candidate_fingerprint_sha256") != fingerprint:
            problems.append(f"priority {index}: strategy fingerprint mismatch")
        if fingerprint in seen:
            problems.append(f"priority {index}: duplicate candidate fingerprint")
        seen.add(fingerprint)

    windows = payload.get("windows")
    if not isinstance(windows, list):
        problems.append("portfolio windows are missing or invalid")
        return problems
    phases = {row.get("phase") for row in windows if isinstance(row, dict)}
    if phases - {"discovery", "validation"}:
        problems.append("portfolio windows include an unauthorized phase")
    try:
        recomputed = _phase_aggregates(windows, slot_count=spec.slot_count)
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"portfolio window aggregates are invalid: {exc}")
        return problems
    if payload.get("phase_aggregates") != recomputed:
        problems.append("phase_aggregates do not match stored windows")
    combined = _aggregate_portfolio_windows(windows, slot_count=spec.slot_count)
    if payload.get("aggregate") != combined:
        problems.append("aggregate does not match stored windows")
    return problems


def _portfolio_spec_from_result(payload: dict[str, Any]) -> PortfolioSpec:
    spec_payload = payload["portfolio_spec"]
    return PortfolioSpec(
        name=str(spec_payload["name"]),
        source_families_result_sha256=str(
            spec_payload["source_families_result_sha256"]
        ),
        slot_count=int(spec_payload["slot_count"]),
        priority_family_ids=tuple(str(value) for value in spec_payload["priority_family_ids"]),
    )


def _replay_candidate_set(
    context: StudyContext,
    candidates: Sequence[_Candidate],
    *,
    slot_count: int,
) -> list[dict[str, Any]]:
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
                    slot_count=slot_count,
                )
            )
    return windows


def _phase_aggregates(
    windows: Sequence[dict[str, Any]], *, slot_count: int
) -> dict[str, dict[str, Any]]:
    return {
        phase: _aggregate_portfolio_windows(
            [row for row in windows if row["phase"] == phase],
            slot_count=slot_count,
        )
        for phase in ("discovery", "validation")
    }


def _verify_replayed_full_matches_source(
    source: dict[str, Any],
    windows: Sequence[dict[str, Any]],
    phase_aggregates: dict[str, dict[str, Any]],
) -> None:
    if source.get("windows") != list(windows):
        raise ValueError("fresh full-portfolio replay does not reproduce source windows")
    if source.get("phase_aggregates") != phase_aggregates:
        raise ValueError("fresh full-portfolio replay does not reproduce source phase aggregates")


def _marginal_evidence(
    candidate: _Candidate,
    full_windows: Sequence[dict[str, Any]],
    full: dict[str, dict[str, Any]],
    leaveout: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validation_rows = [
        candidate_row
        for window in full_windows
        if window["phase"] == "validation"
        for candidate_row in window["candidates"]
    ]
    candidate_rows = [
        row for row in validation_rows if row["family_id"] == candidate.family_id
    ]
    other_rows = [
        row for row in validation_rows if row["family_id"] != candidate.family_id
    ]
    validation_accepted = sum(int(row["accepted_entry_count"]) for row in candidate_rows)
    validation_blocked = sum(int(row["blocked_no_slot_count"]) for row in candidate_rows)
    validation_raw = sum(int(row["raw_signal_count"]) for row in candidate_rows)
    other_blocked_with_candidate = sum(
        int(row["blocked_no_slot_count"]) for row in other_rows
    )
    other_raw = sum(int(row["raw_signal_count"]) for row in other_rows)

    full_validation = full["validation"]
    leaveout_validation = leaveout["validation"]
    full_discovery = full["discovery"]
    leaveout_discovery = leaveout["discovery"]
    drawdown_worsening = max(
        0.0,
        float(leaveout_validation["worst_within_window_closed_drawdown_pct"])
        - float(full_validation["worst_within_window_closed_drawdown_pct"]),
    )
    other_blocked_without_candidate = int(leaveout_validation["blocked_no_slot_count"])
    contention_added_count = max(
        0,
        other_blocked_with_candidate - other_blocked_without_candidate,
    )
    contention_added_fraction = (
        0.0 if other_raw == 0 else contention_added_count / other_raw
    )
    return {
        "discovery_marginal_return_pct": float(
            full_discovery["fixed_baseline_total_return_pct"]
        )
        - float(leaveout_discovery["fixed_baseline_total_return_pct"]),
        "validation_marginal_return_pct": float(
            full_validation["fixed_baseline_total_return_pct"]
        )
        - float(leaveout_validation["fixed_baseline_total_return_pct"]),
        "validation_accepted_entries": validation_accepted,
        "validation_candidate_blocked_no_slot_count": validation_blocked,
        "validation_candidate_raw_signal_count": validation_raw,
        "validation_drawdown_worsening_pct": drawdown_worsening,
        "validation_other_family_raw_signal_count": other_raw,
        "validation_other_family_blocked_with_candidate_count": (
            other_blocked_with_candidate
        ),
        "validation_other_family_blocked_without_candidate_count": (
            other_blocked_without_candidate
        ),
        "validation_contention_added_count": contention_added_count,
        "validation_contention_added_fraction": contention_added_fraction,
        "full_validation_return_pct": full_validation["fixed_baseline_total_return_pct"],
        "leaveout_validation_return_pct": leaveout_validation[
            "fixed_baseline_total_return_pct"
        ],
    }


def _selection_failures(
    evidence: dict[str, Any], gates: SelectionGates
) -> list[str]:
    failures: list[str] = []
    if (
        evidence["discovery_marginal_return_pct"]
        < gates.min_discovery_marginal_return_pct
    ):
        failures.append("DISCOVERY_MARGINAL_RETURN")
    if (
        evidence["validation_marginal_return_pct"]
        < gates.min_validation_marginal_return_pct
    ):
        failures.append("VALIDATION_MARGINAL_RETURN")
    if evidence["validation_accepted_entries"] < gates.min_validation_accepted_entries:
        failures.append("VALIDATION_ACCEPTED_ENTRIES")
    if (
        gates.max_validation_drawdown_worsening_pct is not None
        and evidence["validation_drawdown_worsening_pct"]
        > gates.max_validation_drawdown_worsening_pct
    ):
        failures.append("VALIDATION_DRAWDOWN_WORSENING")
    if (
        gates.max_validation_contention_added_fraction is not None
        and evidence["validation_contention_added_fraction"]
        > gates.max_validation_contention_added_fraction
    ):
        failures.append("VALIDATION_CONTENTION_ADDED")
    return failures


def _selection_set_fingerprint(
    *,
    source_portfolio_sha: str,
    slot_count: int,
    selected: Sequence[dict[str, Any]],
) -> str:
    stable = {
        "schema_version": 1,
        "kind": "parameter_extract.selected_portfolio_set",
        "source_portfolio_result_sha256": source_portfolio_sha,
        "slot_count": slot_count,
        "selected": list(selected),
    }
    canonical = json.dumps(
        stable, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc
