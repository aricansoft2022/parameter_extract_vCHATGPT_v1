from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .exchange_risk import verify_exchange_risk_result
from .manifest import sha256_file
from .models import StrategySpec
from .promotion import candidate_fingerprint
from .selection import _selection_set_fingerprint, verify_selection_result

DEPLOYMENT_SCHEMA_VERSION = 1
DEPLOYMENT_EXPORT_METHOD = "ccbot_teams_csv_frozen_selected_set_v1"
TARGET_CCBOT_REPOSITORY = "aricansoft2022/cryptobot_vCLUADE_v5"
AUDITED_CCBOT_COMMIT = "0ab6aa532cb22f399bc94393280c604cb6756d66"
TEAM_CSV_FIELDS: tuple[str, ...] = (
    "id",
    "enabled",
    "priority",
    "symbol",
    "rsi_period",
    "rsi_entry",
    "adx_min",
    "adx_max",
    "exit_mode",
    "rsi_exit",
    "tp_price_pct",
    "leverage",
)
RSI_PERIOD_CHOICES: tuple[int, ...] = (14, 15, 16, 17, 18, 19)


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    name: str
    source_selection_result_sha256: str
    source_exchange_risk_result_sha256: str
    target_ccbot_repository: str
    target_ccbot_commit_sha: str
    first_team_id: int
    enabled: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("deployment name cannot be empty")
        _validate_digest(self.source_selection_result_sha256, length=64)
        _validate_digest(self.source_exchange_risk_result_sha256, length=64)
        if self.target_ccbot_repository != TARGET_CCBOT_REPOSITORY:
            raise ValueError(
                f"deployment target must be {TARGET_CCBOT_REPOSITORY}"
            )
        _validate_digest(self.target_ccbot_commit_sha, length=40)
        if self.target_ccbot_commit_sha != AUDITED_CCBOT_COMMIT:
            raise ValueError(
                "deployment target commit is not the ccbot commit audited by this exporter"
            )
        if self.first_team_id < 1:
            raise ValueError("first_team_id must be positive")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a JSON boolean")


def load_deployment_json(path: str | Path) -> DeploymentSpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != DEPLOYMENT_SCHEMA_VERSION:
        raise ValueError("unsupported deployment schema_version")
    return DeploymentSpec(
        name=str(payload["name"]),
        source_selection_result_sha256=str(
            payload["source_selection_result_sha256"]
        ).lower(),
        source_exchange_risk_result_sha256=str(
            payload["source_exchange_risk_result_sha256"]
        ).lower(),
        target_ccbot_repository=str(payload["target_ccbot_repository"]),
        target_ccbot_commit_sha=str(payload["target_ccbot_commit_sha"]).lower(),
        first_team_id=int(payload["first_team_id"]),
        enabled=payload["enabled"],
    )


def deployment_fingerprint(spec: DeploymentSpec) -> str:
    return _sha256_json(asdict(spec))


def team_csv_contract_fingerprint() -> str:
    contract = {
        "schema_version": 1,
        "target_repository": TARGET_CCBOT_REPOSITORY,
        "audited_commit": AUDITED_CCBOT_COMMIT,
        "fields": list(TEAM_CSV_FIELDS),
        "constraints": {
            "id_min": 1,
            "priority_min": 0,
            "priority_max": 10000,
            "rsi_period_choices": list(RSI_PERIOD_CHOICES),
            "rsi_entry_open_interval": [0.0, 100.0],
            "adx_bounds": "0 <= adx_min < adx_max <= 100",
            "exit_modes": ["rsi", "tp"],
            "leverage_min": 1,
            "leverage_max": 125,
        },
    }
    return _sha256_json(contract)


