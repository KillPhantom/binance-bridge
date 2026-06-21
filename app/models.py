from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TradingViewSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    event_id: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=30)
    side: Literal["buy", "sell"]
    position_side: Literal["BOTH"] = Field(alias="positionSide")
    investment_type: Literal["notional_value"] = Field(alias="investmentType")
    price: Decimal = Field(gt=0)
    amount: Decimal = Field(gt=0)
    reduce_only: bool = Field(alias="reduceOnly")
    position_mode: Literal["one_way_mode"] | None = Field(
        default=None, alias="positionMode"
    )
    action: Literal["buy", "sell"] | None = None
    notional: Decimal | None = Field(default=None, gt=0)
    source: str = "tradingview"
    strategy: str | None = None
    retry: bool = False

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("side", "action", mode="before")
    @classmethod
    def normalize_side(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_compatibility_fields(self) -> "TradingViewSignal":
        if self.action is not None and self.action != self.side:
            raise ValueError("action must match side")
        if self.notional is not None and self.notional != self.amount:
            raise ValueError("notional must match amount for notional_value")
        return self


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
