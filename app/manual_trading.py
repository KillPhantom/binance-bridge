from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .binance_client import BinanceAPIError
from .models import Position
from .utils import decimal_string, round_step_size


MANUAL_SYMBOL = "ETHUSDT"


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ManualTradingError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class RequestIdModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=_to_camel)

    client_request_id: str = Field(min_length=8, max_length=100)

    @field_validator("client_request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in normalized
        ):
            raise ValueError("clientRequestId contains unsupported characters")
        return normalized


class ManualOrderRequest(RequestIdModel):
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"]
    amount: Decimal = Field(gt=0)
    amount_unit: Literal["USDT", "ETH"]
    limit_price: Decimal | None = Field(default=None, gt=0)
    leverage: int = Field(ge=1, le=125)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_limit_price(self) -> "ManualOrderRequest":
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("limitPrice is required for LIMIT orders")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("limitPrice is only valid for LIMIT orders")
        return self


class ManualProtectionRequest(RequestIdModel):
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal | None = Field(default=None, gt=0)


class ManualCancelRequest(RequestIdModel):
    pass


class ManualCloseRequest(RequestIdModel):
    pass


class ManualTradingService:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.client = runtime.client
        self.store = runtime.store
        self.settings = runtime.settings
        self.symbol = MANUAL_SYMBOL

    @staticmethod
    def _client_order_id(request_id: str, action: str) -> str:
        digest = hashlib.sha256(f"{action}:{request_id}".encode()).hexdigest()[:28]
        return f"mt{digest}"

    @staticmethod
    def _json_payload(model: BaseModel) -> dict[str, Any]:
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def _action(
        self,
        request_id: str,
        action: str,
        payload: dict[str, Any],
        execute: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        claim = self.store.claim_manual_action(request_id, action, payload)
        if claim == "duplicate_success":
            row = self.store.get_manual_action(request_id)
            if row and row.get("response_json"):
                response = json.loads(row["response_json"])
                response["duplicate"] = True
                return response
            raise ManualTradingError(409, "completed request has no stored response")
        if claim == "processing":
            raise ManualTradingError(409, "request is already processing")
        if claim == "failed_needs_retry":
            raise ManualTradingError(
                409, "request previously failed; confirm again with a new request id"
            )
        try:
            response = await execute()
        except Exception as exc:
            self.store.finish_manual_action(
                request_id, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        self.store.finish_manual_action(request_id, response=response)
        return response

    async def state(self) -> dict[str, Any]:
        mark_price, position, open_orders, open_algos = await asyncio.gather(
            self.client.get_mark_price(self.symbol),
            self.client.get_position(self.symbol),
            self.client.get_open_orders(self.symbol),
            self._safe_open_algos(),
        )
        recorded = {
            int(order["order_id"]): order
            for order in self.store.list_manual_orders(self.symbol)
        }
        pending_orders = []
        for order in open_orders:
            try:
                order_id = int(order["orderId"])
            except (KeyError, TypeError, ValueError):
                continue
            record = recorded.get(order_id)
            if record is None:
                continue
            status = str(order.get("status", record["status"])).upper()
            self.store.update_manual_order_status(order_id, status)
            pending_orders.append(
                {
                    "orderId": order_id,
                    "clientOrderId": record["client_order_id"],
                    "side": order.get("side", record["side"]),
                    "orderType": order.get("type", record["order_type"]),
                    "price": str(order.get("price") or record["price"] or "0"),
                    "quantity": str(
                        order.get("origQty") or record["quantity"] or "0"
                    ),
                    "executedQuantity": str(order.get("executedQty") or "0"),
                    "status": status,
                }
            )

        raw = position.raw
        active = self.store.get_active_bracket(self.symbol)
        protection = None
        if active and position.side != "flat":
            protection = {
                "source": active.get("source"),
                "status": active.get("status"),
                "stopLossPrice": active.get("stop_loss_price"),
                "takeProfitPrice": active.get("take_profit_price"),
                "workingType": active.get("working_type"),
            }
        if position.side != "flat":
            exit_side = "SELL" if position.side == "long" else "BUY"
            external_stop = None
            external_take = None
            external_working_type = None
            for order in open_algos:
                if str(order.get("side") or "").upper() != exit_side:
                    continue
                if not self.client._is_true(order.get("closePosition")):
                    continue
                order_type = str(order.get("type") or "").upper()
                trigger = order.get("triggerPrice") or order.get("stopPrice")
                if order_type == "STOP_MARKET":
                    external_stop = str(trigger) if trigger is not None else None
                elif order_type == "TAKE_PROFIT_MARKET":
                    external_take = str(trigger) if trigger is not None else None
                external_working_type = (
                    order.get("workingType") or external_working_type
                )
            if external_stop or external_take:
                protection = protection or {
                    "source": "binance",
                    "status": "external",
                    "stopLossPrice": None,
                    "takeProfitPrice": None,
                    "workingType": external_working_type or "MARK_PRICE",
                }
                protection["stopLossPrice"] = (
                    external_stop or protection.get("stopLossPrice")
                )
                protection["takeProfitPrice"] = (
                    external_take or protection.get("takeProfitPrice")
                )
        return {
            "account": self.runtime.name,
            "symbol": self.symbol,
            "markPrice": decimal_string(mark_price),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "position": {
                "side": position.side,
                "quantity": decimal_string(abs(position.amount)),
                "signedQuantity": decimal_string(position.amount),
                "entryPrice": str(raw.get("entryPrice") or "0"),
                "markPrice": str(raw.get("markPrice") or decimal_string(mark_price)),
                "unrealizedPnl": str(
                    raw.get("unRealizedProfit")
                    or raw.get("unrealizedProfit")
                    or "0"
                ),
                "liquidationPrice": str(raw.get("liquidationPrice") or "0"),
                "leverage": int(Decimal(str(raw.get("leverage") or "1"))),
                "marginType": str(raw.get("marginType") or "cross").lower(),
                "notional": str(raw.get("notional") or "0"),
            },
            "pendingOrders": pending_orders,
            "protection": protection,
        }

    async def open_order(self, request: ManualOrderRequest) -> dict[str, Any]:
        async def execute() -> dict[str, Any]:
            async with self.runtime.engine.lock_for(self.symbol):
                mark_price = await self.client.get_mark_price(self.symbol)
                reference_price = request.limit_price or mark_price
                self._validate_protection(
                    request.side,
                    reference_price,
                    request.stop_loss_price,
                    request.take_profit_price,
                )
                quantity = await self._normalize_quantity(
                    request.amount, request.amount_unit, reference_price
                )
                before = await self.client.get_position(self.symbol)
                expected = "long" if request.side == "BUY" else "short"

                await self.client.change_leverage(self.symbol, request.leverage)
                await self.client.cancel_all_open_orders(self.symbol)

                if before.side not in {"flat", expected}:
                    await self.client.cancel_all_algo_open_orders(self.symbol)
                    self.store.deactivate_active_brackets(
                        self.symbol, "manual_reduce"
                    )
                    await self.client.place_market_order(
                        self.symbol,
                        "BUY" if before.side == "short" else "SELL",
                        abs(before.amount),
                        True,
                    )
                    await self._wait_for_side("flat")
                    before = Position(symbol=self.symbol, amount=Decimal("0"))

                client_order_id = self._client_order_id(
                    request.client_request_id, "open"
                )
                if request.order_type == "MARKET":
                    order = await self._submit_with_recovery(
                        client_order_id,
                        lambda: self.client.place_market_order(
                            self.symbol,
                            request.side,
                            quantity,
                            False,
                            client_order_id,
                        ),
                    )
                else:
                    order = await self._submit_with_recovery(
                        client_order_id,
                        lambda: self.client.place_limit_order(
                            self.symbol,
                            request.side,
                            quantity,
                            request.limit_price,
                            client_order_id,
                        ),
                    )
                order_id = int(order["orderId"])
                status = str(order.get("status") or "NEW").upper()
                self.store.record_manual_order(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    request_id=request.client_request_id,
                    symbol=self.symbol,
                    side=request.side,
                    order_type=request.order_type,
                    quantity=decimal_string(quantity),
                    price=(
                        decimal_string(request.limit_price)
                        if request.limit_price is not None
                        else None
                    ),
                    status=status,
                )

                if request.order_type == "LIMIT" and status not in {
                    "FILLED",
                    "PARTIALLY_FILLED",
                }:
                    if before.side == expected:
                        await self._replace_protection(
                            before,
                            request.stop_loss_price,
                            request.take_profit_price,
                            request.client_request_id,
                            entry_order_id=order_id,
                            entry_client_order_id=client_order_id,
                            monitor=True,
                        )
                    else:
                        self.store.create_manual_bracket(
                            request.client_request_id,
                            self.symbol,
                            order_id,
                            client_order_id,
                            request.side,
                            self._optional_decimal(request.stop_loss_price),
                            self._optional_decimal(request.take_profit_price),
                            status="awaiting_entry",
                        )
                else:
                    position = await self._wait_for_side(expected)
                    try:
                        await self._replace_protection(
                            position,
                            request.stop_loss_price,
                            request.take_profit_price,
                            request.client_request_id,
                            entry_order_id=(
                                order_id if request.order_type == "LIMIT" else None
                            ),
                            entry_client_order_id=(
                                client_order_id
                                if request.order_type == "LIMIT"
                                else None
                            ),
                            monitor=request.order_type == "LIMIT",
                        )
                    except Exception:
                        if (
                            request.stop_loss_price is not None
                            or request.take_profit_price is not None
                        ):
                            await self.client.cancel_all_algo_open_orders(self.symbol)
                            live = await self.client.get_position(self.symbol)
                            if live.side == expected:
                                await self.client.place_market_order(
                                    self.symbol,
                                    "SELL" if expected == "long" else "BUY",
                                    abs(live.amount),
                                    True,
                                )
                        raise

                return {
                    "ok": True,
                    "requestId": request.client_request_id,
                    "order": {
                        "orderId": order_id,
                        "clientOrderId": client_order_id,
                        "status": status,
                        "side": request.side,
                        "orderType": request.order_type,
                        "quantity": decimal_string(quantity),
                        "price": (
                            decimal_string(request.limit_price)
                            if request.limit_price is not None
                            else None
                        ),
                    },
                }

        return await self._action(
            request.client_request_id,
            "open_order",
            self._json_payload(request),
            execute,
        )

    async def cancel_order(
        self, order_id: int, request: ManualCancelRequest
    ) -> dict[str, Any]:
        async def execute() -> dict[str, Any]:
            async with self.runtime.engine.lock_for(self.symbol):
                record = self.store.get_manual_order(order_id)
                if record is None or record["symbol"] != self.symbol:
                    raise ManualTradingError(404, "manual order not found")
                response = await self.client.cancel_order(self.symbol, order_id)
                self.store.update_manual_order_status(order_id, "CANCELED")
                active = self.store.get_active_bracket(self.symbol)
                if active and active.get("entry_order_id") == order_id:
                    self.store.update_bracket(
                        int(active["id"]), status="entry_canceled", error=None
                    )
                return {
                    "ok": True,
                    "requestId": request.client_request_id,
                    "orderId": order_id,
                    "status": str(response.get("status") or "CANCELED"),
                }

        return await self._action(
            request.client_request_id,
            "cancel_order",
            {"orderId": order_id, **self._json_payload(request)},
            execute,
        )

    async def close_position(
        self, request: ManualCloseRequest
    ) -> dict[str, Any]:
        async def execute() -> dict[str, Any]:
            async with self.runtime.engine.lock_for(self.symbol):
                await self.client.cancel_all_open_orders(self.symbol)
                await self.client.cancel_all_algo_open_orders(self.symbol)
                self.store.deactivate_active_brackets(
                    self.symbol, "manual_reduce"
                )
                position = await self.client.get_position(self.symbol)
                order = None
                if position.side != "flat":
                    client_order_id = self._client_order_id(
                        request.client_request_id, "close"
                    )
                    order = await self._submit_with_recovery(
                        client_order_id,
                        lambda: self.client.place_market_order(
                            self.symbol,
                            "SELL" if position.side == "long" else "BUY",
                            abs(position.amount),
                            True,
                            client_order_id,
                        ),
                    )
                    await self._wait_for_side("flat")
                return {
                    "ok": True,
                    "requestId": request.client_request_id,
                    "status": "closed" if order else "already_flat",
                    "orderId": order.get("orderId") if order else None,
                }

        return await self._action(
            request.client_request_id,
            "close_position",
            self._json_payload(request),
            execute,
        )

    async def update_protection(
        self, request: ManualProtectionRequest
    ) -> dict[str, Any]:
        async def execute() -> dict[str, Any]:
            async with self.runtime.engine.lock_for(self.symbol):
                position = await self.client.get_position(self.symbol)
                if position.side == "flat":
                    raise ManualTradingError(409, "cannot protect a flat position")
                mark_price = await self.client.get_mark_price(self.symbol)
                side = "BUY" if position.side == "long" else "SELL"
                self._validate_protection(
                    side,
                    mark_price,
                    request.stop_loss_price,
                    request.take_profit_price,
                )
                active = self.store.get_active_bracket(self.symbol)
                bracket = await self._replace_protection(
                    position,
                    request.stop_loss_price,
                    request.take_profit_price,
                    request.client_request_id,
                    entry_order_id=active.get("entry_order_id") if active else None,
                    entry_client_order_id=(
                        active.get("entry_client_order_id") if active else None
                    ),
                    monitor=bool(active and active.get("entry_order_id")),
                )
                return {
                    "ok": True,
                    "requestId": request.client_request_id,
                    "protection": (
                        {
                            "stopLossPrice": bracket.get("stop_loss_price"),
                            "takeProfitPrice": bracket.get("take_profit_price"),
                            "workingType": "MARK_PRICE",
                        }
                        if bracket
                        else None
                    ),
                }

        return await self._action(
            request.client_request_id,
            "update_protection",
            self._json_payload(request),
            execute,
        )

    async def _normalize_quantity(
        self, amount: Decimal, unit: str, reference_price: Decimal
    ) -> Decimal:
        filters = await self.client.get_symbol_filters(self.symbol)
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
        if not lot:
            raise ValueError("Binance quantity filter is unavailable")
        raw_quantity = amount / reference_price if unit == "USDT" else amount
        quantity = round_step_size(raw_quantity, Decimal(str(lot["stepSize"])))
        if quantity < Decimal(str(lot["minQty"])):
            raise ValueError("quantity is below Binance minimum")
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
        if notional_filter:
            minimum_notional = Decimal(
                str(
                    notional_filter.get("notional")
                    or notional_filter.get("minNotional")
                    or "0"
                )
            )
            if quantity * reference_price < minimum_notional:
                raise ValueError("order is below Binance minimum notional")
        return quantity

    async def _normalized_trigger(self, value: Decimal) -> Decimal:
        filters = await self.client.get_symbol_filters(self.symbol)
        price_filter = filters.get("PRICE_FILTER")
        if not price_filter:
            raise ValueError("Binance price filter is unavailable")
        return round_step_size(value, Decimal(str(price_filter["tickSize"])))

    @staticmethod
    def _validate_protection(
        side: str,
        reference_price: Decimal,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
    ) -> None:
        if side == "BUY":
            if stop_loss is not None and stop_loss >= reference_price:
                raise ValueError("long stop loss must be below the reference price")
            if take_profit is not None and take_profit <= reference_price:
                raise ValueError("long take profit must be above the reference price")
        else:
            if stop_loss is not None and stop_loss <= reference_price:
                raise ValueError("short stop loss must be above the reference price")
            if take_profit is not None and take_profit >= reference_price:
                raise ValueError("short take profit must be below the reference price")

    async def _replace_protection(
        self,
        position: Position,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        event_id: str,
        *,
        entry_order_id: int | None,
        entry_client_order_id: str | None,
        monitor: bool,
    ) -> dict[str, Any] | None:
        active = self.store.get_active_bracket(self.symbol)
        open_algos = await self._safe_open_algos()
        exit_side = "SELL" if position.side == "long" else "BUY"
        old_algo_ids = {
            int(value)
            for value in (
                active.get("stop_algo_id") if active else None,
                active.get("take_profit_algo_id") if active else None,
            )
            if value is not None
        }
        for order in open_algos:
            if not self.client._is_true(order.get("closePosition")):
                continue
            if str(order.get("symbol") or "") != self.symbol:
                continue
            if str(order.get("side") or "").upper() != exit_side:
                continue
            if str(order.get("type") or "").upper() not in {
                "STOP_MARKET",
                "TAKE_PROFIT_MARKET",
            }:
                continue
            algo_id = order.get("algoId")
            if algo_id is not None:
                old_algo_ids.add(int(algo_id))

        installed: list[int] = []
        stop_algo_id = None
        take_profit_algo_id = None
        try:
            if stop_loss is not None:
                normalized = await self._normalized_trigger(stop_loss)
                response = await self.client.place_close_position_algo_order(
                    self.symbol,
                    exit_side,
                    "STOP_MARKET",
                    normalized,
                    self._client_order_id(event_id, "sl"),
                    working_type="MARK_PRICE",
                )
                stop_algo_id = int(response["algoId"])
                installed.append(stop_algo_id)
            if take_profit is not None:
                normalized = await self._normalized_trigger(take_profit)
                response = await self.client.place_close_position_algo_order(
                    self.symbol,
                    exit_side,
                    "TAKE_PROFIT_MARKET",
                    normalized,
                    self._client_order_id(event_id, "tp"),
                    working_type="MARK_PRICE",
                )
                take_profit_algo_id = int(response["algoId"])
                installed.append(take_profit_algo_id)
        except Exception:
            for algo_id in installed:
                try:
                    await self.client.cancel_algo_order(algo_id)
                except Exception:
                    pass
            raise

        for algo_id in old_algo_ids - set(installed):
            try:
                await self.client.cancel_algo_order(algo_id)
            except Exception:
                pass
        if not stop_loss and not take_profit and not monitor:
            self.store.deactivate_active_brackets(self.symbol, "superseded")
            return None

        return self.store.create_manual_bracket(
            event_id,
            self.symbol,
            entry_order_id,
            entry_client_order_id,
            "BUY" if position.side == "long" else "SELL",
            self._optional_decimal(stop_loss),
            self._optional_decimal(take_profit),
            status="protected" if installed else "monitoring",
            stop_algo_id=stop_algo_id,
            take_profit_algo_id=take_profit_algo_id,
        )

    async def _submit_with_recovery(
        self,
        client_order_id: str,
        submit: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            response = await submit()
        except httpx.TimeoutException:
            response = await self.client.query_order_by_client_id(
                self.symbol, client_order_id
            )
        except BinanceAPIError as exc:
            if exc.status_code < 500:
                raise
            response = await self.client.query_order_by_client_id(
                self.symbol, client_order_id
            )
        if not isinstance(response, dict) or response.get("orderId") is None:
            raise RuntimeError("Binance did not return an order id")
        return response

    async def _safe_open_algos(self) -> list[dict[str, Any]]:
        try:
            return await self.client.get_open_algo_orders(self.symbol)
        except (BinanceAPIError, httpx.HTTPError):
            return []

    async def _wait_for_side(self, expected: str) -> Position:
        if self.client.dry_run:
            amount = Decimal("0")
            if expected == "long":
                amount = Decimal("1")
            elif expected == "short":
                amount = Decimal("-1")
            return Position(
                symbol=self.symbol,
                amount=amount,
                raw={"dryRun": True, "positionAmt": str(amount)},
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.position_poll_seconds
        while True:
            position = await self.client.get_position(self.symbol)
            if position.side == expected:
                return position
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"{self.symbol} did not become {expected} before timeout"
                )
            await asyncio.sleep(self.settings.position_poll_interval)

    @staticmethod
    def _optional_decimal(value: Decimal | None) -> str | None:
        return decimal_string(value) if value is not None else None
