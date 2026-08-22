from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import Candle, FundingEvent, StrategySpec


def load_binance_klines_csv(path: str | Path) -> list[Candle]:
    """Load Binance 1m kline CSV, with or without a header row.

    Binance archive rows begin with:
    open_time, open, high, low, close, volume, close_time, ...
    """
    candles: list[Candle] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row_number, row in enumerate(reader, start=1):
            if not row:
                continue
            if row_number == 1 and not _looks_integer(row[0]):
                continue
            if len(row) < 7:
                raise ValueError(f"kline row {row_number} has fewer than 7 columns")
            candles.append(
                Candle(
                    open_time_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time_ms=int(row[6]),
                )
            )
    return candles


def load_funding_csv(path: str | Path) -> list[FundingEvent]:
    events: list[FundingEvent] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp_ms", "rate"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("funding CSV requires timestamp_ms,rate columns")
        for row in reader:
            mark_text = (row.get("mark_price") or "").strip()
            events.append(
                FundingEvent(
                    timestamp_ms=int(row["timestamp_ms"]),
                    rate=float(row["rate"]),
                    mark_price=None if not mark_text else float(mark_text),
                )
            )
    return events


def load_strategy_json(path: str | Path) -> StrategySpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return StrategySpec(**payload)


def write_json(path: str | Path, payload: object) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _looks_integer(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True
