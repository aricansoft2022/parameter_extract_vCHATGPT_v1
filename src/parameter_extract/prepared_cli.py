from __future__ import annotations

import argparse
import json

from .io import write_json
from .prepared_search import run_prepared_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-fast-search",
        description=(
            "Run the parity-gated prepared-exact discovery search. Indicator state is "
            "cached across candidates; execution remains the truth replay."
        ),
    )
    parser.add_argument("--study", dest="study_file", required=True)
    parser.add_argument("--search", dest="search_file", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_prepared_search(
        args.study_file,
        args.search_file,
        data_directory=args.data_directory,
    )
    write_json(args.output, payload)
    print(
        json.dumps(
            {
                "output": args.output,
                "search_engine": payload["search_engine"],
                "reference_engine": payload["reference_engine"],
                "runtime_parity_passed": payload["runtime_parity_passed"],
                "runtime_parity_checked_candidates": payload[
                    "runtime_parity_checked_candidates"
                ],
                "coarse_candidates": payload["coarse_candidates"],
                "refined_candidates": payload["refined_candidates"],
                "evaluated_candidates": payload["evaluated_candidates"],
                "passed_gates": payload["passed_gates"],
                "pareto_candidates": payload["pareto_candidates"],
                "validation_accessed": payload["validation_accessed"],
                "holdout_accessed": payload["holdout_accessed"],
            },
            indent=2,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
