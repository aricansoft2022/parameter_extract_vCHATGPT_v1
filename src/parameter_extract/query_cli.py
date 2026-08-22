from __future__ import annotations

import argparse
import json
from pathlib import Path

from .query_search import run_query_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-query-search",
        description=(
            "Run discovery search with exact cached entry signals and first-exit/range queries."
        ),
    )
    parser.add_argument("--study", required=True)
    parser.add_argument("--search", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_query_search(
        args.study,
        args.search,
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
                "search_engine": result["search_engine"],
                "reference_engine": result["reference_engine"],
                "runtime_parity_passed": result["runtime_parity_passed"],
                "evaluated_candidates": result["evaluated_candidates"],
                "pareto_candidates": result["pareto_candidates"],
                "entry_signal_cache_hit_fraction": result[
                    "entry_signal_cache_hit_fraction"
                ],
                "reference_full_candle_replay_visits_upper_bound": result[
                    "reference_full_candle_replay_visits_upper_bound"
                ],
            },
            indent=2,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
