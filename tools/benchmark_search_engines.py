from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from parameter_extract.cached_search import run_cached_search
from parameter_extract.query_search import run_query_search

PARITY_FIELDS = (
    "search_fingerprint_sha256",
    "study_fingerprint_sha256",
    "dataset_fingerprint_sha256",
    "execution",
    "symbol",
    "phase_used",
    "validation_accessed",
    "holdout_accessed",
    "pareto_objectives",
    "coarse_candidates",
    "refined_candidates",
    "evaluated_candidates",
    "passed_gates",
    "pareto_candidates",
    "frontier",
)


def _run_timed(function, *args, **kwargs) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return result, time.perf_counter() - started


def _assert_parity(reference: dict[str, Any], candidate: dict[str, Any]) -> None:
    mismatches = [field for field in PARITY_FIELDS if reference.get(field) != candidate.get(field)]
    if mismatches:
        raise RuntimeError(
            "benchmark aborted because cached/query research output differs: "
            + ", ".join(mismatches)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark cached-search vs exact exit-query search after parity checks."
    )
    parser.add_argument("--study", required=True)
    parser.add_argument("--search", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reference, cached_seconds = _run_timed(
        run_cached_search,
        args.study,
        args.search,
        data_directory=args.data_directory,
    )
    candidate, query_seconds = _run_timed(
        run_query_search,
        args.study,
        args.search,
        data_directory=args.data_directory,
    )
    _assert_parity(reference, candidate)

    speed_ratio = None if query_seconds == 0.0 else cached_seconds / query_seconds
    payload = {
        "schema_version": 1,
        "kind": "parameter_extract.search_engine_benchmark",
        "parity_passed": True,
        "study": str(args.study),
        "search": str(args.search),
        "data_directory": str(args.data_directory),
        "cached_engine": reference["search_engine"],
        "query_engine": candidate["search_engine"],
        "evaluated_candidates": candidate["evaluated_candidates"],
        "pareto_candidates": candidate["pareto_candidates"],
        "cached_seconds": cached_seconds,
        "query_seconds": query_seconds,
        "cached_over_query_speed_ratio": speed_ratio,
        "note": (
            "Wall-clock result for this machine/dataset/grid only. Correctness parity is "
            "checked before timing is reported; this is not a universal speed guarantee."
        ),
    }
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
