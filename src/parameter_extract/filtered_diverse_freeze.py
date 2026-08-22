from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from .diverse_freeze import freeze_diverse_discovery_result
from .io import write_json
from .manifest import sha256_file
from .promotion import candidate_set_fingerprint, verify_candidate_set

FILTERED_DIVERSE_FREEZE_METHOD = "discovery_exit_mode_filtered_kcenter_v1"


def freeze_diverse_discovery_result_by_exit_mode(
    path: str | Path,
    *,
    count: int,
    exit_mode: str,
) -> dict[str, Any]:
    """Freeze a diverse discovery-only subset restricted to one exit mode.

    The original discovery result remains the provenance anchor. A temporary derived search
    result is used only to reuse the deterministic k-center selector; its temporary hash is never
    retained. Selected strategies are copied verbatim from the original Pareto frontier.
    Validation and holdout are never read.
    """
    if exit_mode not in {"tp", "rsi"}:
        raise ValueError("exit_mode must be 'tp' or 'rsi'")
    if count < 1:
        raise ValueError("count must be positive")

    source_path = Path(path)
    original = json.loads(source_path.read_text(encoding="utf-8"))
    if original.get("kind") != "parameter_extract.discovery_search":
        raise ValueError("input is not a discovery search result")
    if original.get("phase_used") != "discovery":
        raise ValueError("search result was not produced from discovery phase only")
    if original.get("validation_accessed") is not False:
        raise ValueError("search result indicates validation access")
    if original.get("holdout_accessed") is not False:
        raise ValueError("search result indicates holdout access")

    frontier = original.get("frontier")
    if not isinstance(frontier, list) or not frontier:
        raise ValueError("discovery result has no Pareto candidates to freeze")

    eligible = [row for row in frontier if row.get("strategy", {}).get("exit_mode") == exit_mode]
    if not eligible:
        raise ValueError(f"discovery frontier contains no {exit_mode!r} candidates")
    if count > len(eligible):
        raise ValueError(
            f"requested {count} candidates but exit_mode={exit_mode!r} eligibility contains "
            f"only {len(eligible)} candidates"
        )

    filtered = dict(original)
    filtered["frontier"] = eligible
    filtered["pareto_candidates"] = len(eligible)

    with tempfile.TemporaryDirectory(prefix="pextract-filtered-freeze-") as directory:
        filtered_path = Path(directory) / "filtered-discovery.json"
        filtered_path.write_text(
            json.dumps(filtered, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        payload = freeze_diverse_discovery_result(filtered_path, count=count)

    source = dict(payload["source"])
    source["search_result_sha256"] = sha256_file(source_path)
    selection = dict(source["prevalidation_selection"])
    selection["method"] = FILTERED_DIVERSE_FREEZE_METHOD
    selection["source_frontier_count"] = len(frontier)
    selection["eligible_frontier_count"] = len(eligible)
    selection["eligibility_filter"] = {"exit_mode": exit_mode}
    source["prevalidation_selection"] = selection
    payload["source"] = source
    payload["candidate_set_fingerprint_sha256"] = candidate_set_fingerprint(payload)

    if any(row["strategy"]["exit_mode"] != exit_mode for row in payload["candidates"]):
        raise RuntimeError("filtered diverse freeze selected a strategy outside the exit-mode filter")

    problems = verify_candidate_set(payload)
    if problems:
        raise RuntimeError(
            "filtered diverse frozen candidate set failed self-verification: " + "; ".join(problems)
        )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-freeze-diverse-filtered",
        description="Freeze a deterministic diverse discovery-only subset for one exit mode.",
    )
    parser.add_argument("--search-result", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--exit-mode", required=True, choices=("tp", "rsi"))
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = freeze_diverse_discovery_result_by_exit_mode(
        args.search_result,
        count=args.count,
        exit_mode=args.exit_mode,
    )
    write_json(args.output, payload)
    selection = payload["source"]["prevalidation_selection"]
    print(
        json.dumps(
            {
                "output": args.output,
                "candidate_set_fingerprint_sha256": payload["candidate_set_fingerprint_sha256"],
                "source_frontier_count": selection["source_frontier_count"],
                "eligible_frontier_count": selection["eligible_frontier_count"],
                "eligibility_filter": selection["eligibility_filter"],
                "candidate_count": payload["candidate_count"],
                "selected_exit_mode_counts": selection.get("selected_exit_mode_counts"),
                "selected_rsi_period_counts": selection.get("selected_rsi_period_counts"),
                "parameters_frozen": payload["parameters_frozen"],
                "validation_accessed": selection["validation_accessed"],
                "holdout_accessed": selection["holdout_accessed"],
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
