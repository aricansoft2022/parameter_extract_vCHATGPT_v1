from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .manifest import sha256_file
from .risk import verify_risk_result

EXCHANGE_SNAPSHOT_SCHEMA_VERSION = 1
EXCHANGE_RISK_SCHEMA_VERSION = 1
EXCHANGE_RISK_METHOD = "binance_usdm_isolated_long_bracket_liquidation_v1"


@dataclass(frozen=True, slots=True)
class Bracket:
    bracket: int
    initial_leverage: int
    notional_floor: float
    notional_cap: float
    maint_margin_ratio: float
    cum: float

    def __post_init__(self) -> None:
        if self.bracket < 1:
            raise ValueError("bracket id must be positive")
        if not 1 <= self.initial_leverage <= 125:
            raise ValueError("initial_leverage must be inside [1, 125]")
        for name in ("notional_floor", "notional_cap", "maint_margin_ratio", "cum"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.notional_floor < 0.0 or self.notional_cap <= self.notional_floor:
            raise ValueError("notional bracket bounds are invalid")
        if not 0.0 <= self.maint_margin_ratio < 1.0:
            raise ValueError("maint_margin_ratio must be inside [0, 1)")
        if self.cum < 0.0:
            raise ValueError("cum cannot be negative")


@dataclass(frozen=True, slots=True)
class LiquidationParityCase:
    name: str
    entry_price: float
    position_amt: float
    isolated_wallet: float
    reported_liquidation_price: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("parity case name cannot be empty")
        for name in (
            "entry_price",
            "position_amt",
            "isolated_wallet",
            "reported_liquidation_price",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class ExchangeSnapshot:
    captured_at_utc: str
    source: str
    symbol: str
    margin_asset: str
    margin_type: str
    position_mode: str
    auto_add_margin: bool
    notional_coef: float | None
    brackets: tuple[Bracket, ...]
    liquidation_parity_cases: tuple[LiquidationParityCase, ...]

    def __post_init__(self) -> None:
        if not self.captured_at_utc.strip() or not self.source.strip():
            raise ValueError("snapshot provenance cannot be empty")
        if not self.symbol.isupper() or not self.symbol.endswith("USDT"):
            raise ValueError("snapshot symbol must be an upper-case USDT pair")
        if self.margin_asset != "USDT":
            raise ValueError("V1 supports USDT margin only")
        if self.margin_type != "ISOLATED":
            raise ValueError("V1 supports isolated margin only")
        if self.position_mode != "ONE_WAY":
            raise ValueError("V1 supports one-way mode only")
        if self.auto_add_margin:
            raise ValueError("auto-add-margin must be disabled for deterministic V1")
        if self.notional_coef is not None:
            if not math.isfinite(self.notional_coef) or self.notional_coef <= 0.0:
                raise ValueError("notional_coef must be finite and positive")
        if not self.brackets:
            raise ValueError("snapshot requires leverage brackets")
        _validate_bracket_ladder(self.brackets)


@dataclass(frozen=True, slots=True)
class ExchangeRiskSpec:
    name: str
    source_risk_result_sha256: str
    source_exchange_snapshot_sha256: str
    baseline_capital_min_usdt: float
    baseline_capital_max_usdt: float
    isolated_wallet_haircut_pct: float
    min_liquidation_headroom_over_required_budget_pct: float
    min_parity_cases: int
    max_parity_error_bps: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("exchange-risk name cannot be empty")
        _validate_digest(self.source_risk_result_sha256)
        _validate_digest(self.source_exchange_snapshot_sha256)
        for name in (
            "baseline_capital_min_usdt",
            "baseline_capital_max_usdt",
            "isolated_wallet_haircut_pct",
            "min_liquidation_headroom_over_required_budget_pct",
            "max_parity_error_bps",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.baseline_capital_min_usdt <= 0.0:
            raise ValueError("baseline_capital_min_usdt must be positive")
        if self.baseline_capital_max_usdt < self.baseline_capital_min_usdt:
            raise ValueError("baseline capital range is invalid")
        if not 0.0 <= self.isolated_wallet_haircut_pct < 100.0:
            raise ValueError("isolated_wallet_haircut_pct must be inside [0, 100)")
        if self.min_liquidation_headroom_over_required_budget_pct < 0.0:
            raise ValueError("minimum liquidation headroom cannot be negative")
        if self.min_parity_cases < 1:
            raise ValueError("min_parity_cases must be positive")
        if self.max_parity_error_bps < 0.0:
            raise ValueError("max_parity_error_bps cannot be negative")


def load_exchange_snapshot(path: str | Path) -> ExchangeSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != EXCHANGE_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported exchange snapshot schema_version")
    return ExchangeSnapshot(
        captured_at_utc=str(payload["captured_at_utc"]),
        source=str(payload["source"]),
        symbol=str(payload["symbol"]),
        margin_asset=str(payload["margin_asset"]),
        margin_type=str(payload["margin_type"]),
        position_mode=str(payload["position_mode"]),
        auto_add_margin=bool(payload["auto_add_margin"]),
        notional_coef=_optional_float(payload.get("notional_coef")),
        brackets=tuple(
            Bracket(
                bracket=int(row["bracket"]),
                initial_leverage=int(row["initialLeverage"]),
                notional_floor=float(row["notionalFloor"]),
                notional_cap=float(row["notionalCap"]),
                maint_margin_ratio=float(row["maintMarginRatio"]),
                cum=float(row["cum"]),
            )
            for row in payload["brackets"]
        ),
        liquidation_parity_cases=tuple(
            LiquidationParityCase(
                name=str(row["name"]),
                entry_price=float(row["entry_price"]),
                position_amt=float(row["position_amt"]),
                isolated_wallet=float(row["isolated_wallet"]),
                reported_liquidation_price=float(row["reported_liquidation_price"]),
            )
            for row in payload.get("liquidation_parity_cases", [])
        ),
    )


def load_exchange_risk_json(path: str | Path) -> ExchangeRiskSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != EXCHANGE_RISK_SCHEMA_VERSION:
        raise ValueError("unsupported exchange-risk schema_version")
    return ExchangeRiskSpec(
        name=str(payload["name"]),
        source_risk_result_sha256=str(payload["source_risk_result_sha256"]).lower(),
        source_exchange_snapshot_sha256=str(
            payload["source_exchange_snapshot_sha256"]
        ).lower(),
        baseline_capital_min_usdt=float(payload["baseline_capital_min_usdt"]),
        baseline_capital_max_usdt=float(payload["baseline_capital_max_usdt"]),
        isolated_wallet_haircut_pct=float(payload["isolated_wallet_haircut_pct"]),
        min_liquidation_headroom_over_required_budget_pct=float(
            payload["min_liquidation_headroom_over_required_budget_pct"]
        ),
        min_parity_cases=int(payload["min_parity_cases"]),
        max_parity_error_bps=float(payload["max_parity_error_bps"]),
    )


def exchange_snapshot_fingerprint(snapshot: ExchangeSnapshot) -> str:
    canonical = json.dumps(
        asdict(snapshot), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def exchange_risk_fingerprint(spec: ExchangeRiskSpec) -> str:
    canonical = json.dumps(
        asdict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_exchange_risk(
    risk_result_path: str | Path,
    exchange_snapshot_path: str | Path,
    exchange_risk_path: str | Path,
) -> dict[str, Any]:
    risk_path = Path(risk_result_path)
    snapshot_path = Path(exchange_snapshot_path)
    risk = json.loads(risk_path.read_text(encoding="utf-8"))
    spec = load_exchange_risk_json(exchange_risk_path)
    snapshot = load_exchange_snapshot(snapshot_path)

    risk_problems = verify_risk_result(risk)
    if risk_problems:
        raise ValueError("risk-result verification failed: " + "; ".join(risk_problems))
    if risk.get("status") != "RISK_BUDGET_PASS":
        raise ValueError("exchange risk requires RISK_BUDGET_PASS")

    actual_risk_sha = sha256_file(risk_path)
    actual_snapshot_sha = sha256_file(snapshot_path)
    if spec.source_risk_result_sha256 != actual_risk_sha:
        raise ValueError("exchange-risk contract is pinned to a different risk-result file")
    if spec.source_exchange_snapshot_sha256 != actual_snapshot_sha:
        raise ValueError("exchange-risk contract is pinned to a different exchange snapshot")
    if snapshot.symbol != risk.get("symbol"):
        raise ValueError("exchange snapshot symbol does not match risk result")

    leverage = risk.get("summary", {}).get("provisional_deployment_leverage")
    if not isinstance(leverage, int) or leverage < 1:
        raise ValueError("risk result has no positive provisional deployment leverage")
    allocation_pct = float(risk["allocation_pct"])
    reserve_pct = float(risk["reserve_pct"])
    required_budget_pct = float(risk["summary"]["required_adverse_budget_pct"])

    parity = _evaluate_parity(snapshot)
    parity_failures: list[str] = []
    if parity["case_count"] < spec.min_parity_cases:
        parity_failures.append("INSUFFICIENT_LIQUIDATION_PARITY_CASES")
    if (
        parity["max_error_bps"] is None
        or parity["max_error_bps"] > spec.max_parity_error_bps
    ):
        parity_failures.append("LIQUIDATION_MODEL_PARITY")

    capital_points = _capital_test_points(
        spec,
        snapshot.brackets,
        leverage=leverage,
        allocation_pct=allocation_pct,
        reserve_pct=reserve_pct,
    )
    scenarios = [
        _deployment_scenario(
            baseline_capital_usdt=capital,
            leverage=leverage,
            allocation_pct=allocation_pct,
            reserve_pct=reserve_pct,
            wallet_haircut_pct=spec.isolated_wallet_haircut_pct,
            brackets=snapshot.brackets,
        )
        for capital in capital_points
    ]
    worst = min(scenarios, key=lambda row: row["liquidation_distance_pct"])
    headroom = worst["liquidation_distance_pct"] - required_budget_pct

    failures = list(parity_failures)
    if any(not row["initial_leverage_allowed"] for row in scenarios):
        failures.append("INITIAL_LEVERAGE_NOT_ALLOWED_FOR_NOTIONAL")
    if headroom < spec.min_liquidation_headroom_over_required_budget_pct:
        failures.append("INSUFFICIENT_LIQUIDATION_HEADROOM")
    status = "EXCHANGE_RISK_PASS" if not failures else "BLOCK"

    payload = {
        "schema_version": 1,
        "kind": "parameter_extract.exchange_risk",
        "exchange_risk": spec.name,
        "exchange_risk_fingerprint_sha256": exchange_risk_fingerprint(spec),
        "exchange_risk_spec": asdict(spec),
        "source_risk_result_sha256": actual_risk_sha,
        "source_exchange_snapshot_sha256": actual_snapshot_sha,
        "exchange_snapshot_fingerprint_sha256": exchange_snapshot_fingerprint(snapshot),
        "snapshot": asdict(snapshot),
        "source_selected_set_fingerprint_sha256": risk[
            "source_selected_set_fingerprint_sha256"
        ],
        "symbol": risk["symbol"],
        "slot_count": risk["slot_count"],
        "allocation_pct": allocation_pct,
        "reserve_pct": reserve_pct,
        "provisional_deployment_leverage": leverage,
        "risk_required_adverse_budget_pct": required_budget_pct,
        "exchange_risk_method": EXCHANGE_RISK_METHOD,
        "alpha_parameters_retuned": False,
        "selected_set_changed": False,
        "priority_reoptimized": False,
        "leverage_optimized": False,
        "exchange_liquidation_validated": status == "EXCHANGE_RISK_PASS",
        "teams_export_ready": status == "EXCHANGE_RISK_PASS",
        "status": status,
        "failure_reasons": failures,
        "liquidation_parity": parity,
        "capital_test_points_usdt": capital_points,
        "deployment_scenarios": scenarios,
        "worst_case": worst,
        "liquidation_headroom_over_required_budget_pct": headroom,
    }
    problems = verify_exchange_risk_result(payload)
    if problems:
        raise RuntimeError(
            "generated exchange-risk result failed self-verification: "
            + "; ".join(problems)
        )
    return payload


def verify_exchange_risk_result(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported exchange-risk result schema_version")
    if payload.get("kind") != "parameter_extract.exchange_risk":
        problems.append("exchange-risk result kind is invalid")
    if payload.get("exchange_risk_method") != EXCHANGE_RISK_METHOD:
        problems.append("exchange-risk method is unsupported")
    for flag in (
        "alpha_parameters_retuned",
        "selected_set_changed",
        "priority_reoptimized",
        "leverage_optimized",
    ):
        if payload.get(flag) is not False:
            problems.append(f"{flag} must be false")

    try:
        spec_payload = payload["exchange_risk_spec"]
        spec = ExchangeRiskSpec(**spec_payload)
        if payload.get("exchange_risk_fingerprint_sha256") != exchange_risk_fingerprint(spec):
            problems.append("exchange-risk fingerprint does not match spec")
        snapshot_payload = payload["snapshot"]
        snapshot = ExchangeSnapshot(
            captured_at_utc=str(snapshot_payload["captured_at_utc"]),
            source=str(snapshot_payload["source"]),
            symbol=str(snapshot_payload["symbol"]),
            margin_asset=str(snapshot_payload["margin_asset"]),
            margin_type=str(snapshot_payload["margin_type"]),
            position_mode=str(snapshot_payload["position_mode"]),
            auto_add_margin=bool(snapshot_payload["auto_add_margin"]),
            notional_coef=_optional_float(snapshot_payload.get("notional_coef")),
            brackets=tuple(Bracket(**row) for row in snapshot_payload["brackets"]),
            liquidation_parity_cases=tuple(
                LiquidationParityCase(**row)
                for row in snapshot_payload["liquidation_parity_cases"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"exchange-risk spec/snapshot is invalid: {exc}")
        return problems

    if payload.get("exchange_snapshot_fingerprint_sha256") != exchange_snapshot_fingerprint(snapshot):
        problems.append("exchange snapshot fingerprint does not match snapshot")
    if payload.get("source_risk_result_sha256") != spec.source_risk_result_sha256:
        problems.append("source risk-result SHA is inconsistent")
    if payload.get("source_exchange_snapshot_sha256") != spec.source_exchange_snapshot_sha256:
        problems.append("source exchange snapshot SHA is inconsistent")

    leverage = payload.get("provisional_deployment_leverage")
    if not isinstance(leverage, int) or leverage < 1:
        problems.append("provisional deployment leverage is invalid")
        return problems
    allocation_pct = float(payload.get("allocation_pct"))
    reserve_pct = float(payload.get("reserve_pct"))
    required_budget_pct = float(payload.get("risk_required_adverse_budget_pct"))

    parity = _evaluate_parity(snapshot)
    if payload.get("liquidation_parity") != parity:
        problems.append("liquidation parity does not match snapshot fixtures")
    points = _capital_test_points(
        spec,
        snapshot.brackets,
        leverage=leverage,
        allocation_pct=allocation_pct,
        reserve_pct=reserve_pct,
    )
    if payload.get("capital_test_points_usdt") != points:
        problems.append("capital test points do not match policy and brackets")
    scenarios = [
        _deployment_scenario(
            baseline_capital_usdt=capital,
            leverage=leverage,
            allocation_pct=allocation_pct,
            reserve_pct=reserve_pct,
            wallet_haircut_pct=spec.isolated_wallet_haircut_pct,
            brackets=snapshot.brackets,
        )
        for capital in points
    ]
    if payload.get("deployment_scenarios") != scenarios:
        problems.append("deployment scenarios do not recompute")
    worst = min(scenarios, key=lambda row: row["liquidation_distance_pct"])
    if payload.get("worst_case") != worst:
        problems.append("worst-case exchange scenario does not recompute")
    headroom = worst["liquidation_distance_pct"] - required_budget_pct
    if payload.get("liquidation_headroom_over_required_budget_pct") != headroom:
        problems.append("liquidation headroom does not recompute")

    failures: list[str] = []
    if parity["case_count"] < spec.min_parity_cases:
        failures.append("INSUFFICIENT_LIQUIDATION_PARITY_CASES")
    if parity["max_error_bps"] is None or parity["max_error_bps"] > spec.max_parity_error_bps:
        failures.append("LIQUIDATION_MODEL_PARITY")
    if any(not row["initial_leverage_allowed"] for row in scenarios):
        failures.append("INITIAL_LEVERAGE_NOT_ALLOWED_FOR_NOTIONAL")
    if headroom < spec.min_liquidation_headroom_over_required_budget_pct:
        failures.append("INSUFFICIENT_LIQUIDATION_HEADROOM")
    expected_status = "EXCHANGE_RISK_PASS" if not failures else "BLOCK"
    if payload.get("failure_reasons") != failures:
        problems.append("exchange-risk failure reasons do not recompute")
    if payload.get("status") != expected_status:
        problems.append("exchange-risk status does not recompute")
    expected_ready = expected_status == "EXCHANGE_RISK_PASS"
    if payload.get("exchange_liquidation_validated") is not expected_ready:
        problems.append("exchange_liquidation_validated does not match status")
    if payload.get("teams_export_ready") is not expected_ready:
        problems.append("teams_export_ready does not match status")
    return problems


def solve_isolated_long_liquidation_price(
    *,
    entry_price: float,
    position_amt: float,
    isolated_wallet: float,
    brackets: Sequence[Bracket],
) -> tuple[float, Bracket]:
    if entry_price <= 0.0 or position_amt <= 0.0 or isolated_wallet <= 0.0:
        raise ValueError("long liquidation inputs must be positive")
    candidates: list[tuple[float, Bracket]] = []
    for bracket in brackets:
        denominator = position_amt * (1.0 - bracket.maint_margin_ratio)
        if denominator <= 0.0:
            continue
        price = (
            position_amt * entry_price - isolated_wallet - bracket.cum
        ) / denominator
        if price <= 0.0 or not math.isfinite(price):
            continue
        notional = position_amt * price
        if _notional_in_bracket(notional, bracket, brackets):
            candidates.append((price, bracket))
    if len(candidates) != 1:
        raise ValueError(
            f"liquidation equation resolved to {len(candidates)} bracket-consistent prices"
        )
    return candidates[0]


def maintenance_margin(notional: float, bracket: Bracket) -> float:
    if notional < 0.0:
        raise ValueError("notional cannot be negative")
    return notional * bracket.maint_margin_ratio - bracket.cum


def _evaluate_parity(snapshot: ExchangeSnapshot) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[float] = []
    for case in snapshot.liquidation_parity_cases:
        calculated, bracket = solve_isolated_long_liquidation_price(
            entry_price=case.entry_price,
            position_amt=case.position_amt,
            isolated_wallet=case.isolated_wallet,
            brackets=snapshot.brackets,
        )
        error_bps = (
            abs(calculated - case.reported_liquidation_price)
            / case.reported_liquidation_price
            * 10_000.0
        )
        errors.append(error_bps)
        rows.append(
            {
                "name": case.name,
                "reported_liquidation_price": case.reported_liquidation_price,
                "calculated_liquidation_price": calculated,
                "absolute_error_bps": error_bps,
                "liquidation_bracket": bracket.bracket,
            }
        )
    return {
        "case_count": len(rows),
        "max_error_bps": max(errors) if errors else None,
        "median_error_bps": _median(errors),
        "cases": rows,
    }


def _deployment_scenario(
    *,
    baseline_capital_usdt: float,
    leverage: int,
    allocation_pct: float,
    reserve_pct: float,
    wallet_haircut_pct: float,
    brackets: Sequence[Bracket],
) -> dict[str, Any]:
    slot_margin = (
        baseline_capital_usdt
        * allocation_pct
        / 100.0
        * (1.0 - reserve_pct / 100.0)
    )
    planned_notional = slot_margin * leverage
    entry_price = 1.0
    position_amt = planned_notional / entry_price
    isolated_wallet = slot_margin * (1.0 - wallet_haircut_pct / 100.0)
    entry_bracket = _bracket_for_notional(planned_notional, brackets)
    liquidation_price, liquidation_bracket = solve_isolated_long_liquidation_price(
        entry_price=entry_price,
        position_amt=position_amt,
        isolated_wallet=isolated_wallet,
        brackets=brackets,
    )
    distance = (entry_price - liquidation_price) / entry_price * 100.0
    return {
        "baseline_capital_usdt": baseline_capital_usdt,
        "slot_margin_usdt": slot_margin,
        "planned_notional_usdt": planned_notional,
        "leverage": leverage,
        "entry_bracket": entry_bracket.bracket,
        "entry_bracket_initial_leverage": entry_bracket.initial_leverage,
        "initial_leverage_allowed": leverage <= entry_bracket.initial_leverage,
        "isolated_wallet_after_haircut_usdt": isolated_wallet,
        "liquidation_price_ratio_to_entry": liquidation_price,
        "liquidation_distance_pct": distance,
        "liquidation_bracket": liquidation_bracket.bracket,
    }


def _capital_test_points(
    spec: ExchangeRiskSpec,
    brackets: Sequence[Bracket],
    *,
    leverage: int,
    allocation_pct: float,
    reserve_pct: float,
) -> list[float]:
    margin_fraction = allocation_pct / 100.0 * (1.0 - reserve_pct / 100.0)
    if margin_fraction <= 0.0:
        raise ValueError("allocation/reserve imply zero usable slot margin")
    points = {
        float(spec.baseline_capital_min_usdt),
        float(spec.baseline_capital_max_usdt),
    }
    for bracket in brackets[1:]:
        baseline = bracket.notional_floor / leverage / margin_fraction
        if spec.baseline_capital_min_usdt < baseline < spec.baseline_capital_max_usdt:
            epsilon = max(1e-9, baseline * 1e-9)
            points.add(max(spec.baseline_capital_min_usdt, baseline - epsilon))
            points.add(min(spec.baseline_capital_max_usdt, baseline + epsilon))
    return sorted(points)


def _bracket_for_notional(notional: float, brackets: Sequence[Bracket]) -> Bracket:
    for bracket in brackets:
        if _notional_in_bracket(notional, bracket, brackets):
            return bracket
    raise ValueError(f"notional {notional} is outside the supplied bracket ladder")


def _notional_in_bracket(
    notional: float,
    bracket: Bracket,
    brackets: Sequence[Bracket],
) -> bool:
    is_last = bracket is brackets[-1]
    return bracket.notional_floor <= notional <= bracket.notional_cap if is_last else (
        bracket.notional_floor <= notional < bracket.notional_cap
    )


def _validate_bracket_ladder(brackets: Sequence[Bracket]) -> None:
    ordered = sorted(brackets, key=lambda row: row.bracket)
    if list(brackets) != ordered:
        raise ValueError("brackets must be ordered by bracket id")
    if ordered[0].notional_floor != 0.0:
        raise ValueError("first bracket must start at zero notional")
    if len({row.bracket for row in ordered}) != len(ordered):
        raise ValueError("bracket ids must be unique")
    for previous, current in zip(ordered, ordered[1:]):
        if not math.isclose(
            previous.notional_cap,
            current.notional_floor,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("bracket ladder contains a gap or overlap")
        prev_mm = maintenance_margin(previous.notional_cap, previous)
        curr_mm = maintenance_margin(current.notional_floor, current)
        tolerance = max(1e-6, abs(prev_mm) * 1e-9, abs(curr_mm) * 1e-9)
        if not math.isclose(prev_mm, curr_mm, rel_tol=0.0, abs_tol=tolerance):
            raise ValueError("bracket cum values do not make maintenance margin continuous")


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _validate_digest(value: str) -> None:
    if len(value) != 64:
        raise ValueError("fingerprint must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("fingerprint is not hexadecimal") from exc
