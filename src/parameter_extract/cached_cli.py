from __future__ import annotations

import argparse
import json

from .cached_search import run_cached_search
from .io import write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-cached-search",
        description=(
            "Run parity-gated discovery search with entry-signal membership reused across "
            "exit variants; every candidate still executes through truth replay."
        ),
    )
    parser.add_argument("--study", dest="study_file", required=True)
    parser.add_argument("--search", dest="search_file", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_cached_search(
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
                "evaluated_candidates": payload["evaluated_candidates"],
                "pareto_candidates": payload["pareto_candidates"],
                "unique_entry_signal_keys": payload["unique_entry_signal_keys"],
                "entry_signal_cache_requests": payload["entry_signal_cache_requests"],
                "entry_signal_cache_hits": payload["entry_signal_cache_hits"],
                "entry_signal_cache_misses": payload["entry_signal_cache_misses"],
                "entry_signal_cache_hit_fraction": payload[
                    "entry_signal_cache_hit_fraction"
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
