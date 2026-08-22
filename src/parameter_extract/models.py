from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RSI_PERIOD_CHOICES = (14, 15, 16, 17, 18, 19)
ADX_PERIOD = 14
ADR_PERIOD = 14
ExitMode = Literal["rsi", "tp"]
EntryTiming = Literal["signal_close", "next_open"]


@dataclass(frozen=True, slots=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.open_time_ms < 0 or self.close_time_ms <= self.open_time_ms:
            raise ValueError("candle timestamps are invalid")
        if min(self.open, self.high, self.low, self.close) <= 0.0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.volume < 0.0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True, slots=True)
class FundingEvent:
    timestamp_ms: int
    rate: float
    mark_price: float | None = None

    def __post_init__(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("funding timestamp cannot be negative")
        if self.mark_price is not None and self.mark_price <= 0.0:
            raise ValueError("funding mark price must be positive when supplied")


@dataclass(frozen=True, slots=True)
class StrategySpec:
    symbol: str
    rsi_period: int
    rsi_entry: float
    adx_min: float
    adx_max: float
    exit_mode: ExitMode
    rsi_exit: float | None = None
    tp_price_pct: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.isalnum() or not self.symbol.isupper():
            raise ValueError("symbol must be an upper-case alphanumeric pair")
        if not self.symbol.endswith("USDT"):
            raise ValueError("symbol must be a USDT pair")
        if self.rsi_period not in RSI_PERIOD_CHOICES:
            raise ValueError(f"rsi_period must be one of {RSI_PERIOD_CHOICES}")
        if not 0.0 < self.rsi_entry < 100.0:
            raise ValueError("rsi_entry must be strictly inside (0, 100)")
        if not 0.0 <= self.adx_min < self.adx_max <= 100.0:
            raise ValueError("ADX bounds must satisfy 0 <= min < max <= 100")
        if self.exit_mode == "rsi":
            if self.rsi_exit is None or not 0.0 < self.rsi_exit < 100.0:
                raise ValueError("RSI exit mode requires rsi_exit inside (0, 100)")
            if self.rsi_exit <= self.rsi_entry:
                raise ValueError("rsi_exit must be above rsi_entry")
        elif self.exit_mode == "tp":
            if self.tp_price_pct is None or not 0.0 < self.tp_price_pct <= 100.0:
                raise ValueError("TP exit mode requires tp_price_pct inside (0, 100]")
        else:
            raise ValueError("exit_mode must be 'rsi' or 'tp'")


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    """How a historical signal is translated into a plausible market fill.

    Slippage is always adverse: buys are moved upward and sells downward. Fee values are
    charged on both entry and exit notionals. `next_open` deliberately separates a closed
    candle signal from execution instead of pretending the closing print was a fill.
    """

    name: str
    entry_timing: EntryTiming
    taker_fee_bps: float
    buy_slippage_bps: float
    sell_slippage_bps: float

    def __post_init__(self) -> None:
        if self.entry_timing not in {"signal_close", "next_open"}:
            raise ValueError("entry_timing must be signal_close or next_open")
        for field_name in ("taker_fee_bps", "buy_slippage_bps", "sell_slippage_bps"):
            if getattr(self, field_name) < 0.0:
                raise ValueError(f"{field_name} cannot be negative")

    @classmethod
    def frictionless(cls) -> "ExecutionModel":
        return cls(
            name="frictionless",
            entry_timing="signal_close",
            taker_fee_bps=0.0,
            buy_slippage_bps=0.0,
            sell_slippage_bps=0.0,
        )

    @classmethod
    def expected_live(
        cls,
        *,
        taker_fee_bps: float = 4.0,
        buy_slippage_bps: float = 2.0,
        sell_slippage_bps: float = 2.0,
    ) -> "ExecutionModel":
        return cls(
            name="expected_live",
            entry_timing="next_open",
            taker_fee_bps=taker_fee_bps,
            buy_slippage_bps=buy_slippage_bps,
            sell_slippage_bps=sell_slippage_bps,
        )

    @classmethod
    def stress(
        cls,
        *,
        taker_fee_bps: float = 5.0,
        buy_slippage_bps: float = 8.0,
        sell_slippage_bps: float = 8.0,
    ) -> "ExecutionModel":
        return cls(
            name="stress",
            entry_timing="next_open",
            taker_fee_bps=taker_fee_bps,
            buy_slippage_bps=buy_slippage_bps,
            sell_slippage_bps=sell_slippage_bps,
        )
