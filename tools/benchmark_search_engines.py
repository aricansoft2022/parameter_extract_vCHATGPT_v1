from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from parameter_extract.bulk_search import run_bulk_search
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


def _assert_parity(reference: dict[str, Any], candidate: dict[str, Any], *, label: str) -> None:
    mismatches = [field for field in PARITY_FIELDS if reference.get(field) != candidate.get(field)]
    if mismatches:
        raise RuntimeError(
            f"benchmark aborted because {label} research output differs: "
            + ", ".join(mismatches)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark cached, exit-query and bulk-entry search after exact parity checks."
        )
    )
    parser.add_argument("--study", required=True)
    parser.add_argument("--search", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cached, cached_seconds = _run_timed(
        run_cached_search,
        args.study,
        args.search,
        data_directory=args.data_directory,
    )
    query, query_seconds = _run_timed(
        run_query_search,
        args.study,
        args.search,
        data_directory=args.data_directory,
    )
    _assert_parity(cached, query, label="cached/query")

    bulk, bulk_seconds = _run_timed(
        run_bulk_search,
        args.study,
        args.search,
        data_directory=args.data_directory,
    )
    _assert_parity(query, bulk, label="query/bulk")
    if query.get("query_work_profile") != bulk.get("query_work_profile"):
        raise RuntimeError(
            "benchmark aborted because query/bulk deterministic query-work profiles differ"
        )

    payload = {
        "schema_version": 1,
        "kind": "parameter_extract.search_engine_benchmark",
        "parity_passed": True,
        "study": str(args.study),
        "search": str(args.search),
        "data_directory": str(args.data_directory),
        "cached_engine": cached["search_engine"],
        "query_engine": query["search_engine"],
        "bulk_engine": bulk["search_engine"],
        "evaluated_candidates": bulk["evaluated_candidates"],
        "pareto_candidates": bulk["pareto_candidates"],
        "cached_seconds": cached_seconds,
        "query_seconds": query_seconds,
        "bulk_seconds": bulk_seconds,
        "cached_over_query_speed_ratio": (
            None if query_seconds == 0.0 else cached_seconds / query_seconds
        ),
        "query_over_bulk_speed_ratio": (
            None if bulk_seconds == 0.0 else query_seconds / bulk_seconds
        ),
        "cached_over_bulk_speed_ratio": (
            None if bulk_seconds == 0.0 else cached_seconds / bulk_seconds
        ),
        "query_work_profile": query["query_work_profile"],
        "bulk_query_work_profile": bulk["query_work_profile"],
        "bulk_entry_event_visits": bulk["bulk_entry_event_visits"],
        "bulk_entry_band_membership_checks": bulk[
            "bulk_entry_band_membership_checks"
        ],
        "bulk_keywise_event_scan_upper_bound": bulk[
            "keywise_event_scan_upper_bound"
        ],
        "bulk_event_scan_reduction_fraction": bulk[
            "event_scan_reduction_fraction"
        ],
        "note": (
            "Wall-clock result is for this machine/dataset/grid only. Exact research-output "
            "parity and deterministic query-work-profile equality are checked before timing "
            "is reported; ratios are not universal speed guarantees."
        ),
    }
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
