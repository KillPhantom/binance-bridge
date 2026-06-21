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
            if signal.action == "open_long":
                return await self.open_long(signal.symbol, signal.price, signal.amount)
            if signal.action == "open_short":
                return await self.open_short(signal.symbol, signal.price, signal.amount)
            if signal.action == "close_long":
                return await self.close_long(signal.symbol)
            if signal.action == "close_short":
                return await self.close_short(signal.symbol)
            return await self.flatten(signal.symbol)

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
        self, symbol: str, price: Decimal, amount: Decimal
    ) -> tuple[Decimal, Decimal]:
        filters = await self.client.get_symbol_filters(symbol)
        lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
        if not lot:
            raise ValueError(f"LOT_SIZE filter unavailable for {symbol}")
        price_filter = filters.get("PRICE_FILTER")
        if not price_filter:
            raise ValueError(f"PRICE_FILTER unavailable for {symbol}")
        quantity = round_step_size(amount, Decimal(lot["stepSize"]))
        normalized_price = round_step_size(price, Decimal(price_filter["tickSize"]))
        minimum = Decimal(lot["minQty"])
        if quantity < minimum:
            raise ValueError(f"order amount {quantity} is below minimum {minimum}")
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
        if notional_filter:
            minimum_notional = Decimal(str(notional_filter.get("notional") or notional_filter.get("minNotional") or "0"))
            if quantity * normalized_price < minimum_notional:
                raise ValueError("limit order is below Binance minimum notional")
        return normalized_price, quantity

    async def _open(
        self, symbol: str, price: Decimal | None, amount: Decimal | None, target: str
    ) -> ExecutionResult:
        if price is None or amount is None:
            raise ValueError("open actions require price and amount")
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
        side = "BUY" if target == "long" else "SELL"
        responses.append(
            await self.client.place_limit_order(
                symbol, side, quantity, normalized_price, False
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

    async def open_long(
        self, symbol: str, price: Decimal, amount: Decimal
    ) -> ExecutionResult:
        return await self._open(symbol, price, amount, "long")

    async def open_short(
        self, symbol: str, price: Decimal, amount: Decimal
    ) -> ExecutionResult:
        return await self._open(symbol, price, amount, "short")

    async def _close(self, symbol: str, target: str) -> ExecutionResult:
        responses: list[dict[str, Any]] = []
        opening_side = "BUY" if target == "long" else "SELL"
        responses.append(await self.client.cancel_opening_orders(symbol, opening_side))
        before = await self.client.get_position(symbol)
        matches = (target == "long" and before.amount > 0) or (target == "short" and before.amount < 0)
        if matches:
            side = "SELL" if target == "long" else "BUY"
            responses.append(await self.client.place_market_order(
                symbol, side, abs(before.amount), True
            ))
        after = await self.client.get_position(symbol)
        action = "close submitted" if matches else "already flat or opposite; no close order placed"
        return ExecutionResult(
            summary=f"{target} {action}",
            position_before=self._position_dict(before),
            position_after=self._position_dict(after),
            binance_responses=responses,
        )

    async def close_long(self, symbol: str) -> ExecutionResult:
        return await self._close(symbol, "long")

    async def close_short(self, symbol: str) -> ExecutionResult:
        return await self._close(symbol, "short")

    async def flatten(self, symbol: str) -> ExecutionResult:
        responses = [await self.client.cancel_all_open_orders(symbol)]
        before = await self.client.get_position(symbol)
        if before.amount != Decimal("0"):
            side = "SELL" if before.amount > 0 else "BUY"
            responses.append(await self.client.place_market_order(
                symbol, side, abs(before.amount), True
            ))
            await self._wait_flat(symbol)
        responses.append(await self.client.cancel_all_open_orders(symbol))
        after = await self.client.get_position(symbol)
        return ExecutionResult(
            summary="symbol flattened",
            position_before=self._position_dict(before),
            position_after=self._position_dict(after),
            binance_responses=responses,
        )