def run_deployment_export(
    selection_result_path: str | Path,
    exchange_risk_result_path: str | Path,
    deployment_path: str | Path,
    *,
    teams_csv_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    selection_path = Path(selection_result_path)
    exchange_path = Path(exchange_risk_result_path)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
    spec = load_deployment_json(deployment_path)

    selection_problems = verify_selection_result(selection)
    if selection_problems:
        raise ValueError(
            "selection-result verification failed: " + "; ".join(selection_problems)
        )
    exchange_problems = verify_exchange_risk_result(exchange)
    if exchange_problems:
        raise ValueError(
            "exchange-risk-result verification failed: " + "; ".join(exchange_problems)
        )
    if exchange.get("status") != "EXCHANGE_RISK_PASS":
        raise ValueError("deployment export requires EXCHANGE_RISK_PASS")
    if exchange.get("exchange_liquidation_validated") is not True:
        raise ValueError("deployment export requires validated exchange liquidation")
    if exchange.get("teams_export_ready") is not True:
        raise ValueError("exchange-risk result does not authorize teams export")

    actual_selection_sha = sha256_file(selection_path)
    actual_exchange_sha = sha256_file(exchange_path)
    if spec.source_selection_result_sha256 != actual_selection_sha:
        raise ValueError("deployment contract is pinned to a different selection-result file")
    if spec.source_exchange_risk_result_sha256 != actual_exchange_sha:
        raise ValueError("deployment contract is pinned to a different exchange-risk-result file")

    selected_fp = str(selection["selected_set_fingerprint_sha256"])
    if exchange.get("source_selected_set_fingerprint_sha256") != selected_fp:
        raise ValueError("exchange-risk result belongs to a different selected set")
    if exchange.get("symbol") != selection.get("symbol"):
        raise ValueError("exchange-risk and selection symbols disagree")
    if exchange.get("slot_count") != selection.get("slot_count"):
        raise ValueError("exchange-risk and selection slot counts disagree")

    leverage = exchange.get("provisional_deployment_leverage")
    if not isinstance(leverage, int) or not 1 <= leverage <= 125:
        raise ValueError("exchange-risk result has invalid deployment leverage")

    selected = selection.get("selected")
    if not isinstance(selected, list) or not selected:
        raise ValueError("selection result has no selected teams")
    if len(selected) > 10000:
        raise ValueError("selected team count exceeds ccbot priority range")

    rows = _build_team_rows(
        selected,
        first_team_id=spec.first_team_id,
        enabled=spec.enabled,
        leverage=leverage,
    )
    csv_bytes = serialize_team_csv(rows)
    csv_sha = hashlib.sha256(csv_bytes).hexdigest()

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "parameter_extract.deployment_export",
        "deployment": spec.name,
        "deployment_export_method": DEPLOYMENT_EXPORT_METHOD,
        "deployment_fingerprint_sha256": deployment_fingerprint(spec),
        "deployment_spec": asdict(spec),
        "source_selection_result_sha256": actual_selection_sha,
        "source_exchange_risk_result_sha256": actual_exchange_sha,
        "source_portfolio_result_sha256": selection[
            "source_portfolio_result_sha256"
        ],
        "source_selected_set_fingerprint_sha256": selected_fp,
        "exchange_snapshot_fingerprint_sha256": exchange[
            "exchange_snapshot_fingerprint_sha256"
        ],
        "target_ccbot_repository": spec.target_ccbot_repository,
        "target_ccbot_commit_sha": spec.target_ccbot_commit_sha,
        "target_team_csv_contract_fingerprint_sha256": (
            team_csv_contract_fingerprint()
        ),
        "team_csv_fields": list(TEAM_CSV_FIELDS),
        "symbol": selection["symbol"],
        "slot_count": selection["slot_count"],
        "allocation_pct": exchange["allocation_pct"],
        "reserve_pct": exchange["reserve_pct"],
        "leverage": leverage,
        "team_count": len(rows),
        "first_team_id": spec.first_team_id,
        "enabled": spec.enabled,
        "priority_source": "selection_compact_priority",
        "team_ids_assigned_at_export": True,
        "team_priorities_preserved": True,
        "strategy_parameters_retuned": False,
        "selected_set_changed": False,
        "priority_reoptimized": False,
        "leverage_optimized": False,
        "exchange_liquidation_validated": True,
        "teams_export_ready": True,
        "existing_live_team_id_collisions_checked": False,
        "import_requires_ccbot_dry_run": True,
        "selected": selected,
        "rows": rows,
        "teams_csv_sha256": csv_sha,
        "teams_csv_size_bytes": len(csv_bytes),
    }
    manifest["deployment_manifest_fingerprint_sha256"] = (
        deployment_manifest_fingerprint(manifest)
    )

    problems = verify_deployment_manifest(manifest, csv_bytes=csv_bytes)
    if problems:
        raise RuntimeError(
            "generated deployment manifest failed self-verification: "
            + "; ".join(problems)
        )

    Path(teams_csv_path).write_bytes(csv_bytes)
    Path(manifest_path).write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def deployment_manifest_fingerprint(payload: dict[str, Any]) -> str:
    stable = dict(payload)
    stable.pop("deployment_manifest_fingerprint_sha256", None)
    return _sha256_json(stable)


