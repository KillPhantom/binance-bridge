from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


Action = Literal["open_long", "open_short", "close_long", "close_short", "flatten"]


class TradingViewSignal(BaseModel):
    token: str = Field(min_length=1)
    event_id: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=30)
    action: Action
    notional: float | None = Field(default=None, gt=0)
    source: str = "tradingview"
    strategy: str | None = None
    retry: bool = False

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class Position(BaseModel):
    symbol: str
    amount: Decimal
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def side(self) -> Literal["long", "short", "flat"]:
        if self.amount > Decimal("0"):
            return "long"
        if self.amount < Decimal("0"):
            return "short"
        return "flat"


class ExecutionResult(BaseModel):
    summary: str
    position_before: dict[str, Any]
    position_after: dict[str, Any]
    binance_responses: list[dict[str, Any]] = Field(default_factory=list)
