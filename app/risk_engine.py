from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import Any

from .binance_client import BinanceClient
from .config import Settings
from .models import ExecutionResult, Position, TradingViewSignal
from .utils import round_step_size


class PositionTimeoutError(RuntimeError):
    pass


class RiskEngine:
    def __init__(self, client: BinanceClient, settings: Settings):
        self.client = client
        self.settings = settings
        self._symbol_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle_signal(self, signal: TradingViewSignal) -> ExecutionResult:
        async with self._symbol_locks[signal.symbol]:
            if signal.reduce_only:
                return await self.reduce_order(
                    signal.symbol, signal.side, signal.price, signal.amount
                )
            return await self.open_order(
                signal.symbol, signal.side, signal.price, signal.amount
            )

    @staticmethod
    def _position_dict(position: Position) -> dict[str, Any]:
        return {"symbol": position.symbol, "positionAmt": str(position.amount), "side": position.side}

    async def _wait_flat(self, symbol: str) -> Position:
        if self.client.dry_run:
            return Position(symbol=symbol, amount=Decimal("0"), raw={"dryRun": True})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.position_poll_seconds
        while True:
            position = await self.client.get_position(symbol)
            if position.amount == Decimal("0"):
                return position
            if loop.time() >= deadline:
                raise PositionTimeoutError(f"{symbol} did not become flat before timeout")
            await asyncio.sleep(self.settings.position_poll_interval)

    async def _normalize_limit_order(
        self,
        symbol: str,
        price: Decimal,
        notional_amount: Decimal,
        maximum_quantity: Decimal | None = None,
        validate_minimum_notional: bool = True,
    ) -> tuple[Decimal, Decimal]:
        filters = await self.client.get_symbol_filters(symbol)
        lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
        if not lot:
            raise ValueError(f"LOT_SIZE filter unavailable for {symbol}")
        price_filter = filters.get("PRICE_FILTER")
        if not price_filter:
            raise ValueError(f"PRICE_FILTER unavailable for {symbol}")
        quantity = round_step_size(
            notional_amount / price, Decimal(lot["stepSize"])
        )
        if maximum_quantity is not None:
            quantity = min(quantity, maximum_quantity)
            quantity = round_step_size(quantity, Decimal(lot["stepSize"]))
        normalized_price = round_step_size(price, Decimal(price_filter["tickSize"]))
        minimum = Decimal(lot["minQty"])
        if quantity < minimum:
            raise ValueError(f"order amount {quantity} is below minimum {minimum}")
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
        if notional_filter and validate_minimum_notional:
            minimum_notional = Decimal(str(notional_filter.get("notional") or notional_filter.get("minNotional") or "0"))
            if quantity * normalized_price < minimum_notional:
                raise ValueError("limit order is below Binance minimum notional")
        return normalized_price, quantity

    async def open_order(
        self, symbol: str, side: str, price: Decimal, amount: Decimal
    ) -> ExecutionResult:
        target = "long" if side == "buy" else "short"
        responses: list[dict[str, Any]] = []
        responses.append(await self.client.cancel_all_open_orders(symbol))
        before = await self.client.get_position(symbol)
        position = before
        opposite = (target == "long" and position.amount < 0) or (target == "short" and position.amount > 0)
        if opposite:
            close_side = "BUY" if position.amount < 0 else "SELL"
            responses.append(await self.client.place_market_order(
                symbol, close_side, abs(position.amount), True
            ))
            position = await self._wait_flat(symbol)
        elif position.side == target and not self.settings.allow_add:
            after = await self.client.get_position(symbol)
            return ExecutionResult(
                summary=f"already {target}; ALLOW_ADD=false, no opening order placed",
                position_before=self._position_dict(before),
                position_after=self._position_dict(after),
                binance_responses=responses,
            )
        normalized_price, quantity = await self._normalize_limit_order(symbol, price, amount)
        binance_side = side.upper()
        responses.append(
            await self.client.place_limit_order(
                symbol, binance_side, quantity, normalized_price
            )
        )
        after = await self.client.get_position(symbol)
        return ExecutionResult(
            summary=(
                f"{target} LIMIT GTC order submitted for {quantity} {symbol} "
                f"at {normalized_price}"
            ),
            position_before=self._position_dict(before),
            position_after=self._position_dict(after),
            binance_responses=responses,
        )

    async def reduce_order(
        self, symbol: str, side: str, price: Decimal, amount: Decimal
    ) -> ExecutionResult:
        responses: list[dict[str, Any]] = []
        order_side = side.upper()
        opening_side_to_cancel = "BUY" if order_side == "SELL" else "SELL"
        responses.append(
            await self.client.cancel_opening_orders(
                symbol, opening_side_to_cancel
            )
        )
        before = await self.client.get_position(symbol)
        matches = (side == "sell" and before.amount > 0) or (
            side == "buy" and before.amount < 0
        )
        if matches:
            normalized_price, quantity = await self._normalize_limit_order(
                symbol,
                price,
                amount,
                maximum_quantity=abs(before.amount),
                validate_minimum_notional=False,
            )
            responses.append(
                await self.client.place_reduce_only_limit_order(
                    symbol, order_side, quantity, normalized_price
                )
            )
        after = await self.client.get_position(symbol)
        action = (
            "reduce-only LIMIT GTC order submitted"
            if matches
            else "no matching position; no reduce-only order placed"
        )
        return ExecutionResult(
            summary=f"{side.upper()} {action}",
            position_before=self._position_dict(before),
            position_after=self._position_dict(after),
            binance_responses=responses,
        )