def verify_deployment_manifest(
    payload: dict[str, Any], *, csv_bytes: bytes | None = None
) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("unsupported deployment-manifest schema_version")
    if payload.get("kind") != "parameter_extract.deployment_export":
        problems.append("deployment-manifest kind is invalid")
    if payload.get("deployment_export_method") != DEPLOYMENT_EXPORT_METHOD:
        problems.append("deployment export method is unsupported")

    try:
        spec = DeploymentSpec(**payload["deployment_spec"])
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"deployment_spec is invalid: {exc}")
        return problems

    if payload.get("deployment_fingerprint_sha256") != deployment_fingerprint(spec):
        problems.append("deployment fingerprint does not match deployment_spec")
    if payload.get("source_selection_result_sha256") != spec.source_selection_result_sha256:
        problems.append("source selection-result SHA is inconsistent")
    if payload.get("source_exchange_risk_result_sha256") != spec.source_exchange_risk_result_sha256:
        problems.append("source exchange-risk-result SHA is inconsistent")
    if payload.get("target_ccbot_repository") != spec.target_ccbot_repository:
        problems.append("target ccbot repository is inconsistent")
    if payload.get("target_ccbot_commit_sha") != spec.target_ccbot_commit_sha:
        problems.append("target ccbot commit is inconsistent")
    if payload.get("target_team_csv_contract_fingerprint_sha256") != team_csv_contract_fingerprint():
        problems.append("target team CSV contract fingerprint is inconsistent")
    if payload.get("team_csv_fields") != list(TEAM_CSV_FIELDS):
        problems.append("team CSV fields do not match audited ccbot contract")

    for flag, expected in (
        ("team_ids_assigned_at_export", True),
        ("team_priorities_preserved", True),
        ("strategy_parameters_retuned", False),
        ("selected_set_changed", False),
        ("priority_reoptimized", False),
        ("leverage_optimized", False),
        ("exchange_liquidation_validated", True),
        ("teams_export_ready", True),
        ("existing_live_team_id_collisions_checked", False),
        ("import_requires_ccbot_dry_run", True),
    ):
        if payload.get(flag) is not expected:
            problems.append(f"{flag} must be {str(expected).lower()}")
    if payload.get("priority_source") != "selection_compact_priority":
        problems.append("deployment priority source is unsupported")

    selected = payload.get("selected")
    rows = payload.get("rows")
    if not isinstance(selected, list) or not selected:
        problems.append("deployment selected set is missing or empty")
        return problems
    if not isinstance(rows, list) or not rows:
        problems.append("deployment team rows are missing or empty")
        return problems
    if payload.get("team_count") != len(rows) or len(rows) != len(selected):
        problems.append("deployment team count is inconsistent")

    slot_count = payload.get("slot_count")
    source_portfolio_sha = payload.get("source_portfolio_result_sha256")
    if not isinstance(slot_count, int) or slot_count < 1:
        problems.append("deployment slot_count is invalid")
    elif isinstance(source_portfolio_sha, str):
        try:
            selected_fp = _selection_set_fingerprint(
                source_portfolio_sha=source_portfolio_sha,
                slot_count=slot_count,
                selected=selected,
            )
            if payload.get("source_selected_set_fingerprint_sha256") != selected_fp:
                problems.append("selected-set fingerprint does not match stored selected rows")
        except (TypeError, ValueError) as exc:
            problems.append(f"selected-set fingerprint cannot be recomputed: {exc}")
    else:
        problems.append("source portfolio-result SHA is missing")

    leverage = payload.get("leverage")
    if not isinstance(leverage, int) or not 1 <= leverage <= 125:
        problems.append("deployment leverage is invalid")
        return problems

    expected_rows: list[dict[str, Any]] = []
    try:
        expected_rows = _build_team_rows(
            selected,
            first_team_id=spec.first_team_id,
            enabled=spec.enabled,
            leverage=leverage,
        )
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"stored selected set cannot produce ccbot rows: {exc}")
        return problems
    if rows != expected_rows:
        problems.append("deployment team rows do not match frozen selected strategies")

    expected_csv = serialize_team_csv(expected_rows)
    expected_sha = hashlib.sha256(expected_csv).hexdigest()
    if payload.get("teams_csv_sha256") != expected_sha:
        problems.append("teams CSV SHA does not match stored rows")
    if payload.get("teams_csv_size_bytes") != len(expected_csv):
        problems.append("teams CSV size does not match stored rows")
    if csv_bytes is not None and csv_bytes != expected_csv:
        problems.append("supplied teams CSV bytes do not match deployment manifest")

    expected_manifest_fp = deployment_manifest_fingerprint(payload)
    if payload.get("deployment_manifest_fingerprint_sha256") != expected_manifest_fp:
        problems.append("deployment manifest fingerprint does not recompute")
    return problems


def serialize_team_csv(rows: Sequence[dict[str, Any]]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(
        handle,
        fieldnames=list(TEAM_CSV_FIELDS),
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                field: _csv_value(row.get(field))
                for field in TEAM_CSV_FIELDS
            }
        )
    return handle.getvalue().encode("utf-8")


