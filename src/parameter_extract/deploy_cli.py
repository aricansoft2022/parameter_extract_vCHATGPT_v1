from __future__ import annotations

import argparse
import json

from .deployment import run_deployment_export


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pextract-deploy",
        description=(
            "Export a frozen, exchange-risk-approved selected set as ccbot teams.csv "
            "plus a deployment provenance manifest."
        ),
    )
    parser.add_argument("--selection-result", required=True)
    parser.add_argument("--exchange-risk-result", required=True)
    parser.add_argument("--deployment", dest="deployment_file", required=True)
    parser.add_argument("--teams-csv", required=True)
    parser.add_argument("--manifest", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = run_deployment_export(
        args.selection_result,
        args.exchange_risk_result,
        args.deployment_file,
        teams_csv_path=args.teams_csv,
        manifest_path=args.manifest,
    )
    print(
        json.dumps(
            {
                "teams_csv": args.teams_csv,
                "manifest": args.manifest,
                "deployment_manifest_fingerprint_sha256": manifest[
                    "deployment_manifest_fingerprint_sha256"
                ],
                "teams_csv_sha256": manifest["teams_csv_sha256"],
                "team_count": manifest["team_count"],
                "first_team_id": manifest["first_team_id"],
                "enabled": manifest["enabled"],
                "symbol": manifest["symbol"],
                "leverage": manifest["leverage"],
                "target_ccbot_commit_sha": manifest["target_ccbot_commit_sha"],
                "import_requires_ccbot_dry_run": manifest[
                    "import_requires_ccbot_dry_run"
                ],
            },
            indent=2,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
