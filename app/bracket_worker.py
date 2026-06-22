from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from decimal import Decimal
from typing import Callable

from .binance_client import BinanceClient
from .config import Settings
from .db import EventStore
from .utils import round_step_size


logger = logging.getLogger(__name__)


class BracketWorker:
    def __init__(
        self,
        client: BinanceClient,
        store: EventStore,
        settings: Settings,
        lock_for: Callable[[str], asyncio.Lock] | None = None,
    ):
        self.client = client
        self.store = store
        self.settings = settings
        self._own_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.lock_for = lock_for or (lambda symbol: self._own_locks[symbol])
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        while not self._stopping.is_set():
            for bracket in self.store.list_active_brackets():
                try:
                    async with self.lock_for(bracket["symbol"]):
                        current = self.store.get_bracket(bracket["id"])
                        if current and current["status"] in {
                            "awaiting_entry",
                            "protecting",
                            "protected",
                        }:
                            await self._process(current)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "bracket reconciliation failed bracket_id=%s symbol=%s",
                        bracket["id"],
                        bracket["symbol"],
                    )
                    self.store.update_bracket(
                        bracket["id"], error=f"{type(exc).__name__}: {exc}"[:4000]
                    )
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.bracket_poll_interval
                )

    async def _process(self, bracket: dict) -> None:
        if bracket["status"] == "awaiting_entry":
            await self._process_entry(bracket)
        elif bracket["status"] == "protecting":
            await self._install_protection(bracket)
        elif bracket["status"] == "protected":
            await self._reconcile_protected(bracket)

    async def _process_entry(self, bracket: dict) -> None:
        order = await self.client.query_order(
            bracket["symbol"], int(bracket["entry_order_id"])
        )
        status = str(order.get("status", "")).upper()
        executed = Decimal(str(order.get("executedQty", "0")))
        if status == "PARTIALLY_FILLED":
            await self.client.cancel_order(
                bracket["symbol"], int(bracket["entry_order_id"])
            )
            await self._begin_protection(bracket)
        elif status == "FILLED":
            await self._begin_protection(bracket)
        elif status in {"CANCELED", "EXPIRED", "REJECTED"}:
            if executed > 0:
                await self._begin_protection(bracket)
            else:
                self.store.update_bracket(bracket["id"], status="entry_unfilled")

    async def _begin_protection(self, bracket: dict) -> None:
        position = await self.client.get_position(bracket["symbol"])
        expected = "long" if bracket["entry_side"] == "BUY" else "short"
        if position.side != expected:
            self.store.update_bracket(
                bracket["id"],
                status="position_mismatch",
                error=f"expected {expected}, found {position.side}",
            )
            return
        await self.client.cancel_all_algo_open_orders(bracket["symbol"])
        self.store.update_bracket(bracket["id"], status="protecting", error=None)
        bracket["status"] = "protecting"
        await self._install_protection(bracket)

    async def _normalized_trigger(self, symbol: str, value: str) -> Decimal:
        filters = await self.client.get_symbol_filters(symbol)
        price_filter = filters.get("PRICE_FILTER")
        if not price_filter:
            raise ValueError(f"PRICE_FILTER unavailable for {symbol}")
        return round_step_size(Decimal(value), Decimal(price_filter["tickSize"]))

    async def _install_protection(self, bracket: dict) -> None:
        exit_side = "SELL" if bracket["entry_side"] == "BUY" else "BUY"
        bracket_id = int(bracket["id"])
        if bracket.get("stop_algo_id") is None:
            stop_price = await self._normalized_trigger(
                bracket["symbol"], bracket["stop_loss_price"]
            )
            response = await self.client.place_close_position_algo_order(
                bracket["symbol"],
                exit_side,
                "STOP_MARKET",
                stop_price,
                f"tvb{bracket_id}sl",
            )
            stop_algo_id = int(response["algoId"])
            self.store.update_bracket(bracket_id, stop_algo_id=stop_algo_id, error=None)
            bracket["stop_algo_id"] = stop_algo_id
        if bracket.get("take_profit_algo_id") is None:
            take_profit_price = await self._normalized_trigger(
                bracket["symbol"], bracket["take_profit_price"]
            )
            response = await self.client.place_close_position_algo_order(
                bracket["symbol"],
                exit_side,
                "TAKE_PROFIT_MARKET",
                take_profit_price,
                f"tvb{bracket_id}tp",
            )
            take_profit_algo_id = int(response["algoId"])
            self.store.update_bracket(
                bracket_id, take_profit_algo_id=take_profit_algo_id, error=None
            )
            bracket["take_profit_algo_id"] = take_profit_algo_id
        self.store.update_bracket(bracket_id, status="protected", error=None)
        logger.info(
            "bracket protected bracket_id=%s symbol=%s stop_algo_id=%s tp_algo_id=%s",
            bracket_id,
            bracket["symbol"],
            bracket["stop_algo_id"],
            bracket["take_profit_algo_id"],
        )

    async def _reconcile_protected(self, bracket: dict) -> None:
        position = await self.client.get_position(bracket["symbol"])
        expected = "long" if bracket["entry_side"] == "BUY" else "short"
        if position.side == expected:
            return
        await self.client.cancel_all_algo_open_orders(bracket["symbol"])
        status = "closed" if position.side == "flat" else "position_replaced"
        self.store.update_bracket(bracket["id"], status=status, error=None)
