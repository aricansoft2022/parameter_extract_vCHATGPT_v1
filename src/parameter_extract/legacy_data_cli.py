from __future__ import annotations

import argparse
import json

from .legacy_paramderive import migrate_paramderive_market_data


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
    parser.add_argument("--start", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--end", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--output-directory", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = migrate_paramderive_market_data(
        btc1_root=args.btc1_root,
        funding_root=args.funding_root,
        start=args.start,
        end=args.end,
        output_directory=args.output_directory,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
