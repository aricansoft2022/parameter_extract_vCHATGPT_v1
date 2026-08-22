from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .families import run_family_clustering
from .holdout import run_holdout
from .io import load_binance_klines_csv, load_funding_csv, load_strategy_json, write_json
from .manifest import build_manifest, read_checksum_file, verify_manifest
from .metrics import summarize
from .models import ExecutionModel
from .parity import check_parity_fixture
from .portfolio import run_portfolio
from .promotion import freeze_discovery_result, run_validation
from .replay import run_strategy
from .risk import run_risk
from .robustness import run_robustness
from .search import run_search
from .selection import run_selection
from .study import run_study


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pextract")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="replay one strategy through the truth engine")
    replay.add_argument("--candles", required=True)
    replay.add_argument("--team", required=True, help="JSON StrategySpec")
    replay.add_argument("--funding")
    replay.add_argument(
        "--model",
        choices=("frictionless", "expected", "stress"),
        default="expected",
    )
    replay.add_argument("--min-trades", type=int, default=30)
    replay.add_argument("--trades", action="store_true")

    manifest = sub.add_parser("manifest", help="fingerprint and audit historical input files")
    manifest.add_argument("--candles", required=True)
    manifest.add_argument("--funding")
    manifest.add_argument("--candles-checksum")
    manifest.add_argument("--funding-checksum")
    manifest.add_argument("--source")
    manifest.add_argument("--output", required=True)

    verify = sub.add_parser("verify-manifest", help="re-hash files against a saved manifest")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--directory", required=True)

    parity = sub.add_parser("parity", help="compare extractor results with a frozen live-bot fixture")
    parity.add_argument("--fixture", required=True)

    study = sub.add_parser("study", help="evaluate one candidate over named research windows")
    study.add_argument("--study", dest="study_file", required=True)
    study.add_argument("--team", required=True)
    study.add_argument("--data-directory", required=True)
    study.add_argument("--reveal-holdout", action="store_true")
    study.add_argument("--output")

    search = sub.add_parser("search", help="coarse-to-fine parameter search on discovery only")
    search.add_argument("--study", dest="study_file", required=True)
    search.add_argument("--search", dest="search_file", required=True)
    search.add_argument("--data-directory", required=True)
    search.add_argument("--output", required=True)

    freeze = sub.add_parser("freeze-candidates", help="freeze a discovery Pareto frontier")
    freeze.add_argument("--search-result", required=True)
    freeze.add_argument("--output", required=True)

    validate = sub.add_parser("validate-candidates", help="validate frozen candidates without retuning")
    validate.add_argument("--study", dest="study_file", required=True)
    validate.add_argument("--candidates", required=True)
    validate.add_argument("--validation", dest="validation_file", required=True)
    validate.add_argument("--data-directory", required=True)
    validate.add_argument("--output", required=True)

    robust = sub.add_parser("robustness", help="diagnose PASS candidates with axis neighbors")
    robust.add_argument("--study", dest="study_file", required=True)
    robust.add_argument("--validation-result", required=True)
    robust.add_argument("--robustness", dest="robustness_file", required=True)
    robust.add_argument("--data-directory", required=True)
    robust.add_argument("--output", required=True)

    families = sub.add_parser("families", help="cluster ROBUST frozen centers into families")
    families.add_argument("--study", dest="study_file", required=True)
    families.add_argument("--robustness-result", required=True)
    families.add_argument("--families", dest="family_file", required=True)
    families.add_argument("--data-directory", required=True)
    families.add_argument("--output", required=True)

    portfolio = sub.add_parser("portfolio", help="replay family representatives through shared slots")
    portfolio.add_argument("--study", dest="study_file", required=True)
    portfolio.add_argument("--families-result", required=True)
    portfolio.add_argument("--portfolio", dest="portfolio_file", required=True)
    portfolio.add_argument("--data-directory", required=True)
    portfolio.add_argument("--output", required=True)

    selection = sub.add_parser("select-portfolio", help="apply one-pass leave-one-out selection")
    selection.add_argument("--study", dest="study_file", required=True)
    selection.add_argument("--families-result", required=True)
    selection.add_argument("--portfolio-result", required=True)
    selection.add_argument("--selection", dest="selection_file", required=True)
    selection.add_argument("--data-directory", required=True)
    selection.add_argument("--output", required=True)

    holdout = sub.add_parser("sealed-holdout", help="evaluate the frozen selected set on holdout only")
    holdout.add_argument("--study", dest="study_file", required=True)
    holdout.add_argument("--selection-result", required=True)
    holdout.add_argument("--holdout", dest="holdout_file", required=True)
    holdout.add_argument("--data-directory", required=True)
    holdout.add_argument("--output", required=True)

    risk = sub.add_parser(
        "risk-budget",
        help="apply a post-holdout MAE stress budget without optimizing leverage",
    )
    risk.add_argument("--selection-result", required=True)
    risk.add_argument("--holdout-result", required=True)
    risk.add_argument("--risk", dest="risk_file", required=True)
    risk.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "replay": _replay,
        "manifest": _manifest,
        "verify-manifest": _verify_manifest,
        "parity": _parity,
        "study": _study,
        "search": _search,
        "freeze-candidates": _freeze_candidates,
        "validate-candidates": _validate_candidates,
        "robustness": _robustness,
        "families": _families,
        "portfolio": _portfolio,
        "select-portfolio": _select_portfolio,
        "sealed-holdout": _sealed_holdout,
        "risk-budget": _risk_budget,
    }
    return handlers[args.command](args)


