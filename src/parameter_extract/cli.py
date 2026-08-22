from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .io import load_binance_klines_csv, load_funding_csv, load_strategy_json, write_json
from .manifest import build_manifest, read_checksum_file, verify_manifest
from .metrics import summarize
from .models import ExecutionModel
from .parity import check_parity_fixture
from .replay import run_strategy
from .study import run_study


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pextract")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="replay one strategy through the truth engine")
    replay.add_argument("--candles", required=True)
    replay.add_argument("--team", required=True, help="JSON StrategySpec")
    replay.add_argument("--funding")
    replay.add_argument("--model", choices=("frictionless", "expected", "stress"), default="expected")
    replay.add_argument("--min-trades", type=int, default=30)
    replay.add_argument("--trades", action="store_true", help="include every closed trade in JSON")

    manifest = sub.add_parser("manifest", help="fingerprint and audit historical input files")
    manifest.add_argument("--candles", required=True)
    manifest.add_argument("--funding")
    manifest.add_argument("--candles-checksum", help="Binance-style .CHECKSUM file")
    manifest.add_argument("--funding-checksum", help="Binance-style .CHECKSUM file")
    manifest.add_argument("--source", help="human-readable provenance, URL or archive label")
    manifest.add_argument("--output", required=True)

    verify = sub.add_parser("verify-manifest", help="re-hash files against a saved manifest")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--directory", required=True)

    parity = sub.add_parser("parity", help="compare extractor results with a frozen live-bot fixture")
    parity.add_argument("--fixture", required=True)

    study = sub.add_parser("study", help="evaluate one candidate over named research windows")
    study.add_argument("--study", dest="study_file", required=True)
    study.add_argument("--team", required=True)
    study.add_argument("--data-directory", required=True)
    study.add_argument("--reveal-holdout", action="store_true")
    study.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "replay":
        return _replay(args)
    if args.command == "manifest":
        return _manifest(args)
    if args.command == "verify-manifest":
        return _verify_manifest(args)
    if args.command == "parity":
        return _parity(args)
    if args.command == "study":
        return _study(args)
    return 2


def _replay(args: argparse.Namespace) -> int:
    candles = load_binance_klines_csv(args.candles)
    strategy = load_strategy_json(args.team)
    funding = [] if not args.funding else load_funding_csv(args.funding)
    models = {
        "frictionless": ExecutionModel.frictionless(),
        "expected": ExecutionModel.expected_live(),
        "stress": ExecutionModel.stress(),
    }
    result = run_strategy(candles, strategy, execution=models[args.model], funding=funding)
    payload: dict[str, object] = {
        "strategy": asdict(strategy),
        "execution_model": asdict(models[args.model]),
        "replay": {
            "raw_signal_count": result.raw_signal_count,
            "accepted_signal_count": result.accepted_signal_count,
            "skipped_while_open": result.skipped_while_open,
            "skipped_pending_entry": result.skipped_pending_entry,
            "cancelled_on_gap": result.cancelled_on_gap,
            "open_position": None if result.open_position is None else asdict(result.open_position),
        },
        "metrics": summarize(result, min_trades=args.min_trades).as_dict(),
    }
    if args.trades:
        payload["trades"] = [asdict(trade) for trade in result.trades]
    print(json.dumps(payload, indent=2, allow_nan=True))
    return 0


def _manifest(args: argparse.Namespace) -> int:
    candles = load_binance_klines_csv(args.candles)
    candle_expected = None if not args.candles_checksum else read_checksum_file(args.candles_checksum)
    funding_expected = None if not args.funding_checksum else read_checksum_file(args.funding_checksum)
    payload = build_manifest(
        candle_path=args.candles,
        candles=candles,
        funding_path=args.funding,
        candle_expected_sha256=candle_expected,
        funding_expected_sha256=funding_expected,
        source=args.source,
    )
    write_json(args.output, payload)
    print(json.dumps(payload, indent=2))
    checks = [payload["files"]["candles"], payload["files"].get("funding")]
    checksum_failed = any(
        isinstance(row, dict) and row.get("checksum_verified") is False for row in checks
    )
    return 1 if checksum_failed or not payload["candles"]["integrity_ok"] else 0


def _verify_manifest(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    problems = verify_manifest(payload, directory=args.directory)
    print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    return 0 if not problems else 1


def _parity(args: argparse.Namespace) -> int:
    report = check_parity_fixture(args.fixture)
    print(json.dumps(asdict(report), indent=2))
    return 0 if report.ok else 1


def _study(args: argparse.Namespace) -> int:
    payload = run_study(
        args.study_file,
        args.team,
        data_directory=args.data_directory,
        reveal_holdout=args.reveal_holdout,
    )
    if args.output:
        write_json(args.output, payload)
    print(json.dumps(payload, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
