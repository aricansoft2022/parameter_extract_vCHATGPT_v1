from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .io import load_binance_klines_csv, load_funding_csv, load_strategy_json
from .metrics import summarize
from .models import ExecutionModel
from .replay import run_strategy


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "replay":
        return 2

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


if __name__ == "__main__":
    raise SystemExit(main())
