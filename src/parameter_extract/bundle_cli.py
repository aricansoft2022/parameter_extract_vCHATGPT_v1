from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bundle_builder import seal_research_bundle
from .research_bundle import (
    run_bundle_calibration,
    run_bundle_discovery,
    verify_research_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-bundle",
        description=(
            "Seal, verify and execute a pinned real-data research bundle through "
            "calibration-gated discovery."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seal = subparsers.add_parser(
        "seal",
        help="Compute contract fingerprints and atomically create a verified bundle.json.",
    )
    seal.add_argument("--name", required=True)
    seal.add_argument("--manifest", required=True)
    seal.add_argument("--study", required=True)
    seal.add_argument("--search", required=True)
    seal.add_argument("--calibration", required=True)
    seal.add_argument("--data-directory", required=True)
    seal.add_argument("--output", required=True)

    verify = subparsers.add_parser(
        "verify",
        help="Verify dataset bytes and manifest/study/search/calibration lineage only.",
    )
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--data-directory", required=True)

    calibrate = subparsers.add_parser(
        "calibrate",
        help="Run the bundled fail-closed scale ladder after static verification.",
    )
    calibrate.add_argument("--bundle", required=True)
    calibrate.add_argument("--data-directory", required=True)
    calibrate.add_argument("--output", required=True)

    discovery = subparsers.add_parser(
        "discovery",
        help="Run bundled bulk-exact discovery only when calibration safe cap covers the search.",
    )
    discovery.add_argument("--bundle", required=True)
    discovery.add_argument("--calibration-result", required=True)
    discovery.add_argument("--data-directory", required=True)
    discovery.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "seal":
        result = seal_research_bundle(
            name=args.name,
            manifest_file=args.manifest,
            study_file=args.study,
            discovery_search_file=args.search,
            calibration_file=args.calibration,
            output_file=args.output,
            data_directory=args.data_directory,
        )
        verification = result["verification"]
        _print(
            {
                "output": result["output"],
                "bundle": verification["bundle"],
                "bundle_fingerprint_sha256": verification[
                    "bundle_fingerprint_sha256"
                ],
                "required_safe_max_candidates": verification[
                    "required_safe_max_candidates"
                ],
                "exact_discovery_calibration_stages": verification[
                    "exact_discovery_calibration_stages"
                ],
                "ready_for_calibration": verification["ready_for_calibration"],
            }
        )
        return 0

    if args.command == "verify":
        result = verify_research_bundle(args.bundle, data_directory=args.data_directory)
        _print(result)
        return 0

    if args.command == "calibrate":
        result = run_bundle_calibration(args.bundle, data_directory=args.data_directory)
        output = Path(args.output)
        _write_json(output, result)
        _print(
            {
                "output": str(output),
                "bundle": result["research_bundle"]["bundle"],
                "all_stages_passed": result.get("all_stages_passed"),
                "safe_max_candidates": result.get("safe_max_candidates"),
                "stopped_after_stage": result.get("stopped_after_stage"),
                "required_safe_max_candidates": result["research_bundle"][
                    "required_safe_max_candidates"
                ],
                "scale_calibration_result_fingerprint_sha256": result.get(
                    "scale_calibration_result_fingerprint_sha256"
                ),
            }
        )
        return 0

    result = run_bundle_discovery(
        args.bundle,
        args.calibration_result,
        data_directory=args.data_directory,
    )
    output = Path(args.output)
    _write_json(output, result)
    lineage = result["research_bundle"]
    _print(
        {
            "output": str(output),
            "bundle": lineage["bundle"],
            "engine": result["search_engine"],
            "evaluated_candidates": result["evaluated_candidates"],
            "pareto_candidates": result["pareto_candidates"],
            "calibrated_safe_max_candidates": lineage["calibrated_safe_max_candidates"],
            "required_safe_max_candidates": lineage["required_safe_max_candidates"],
            "calibration_gate_passed": lineage["calibration_gate_passed"],
            "validation_accessed": result["validation_accessed"],
            "holdout_accessed": result["holdout_accessed"],
        }
    )
    return 0


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    raise SystemExit(main())
