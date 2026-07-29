from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .binance_client import BinanceAPIError, BinanceClient
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
        self._last_protected_reconcile: dict[int, float] = {}

    def stop(self) -> None:
        self._stopping.set()

    @staticmethod
    def _priority(bracket: dict) -> int:
        return 1 if bracket["status"] in {"protected", "monitoring"} else 0

    def _protected_reconcile_due(self, bracket_id: int, now: float) -> bool:
        last_checked = self._last_protected_reconcile.get(bracket_id)
        return (
            last_checked is None
            or now - last_checked >= self.settings.protected_reconcile_interval
        )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            for bracket in sorted(self.store.list_active_brackets(), key=self._priority):
                bracket_id = int(bracket["id"])
                if (
                    bracket["status"] in {"protected", "monitoring"}
                    and not self._protected_reconcile_due(bracket_id, loop.time())
                ):
                    continue
                try:
                    async with self.lock_for(bracket["symbol"]):
                        current = self.store.get_bracket(bracket["id"])
                        if current and current["status"] in {
                            "awaiting_entry",
                            "protecting",
                            "protected",
                            "monitoring",
                        }:
                            if current["status"] in {"protected", "monitoring"}:
                                if not self._protected_reconcile_due(
                                    bracket_id, loop.time()
                                ):
                                    continue
                                try:
                                    await self._process(current)
                                finally:
                                    self._last_protected_reconcile[
                                        bracket_id
                                    ] = loop.time()
                            else:
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
        elif bracket["status"] == "monitoring":
            await self._reconcile_protected(bracket)

    async def _process_entry(self, bracket: dict) -> None:
        order = await self.client.query_order(
            bracket["symbol"], int(bracket["entry_order_id"])
        )
        status = str(order.get("status", "")).upper()
        executed = Decimal(str(order.get("executedQty", "0")))
        if status == "PARTIALLY_FILLED":
            # MARKET entries do not leave a resting remainder like LIMIT GTC
            # entries. Protect the live position immediately; closePosition
            # algo orders will cover the whole position if matching continues.
            await self._begin_protection(bracket)
        elif status == "FILLED":
            await self._begin_protection(bracket)
        elif status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}:
            if executed > 0:
                await self._begin_protection(bracket)
            else:
                self.store.update_bracket(bracket["id"], status="entry_unfilled")
        elif status == "NEW" and self._entry_timed_out(bracket):
            await self.client.cancel_order(
                bracket["symbol"], int(bracket["entry_order_id"])
            )
            # Re-read after cancellation: the order may have filled while the
            # cancellation request was in flight.
            final_order = await self.client.query_order(
                bracket["symbol"], int(bracket["entry_order_id"])
            )
            final_status = str(final_order.get("status", "")).upper()
            final_executed = Decimal(str(final_order.get("executedQty", "0")))
            if final_executed > 0 or final_status == "FILLED":
                await self._begin_protection(bracket)
            elif final_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                self.store.update_bracket(
                    bracket["id"], status="entry_timed_out", error=None
                )
                logger.info(
                    "entry order timed out and was canceled bracket_id=%s symbol=%s order_id=%s",
                    bracket["id"],
                    bracket["symbol"],
                    bracket["entry_order_id"],
                )

    def _entry_timed_out(self, bracket: dict) -> bool:
        timeout = bracket.get("entry_timeout_seconds")
        if timeout is None:
            return False
        created_at = datetime.fromisoformat(str(bracket["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created_at).total_seconds()
        return age >= float(timeout)

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
        if not bracket.get("stop_loss_price") and not bracket.get("take_profit_price"):
            self.store.update_bracket(
                bracket["id"], status="monitoring", error=None
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
        try:
            if bracket.get("stop_algo_id") is None:
                if bracket.get("stop_loss_price"):
                    stop_price = await self._normalized_trigger(
                        bracket["symbol"], bracket["stop_loss_price"]
                    )
                    response = await self.client.place_close_position_algo_order(
                        bracket["symbol"],
                        exit_side,
                        "STOP_MARKET",
                        stop_price,
                        f"tvb{bracket_id}sl",
                        working_type=bracket.get("working_type"),
                    )
                    stop_algo_id = int(response["algoId"])
                    self.store.update_bracket(
                        bracket_id, stop_algo_id=stop_algo_id, error=None
                    )
                    bracket["stop_algo_id"] = stop_algo_id
            if bracket.get("take_profit_algo_id") is None:
                if bracket.get("take_profit_price"):
                    take_profit_price = await self._normalized_trigger(
                        bracket["symbol"], bracket["take_profit_price"]
                    )
                    response = await self.client.place_close_position_algo_order(
                        bracket["symbol"],
                        exit_side,
                        "TAKE_PROFIT_MARKET",
                        take_profit_price,
                        f"tvb{bracket_id}tp",
                        working_type=bracket.get("working_type"),
                    )
                    take_profit_algo_id = int(response["algoId"])
                    self.store.update_bracket(
                        bracket_id, take_profit_algo_id=take_profit_algo_id, error=None
                    )
                    bracket["take_profit_algo_id"] = take_profit_algo_id
        except BinanceAPIError as exc:
            if not self._is_immediate_trigger_error(exc):
                raise
            await self._close_immediately_triggered_position(bracket, exit_side, exc)
            return
        self.store.update_bracket(bracket_id, status="protected", error=None)
        logger.info(
            "bracket protected bracket_id=%s symbol=%s stop_algo_id=%s tp_algo_id=%s",
            bracket_id,
            bracket["symbol"],
            bracket["stop_algo_id"],
            bracket["take_profit_algo_id"],
        )

    @staticmethod
    def _is_immediate_trigger_error(exc: BinanceAPIError) -> bool:
        return (
            exc.status_code == 400
            and "would immediately trigger" in str(exc).lower()
        )

    async def _close_immediately_triggered_position(
        self, bracket: dict, exit_side: str, exc: BinanceAPIError
    ) -> None:
        bracket_id = int(bracket["id"])
        symbol = bracket["symbol"]
        expected = "long" if bracket["entry_side"] == "BUY" else "short"

        await self.client.cancel_all_algo_open_orders(symbol)
        position = await self.client.get_position(symbol)
        if position.side == expected:
            await self.client.place_market_order(
                symbol, exit_side, abs(position.amount), True
            )
            status = "closed"
            error = None
            logger.warning(
                "protection trigger already crossed; closed position with reduce-only market order "
                "bracket_id=%s symbol=%s side=%s quantity=%s error=%s",
                bracket_id,
                symbol,
                exit_side,
                abs(position.amount),
                exc,
            )
        else:
            status = "closed" if position.side == "flat" else "position_replaced"
            error = (
                None
                if position.side == "flat"
                else f"expected {expected}, found {position.side}"
            )
            logger.warning(
                "protection trigger already crossed, but position no longer matched bracket "
                "bracket_id=%s symbol=%s expected=%s found=%s error=%s",
                bracket_id,
                symbol,
                expected,
                position.side,
                exc,
            )

        self.store.update_bracket(
            bracket_id,
            status=status,
            stop_algo_id=None,
            take_profit_algo_id=None,
            error=error,
        )

    async def _reconcile_protected(self, bracket: dict) -> None:
        position = await self.client.get_position(bracket["symbol"])
        expected = "long" if bracket["entry_side"] == "BUY" else "short"
        if position.side == expected:
            return
        if bracket.get("source") == "manual" and bracket.get("entry_order_id"):
            try:
                order = await self.client.query_order(
                    bracket["symbol"], int(bracket["entry_order_id"])
                )
                if str(order.get("status", "")).upper() in {"NEW", "PARTIALLY_FILLED"}:
                    await self.client.cancel_order(
                        bracket["symbol"], int(bracket["entry_order_id"])
                    )
            except BinanceAPIError:
                logger.warning(
                    "could not cancel manual entry while reconciling bracket_id=%s",
                    bracket["id"],
                )
        await self.client.cancel_all_algo_open_orders(bracket["symbol"])
        status = "closed" if position.side == "flat" else "position_replaced"
        self.store.update_bracket(bracket["id"], status=status, error=None)
