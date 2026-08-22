from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scale_calibration import (
    run_scale_calibration,
    verify_scale_calibration_result,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-scale",
        description="Run or verify a fail-closed candidate-scale calibration ladder.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    calibrate = subparsers.add_parser(
        "calibrate",
        help="Run explicit scale stages with bulk exact search.",
    )
    calibrate.add_argument("--calibration", required=True)
    calibrate.add_argument("--data-directory", required=True)
    calibrate.add_argument("--output", required=True)

    verify = subparsers.add_parser(
        "verify",
        help="Verify a stored scale-calibration result and its safe-cap semantics.",
    )
    verify.add_argument("--result", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "calibrate":
        result = run_scale_calibration(
            args.calibration,
            data_directory=args.data_directory,
        )
        output = Path(args.output)
        output.write_text(
            json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "output": str(output),
                    "all_stages_passed": result["all_stages_passed"],
                    "safe_max_candidates": result["safe_max_candidates"],
                    "stopped_after_stage": result["stopped_after_stage"],
                    "executed_stages": len(result["stage_results"]),
                    "scale_calibration_result_fingerprint_sha256": result[
                        "scale_calibration_result_fingerprint_sha256"
                    ],
                },
                indent=2,
                allow_nan=False,
            )
        )
        return 0

    payload = json.loads(Path(args.result).read_text(encoding="utf-8"))
    problems = verify_scale_calibration_result(payload)
    print(
        json.dumps(
            {
                "result": args.result,
                "valid": not problems,
                "problems": problems,
                "safe_max_candidates": payload.get("safe_max_candidates"),
                "stopped_after_stage": payload.get("stopped_after_stage"),
            },
            indent=2,
            allow_nan=False,
        )
    )
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
