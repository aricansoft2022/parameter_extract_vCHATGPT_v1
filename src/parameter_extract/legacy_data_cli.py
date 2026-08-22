from __future__ import annotations

import argparse
import json

from .legacy_source_bootstrap import (
    migrate_paramderive_market_data_with_funding_boundary,
    preflight_paramderive_source,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-migrate-paramderive",
        description=(
            "Verify and migrate an accepted paramderive BTC1/funding monthly store into "
            "bundle-ready parameter_extract CSV data without modifying the source store."
        ),
    )
    parser.add_argument("--btc1-root", required=True)
    parser.add_argument("--funding-root", required=True)
    parser.add_argument("--start", required=True, help="Inclusive candle YYYY-MM")
    parser.add_argument(
        "--funding-start",
        help="First month requiring funding YYYY-MM; defaults to --start",
    )
    parser.add_argument("--end", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--output-directory")
    parser.add_argument(
        "--legacy-fingerprint-reference",
        help="Optional exact legacy prepare.dataset_fingerprint SHA-256 to compare.",
    )
    parser.add_argument(
        "--require-legacy-fingerprint-match",
        action="store_true",
        help=(
            "Fail before writing output unless the exact legacy raw-manifest fingerprint "
            "matches --legacy-fingerprint-reference."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate source months and report legacy fingerprint without writing output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    funding_start = args.funding_start or args.start

    if args.require_legacy_fingerprint_match and not args.legacy_fingerprint_reference:
        raise SystemExit(
            "--require-legacy-fingerprint-match requires --legacy-fingerprint-reference"
        )

    if args.preflight_only:
        result = preflight_paramderive_source(
            btc1_root=args.btc1_root,
            funding_root=args.funding_root,
            start=args.start,
            funding_start=funding_start,
            end=args.end,
            legacy_fingerprint_reference=args.legacy_fingerprint_reference,
        )
    else:
        if not args.output_directory:
            raise SystemExit("--output-directory is required unless --preflight-only is used")
        result = migrate_paramderive_market_data_with_funding_boundary(
            btc1_root=args.btc1_root,
            funding_root=args.funding_root,
            start=args.start,
            funding_start=funding_start,
            end=args.end,
            output_directory=args.output_directory,
            legacy_fingerprint_reference=args.legacy_fingerprint_reference,
            require_legacy_fingerprint_match=args.require_legacy_fingerprint_match,
        )

    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
