from __future__ import annotations

import argparse
import json

from .indexed_search import run_indexed_search
from .io import write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-indexed-search",
        description=(
            "Run parity-gated crossing-index discovery search. Raw entry filtering scans "
            "candidate-independent RSI/ADR crossing events while execution remains truth replay."
        ),
    )
    parser.add_argument("--study", dest="study_file", required=True)
    parser.add_argument("--search", dest="search_file", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_indexed_search(
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
                "truth_runtime_parity_checked_candidates": payload[
                    "truth_runtime_parity_checked_candidates"
                ],
                "prepared_runtime_parity_checked_candidates": payload[
                    "prepared_runtime_parity_checked_candidates"
                ],
                "evaluated_candidates": payload["evaluated_candidates"],
                "pareto_candidates": payload["pareto_candidates"],
                "signal_filter_full_candle_checks_reference": payload[
                    "signal_filter_full_candle_checks_reference"
                ],
                "signal_filter_indexed_event_checks": payload[
                    "signal_filter_indexed_event_checks"
                ],
                "signal_filter_check_reduction_pct": payload[
                    "signal_filter_check_reduction_pct"
                ],
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