def _build_team_rows(
    selected: Sequence[dict[str, Any]],
    *,
    first_team_id: int,
    enabled: bool,
    leverage: int,
) -> list[dict[str, Any]]:
    if first_team_id < 1:
        raise ValueError("first_team_id must be positive")
    if len(selected) > 10000:
        raise ValueError("selected team count exceeds ccbot priority range")
    rows: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    for index, selected_row in enumerate(selected):
        strategy = StrategySpec(**selected_row["strategy"])
        fingerprint = candidate_fingerprint(strategy)
        if selected_row.get("candidate_fingerprint_sha256") != fingerprint:
            raise ValueError(f"selected row {index}: strategy fingerprint mismatch")
        if fingerprint in seen_fingerprints:
            raise ValueError(f"selected row {index}: duplicate strategy fingerprint")
        if selected_row.get("priority") != index + 1:
            raise ValueError(f"selected row {index}: compact priority is inconsistent")
        seen_fingerprints.add(fingerprint)
        row = {
            "id": first_team_id + index,
            "enabled": enabled,
            "priority": index + 1,
            "symbol": strategy.symbol,
            "rsi_period": strategy.rsi_period,
            "rsi_entry": strategy.rsi_entry,
            "adx_min": strategy.adx_min,
            "adx_max": strategy.adx_max,
            "exit_mode": strategy.exit_mode,
            "rsi_exit": strategy.rsi_exit if strategy.exit_mode == "rsi" else None,
            "tp_price_pct": (
                strategy.tp_price_pct if strategy.exit_mode == "tp" else None
            ),
            "leverage": leverage,
        }
        _validate_live_team_row(row)
        rows.append(row)
    symbols = {str(row["symbol"]) for row in rows}
    leverages = {int(row["leverage"]) for row in rows}
    if len(symbols) != 1:
        raise ValueError("V1 deployment export requires one symbol")
    if len(leverages) != 1:
        raise ValueError("all exported teams must use the same symbol leverage")
    return rows


def _validate_live_team_row(row: dict[str, Any]) -> None:
    team_id = int(row["id"])
    priority = int(row["priority"])
    symbol = str(row["symbol"])
    rsi_period = int(row["rsi_period"])
    rsi_entry = float(row["rsi_entry"])
    adx_min = float(row["adx_min"])
    adx_max = float(row["adx_max"])
    exit_mode = str(row["exit_mode"])
    leverage = int(row["leverage"])
    if team_id < 1:
        raise ValueError("ccbot team id must be positive")
    if not 0 <= priority <= 10000:
        raise ValueError("ccbot priority must be inside [0, 10000]")
    if not symbol.isalnum() or not symbol.isupper() or not symbol.endswith("USDT"):
        raise ValueError("ccbot symbol must be an upper-case alphanumeric USDT pair")
    if rsi_period not in RSI_PERIOD_CHOICES:
        raise ValueError("ccbot rsi_period is outside the audited choices")
    if not 0.0 < rsi_entry < 100.0:
        raise ValueError("ccbot rsi_entry must be strictly inside (0, 100)")
    if not 0.0 <= adx_min < adx_max <= 100.0:
        raise ValueError("ccbot ADX bounds are invalid")
    if not 1 <= leverage <= 125:
        raise ValueError("ccbot leverage must be inside [1, 125]")
    if exit_mode == "rsi":
        rsi_exit = row.get("rsi_exit")
        if rsi_exit is None or not 0.0 < float(rsi_exit) < 100.0:
            raise ValueError("ccbot RSI exit requires rsi_exit inside (0, 100)")
        if float(rsi_exit) <= rsi_entry:
            raise ValueError("ccbot rsi_exit must be above rsi_entry")
        if row.get("tp_price_pct") is not None:
            raise ValueError("RSI exit row must leave tp_price_pct empty")
    elif exit_mode == "tp":
        tp = row.get("tp_price_pct")
        if tp is None or not 0.0 < float(tp) <= 100.0:
            raise ValueError("ccbot TP exit requires tp_price_pct inside (0, 100]")
        if row.get("rsi_exit") is not None:
            raise ValueError("TP exit row must leave rsi_exit empty")
    else:
        raise ValueError("ccbot exit_mode must be rsi or tp")
    for name in ("rsi_entry", "adx_min", "adx_max"):
        if not math.isfinite(float(row[name])):
            raise ValueError(f"ccbot {name} must be finite")


def _csv_value(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _sha256_json(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_digest(value: str, *, length: int) -> None:
    if len(value) != length:
        raise ValueError(f"digest must contain {length} hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("digest is not hexadecimal") from exc