def _print(payload: object) -> None:
    # Preserve the historical CLI's ability to display NaN sample metrics. Persisted
    # research/risk artifacts still self-verify and their dedicated tests enforce strict JSON.
    print(json.dumps(payload, indent=2, allow_nan=True))


def _replay(args: argparse.Namespace) -> int:
    candles = load_binance_klines_csv(args.candles)
    strategy = load_strategy_json(args.team)
    funding = [] if not args.funding else load_funding_csv(args.funding)
    models = {
        "frictionless": ExecutionModel.frictionless(),
        "expected": ExecutionModel.expected_live(),
        "stress": ExecutionModel.stress(),
    }
    result = run_strategy(candles, strategy, execution=models[args.model], funding=funding)
    payload: dict[str, object] = {
        "strategy": asdict(strategy),
        "execution_model": asdict(models[args.model]),
        "replay": {
            "raw_signal_count": result.raw_signal_count,
            "accepted_signal_count": result.accepted_signal_count,
            "skipped_while_open": result.skipped_while_open,
            "skipped_pending_entry": result.skipped_pending_entry,
            "cancelled_on_gap": result.cancelled_on_gap,
            "open_position": None if result.open_position is None else asdict(result.open_position),
        },
        "metrics": summarize(result, min_trades=args.min_trades).as_dict(),
    }
    if args.trades:
        payload["trades"] = [asdict(trade) for trade in result.trades]
    _print(payload)
    return 0


def _manifest(args: argparse.Namespace) -> int:
    candles = load_binance_klines_csv(args.candles)
    payload = build_manifest(
        candle_path=args.candles,
        candles=candles,
        funding_path=args.funding,
        candle_expected_sha256=(
            None if not args.candles_checksum else read_checksum_file(args.candles_checksum)
        ),
        funding_expected_sha256=(
            None if not args.funding_checksum else read_checksum_file(args.funding_checksum)
        ),
        source=args.source,
    )
    write_json(args.output, payload)
    _print(payload)
    checks = [payload["files"]["candles"], payload["files"].get("funding")]
    checksum_failed = any(
        isinstance(row, dict) and row.get("checksum_verified") is False for row in checks
    )
    return 1 if checksum_failed or not payload["candles"]["integrity_ok"] else 0


def _verify_manifest(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    problems = verify_manifest(payload, directory=args.directory)
    _print({"ok": not problems, "problems": problems})
    return 0 if not problems else 1


def _parity(args: argparse.Namespace) -> int:
    report = check_parity_fixture(args.fixture)
    _print(asdict(report))
    return 0 if report.ok else 1


def _study(args: argparse.Namespace) -> int:
    payload = run_study(
        args.study_file,
        args.team,
        data_directory=args.data_directory,
        reveal_holdout=args.reveal_holdout,
    )
    if args.output:
        write_json(args.output, payload)
    _print(payload)
    return 0


def _search(args: argparse.Namespace) -> int:
    payload = run_search(args.study_file, args.search_file, data_directory=args.data_directory)
    write_json(args.output, payload)
    _print(
        {
            "output": args.output,
            "search_fingerprint_sha256": payload["search_fingerprint_sha256"],
            "phase_used": payload["phase_used"],
            "evaluated_candidates": payload["evaluated_candidates"],
            "passed_gates": payload["passed_gates"],
            "pareto_candidates": payload["pareto_candidates"],
        }
    )
    return 0


def _freeze_candidates(args: argparse.Namespace) -> int:
    payload = freeze_discovery_result(args.search_result)
    write_json(args.output, payload)
    _print(
        {
            "output": args.output,
            "candidate_set_fingerprint_sha256": payload["candidate_set_fingerprint_sha256"],
            "candidate_count": payload["candidate_count"],
            "parameters_frozen": payload["parameters_frozen"],
        }
    )
    return 0


def _validate_candidates(args: argparse.Namespace) -> int:
    payload = run_validation(
        args.study_file,
        args.candidates,
        args.validation_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    _print(
        {
            "output": args.output,
            "validation_fingerprint_sha256": payload["validation_fingerprint_sha256"],
            "candidate_count": payload["candidate_count"],
            "promoted_count": payload["promoted_count"],
            "rejected_count": payload["rejected_count"],
            "parameters_retuned": payload["parameters_retuned"],
            "holdout_accessed": payload["holdout_accessed"],
        }
    )
    return 0


def _robustness(args: argparse.Namespace) -> int:
    payload = run_robustness(
        args.study_file,
        args.validation_result,
        args.robustness_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    _print(
        {
            "output": args.output,
            "robustness_fingerprint_sha256": payload["robustness_fingerprint_sha256"],
            "center_count": payload["center_count"],
            "neighbor_evaluations": payload["neighbor_evaluations"],
            "robust_count": payload["robust_count"],
            "fragile_count": payload["fragile_count"],
            "parameters_retuned": payload["parameters_retuned"],
            "neighbor_strategies_promotable": payload["neighbor_strategies_promotable"],
            "holdout_accessed": payload["holdout_accessed"],
        }
    )
    return 0


def _families(args: argparse.Namespace) -> int:
    payload = run_family_clustering(
        args.study_file,
        args.robustness_result,
        args.family_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    _print(
        {
            "output": args.output,
            "family_fingerprint_sha256": payload["family_fingerprint_sha256"],
            "robust_center_count": payload["robust_center_count"],
            "pair_evaluations": payload["pair_evaluations"],
            "family_count": payload["family_count"],
            "deduplicated_center_count": payload["deduplicated_center_count"],
            "parameters_retuned": payload["parameters_retuned"],
            "holdout_accessed": payload["holdout_accessed"],
        }
    )
    return 0


def _portfolio(args: argparse.Namespace) -> int:
    payload = run_portfolio(
        args.study_file,
        args.families_result,
        args.portfolio_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    aggregate = payload["aggregate"]
    _print(
        {
            "output": args.output,
            "portfolio_fingerprint_sha256": payload["portfolio_fingerprint_sha256"],
            "slot_count": payload["slot_count"],
            "representative_count": payload["representative_count"],
            "fixed_baseline_total_return_pct": aggregate["fixed_baseline_total_return_pct"],
            "blocked_no_slot_count": aggregate["blocked_no_slot_count"],
            "weighted_slot_utilization_pct": aggregate["weighted_slot_utilization_pct"],
            "priority_optimized": payload["priority_optimized"],
            "leverage_applied": payload["leverage_applied"],
            "holdout_accessed": payload["holdout_accessed"],
        }
    )
    return 0


def _select_portfolio(args: argparse.Namespace) -> int:
    payload = run_selection(
        args.study_file,
        args.families_result,
        args.portfolio_result,
        args.selection_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    validation = payload["selected_portfolio_phase_aggregates"]["validation"]
    _print(
        {
            "output": args.output,
            "selection_fingerprint_sha256": payload["selection_fingerprint_sha256"],
            "selected_set_fingerprint_sha256": payload["selected_set_fingerprint_sha256"],
            "source_representative_count": payload["source_representative_count"],
            "selected_count": payload["selected_count"],
            "dropped_count": payload["dropped_count"],
            "selected_validation_return_pct": validation["fixed_baseline_total_return_pct"],
            "priority_reoptimized": payload["priority_reoptimized"],
            "iterative_subset_search": payload["iterative_subset_search"],
            "leverage_applied": payload["leverage_applied"],
            "holdout_accessed": payload["holdout_accessed"],
        }
    )
    return 0


def _sealed_holdout(args: argparse.Namespace) -> int:
    payload = run_holdout(
        args.study_file,
        args.selection_result,
        args.holdout_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    evaluation = payload["evaluation"]
    _print(
        {
            "output": args.output,
            "holdout_fingerprint_sha256": payload["holdout_fingerprint_sha256"],
            "source_selected_set_fingerprint_sha256": payload[
                "source_selected_set_fingerprint_sha256"
            ],
            "status": payload["status"],
            "failure_reasons": payload["failure_reasons"],
            "selected_count": payload["selected_count"],
            "closed_trade_count": evaluation["closed_trade_count"],
            "fixed_baseline_total_return_pct": evaluation["fixed_baseline_total_return_pct"],
            "positive_window_fraction": evaluation["positive_window_fraction"],
            "strategy_parameters_retuned": payload["strategy_parameters_retuned"],
            "selection_gates_retuned": payload["selection_gates_retuned"],
            "priority_reoptimized": payload["priority_reoptimized"],
            "holdout_accessed": payload["holdout_accessed"],
        }
    )
    return 0


def _risk_budget(args: argparse.Namespace) -> int:
    payload = run_risk(args.selection_result, args.holdout_result, args.risk_file)
    write_json(args.output, payload)
    summary = payload["summary"]
    _print(
        {
            "output": args.output,
            "risk_fingerprint_sha256": payload["risk_fingerprint_sha256"],
            "status": payload["status"],
            "failure_reasons": payload["failure_reasons"],
            "selected_count": payload["selected_count"],
            "allocation_pct": payload["allocation_pct"],
            "reserve_pct": payload["reserve_pct"],
            "closed_trade_count": summary["closed_trade_count"],
            "stressed_adverse_move_pct": summary["stressed_adverse_move_pct"],
            "mae_budget_leverage_ceiling": summary["mae_budget_leverage_ceiling"],
            "provisional_deployment_leverage": summary["provisional_deployment_leverage"],
            "leverage_optimized": payload["leverage_optimized"],
            "exchange_liquidation_validated": payload["exchange_liquidation_validated"],
            "teams_export_ready": payload["teams_export_ready"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
