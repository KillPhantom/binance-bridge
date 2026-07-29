from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .models import Position
from .utils import decimal_string


class BinanceAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Binance API error ({status_code}): {message}")
        self.status_code = status_code


class BinanceClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=settings.binance_base_url, timeout=10.0)
        self._owns_client = client is None
        self._time_offset_ms = 0
        self._exchange_info: dict[str, Any] | None = None

    @property
    def dry_run(self) -> bool:
        return self.settings.dry_run

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def get_account_time_or_server_time_if_needed(self) -> int:
        if self.dry_run:
            return int(time.time() * 1000)
        response = await self.client.get("/fapi/v1/time")
        self._raise_for_error(response)
        server_time = int(response.json()["serverTime"])
        self._time_offset_ms = server_time - int(time.time() * 1000)
        return server_time

    async def signed_request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            raise BinanceAPIError(0, "API credentials are not configured")
        for attempt in range(2):
            request_params = dict(params or {})
            request_params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            request_params["recvWindow"] = self.settings.recv_window
            normalized = {
                key: (str(value).lower() if isinstance(value, bool) else value)
                for key, value in request_params.items()
            }
            query = urlencode(normalized)
            signature = hmac.new(
                self.settings.binance_api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            url = f"{path}?{query}&signature={signature}"
            response = await self.client.request(
                method.upper(), url, headers={"X-MBX-APIKEY": self.settings.binance_api_key}
            )
            if response.status_code == 400 and attempt == 0:
                try:
                    if response.json().get("code") == -1021:
                        await self.get_account_time_or_server_time_if_needed()
                        continue
                except (ValueError, AttributeError):
                    pass
            self._raise_for_error(response)
            return response.json()
        raise BinanceAPIError(400, "timestamp remained out of sync after one retry")

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
            message = body.get("msg", str(body)) if isinstance(body, dict) else str(body)
        except ValueError:
            message = response.text[:500]
        raise BinanceAPIError(response.status_code, message)

    async def get_position(self, symbol: str) -> Position:
        if self.dry_run:
            return Position(symbol=symbol, amount=Decimal("0"), raw={"symbol": symbol, "positionAmt": "0", "dryRun": True})
        rows = await self.signed_request("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
        if not isinstance(rows, list):
            raise BinanceAPIError(200, "unexpected positionRisk response format")
        if not rows:
            mode = await self.signed_request("GET", "/fapi/v1/positionSide/dual")
            dual_side = mode.get("dualSidePosition") if isinstance(mode, dict) else None
            if dual_side is not False and str(dual_side).lower() != "false":
                raise BinanceAPIError(
                    200, "cannot treat empty position response as flat unless One-way Mode is confirmed"
                )
            return Position(
                symbol=symbol,
                amount=Decimal("0"),
                raw={
                    "symbol": symbol,
                    "positionAmt": "0",
                    "positionSide": "BOTH",
                    "inferredFlatFromEmptyResponse": True,
                },
            )
        if not all(isinstance(item, dict) for item in rows):
            raise BinanceAPIError(200, "unexpected positionRisk row format")
        row = next((item for item in rows if item.get("symbol") == symbol), None)
        if row is None:
            raise BinanceAPIError(200, f"position not returned for {symbol}")
        if row.get("positionSide", "BOTH") != "BOTH":
            raise BinanceAPIError(200, "account is not in One-way Mode (positionSide BOTH)")
        return Position(symbol=symbol, amount=Decimal(row["positionAmt"]), raw=row)

    async def get_mark_price(self, symbol: str) -> Decimal:
        if self.dry_run:
            return Decimal("1")
        response = await self.client.get("/fapi/v1/premiumIndex", params={"symbol": symbol})
        self._raise_for_error(response)
        return Decimal(response.json()["markPrice"])

    async def get_asset_balance(self, asset: str) -> dict[str, Any]:
        normalized_asset = asset.upper()
        if self.dry_run:
            return {
                "asset": normalized_asset,
                "balance": "0",
                "availableBalance": "0",
            }
        rows = await self.signed_request("GET", "/fapi/v3/balance")
        if not isinstance(rows, list) or not all(
            isinstance(item, dict) for item in rows
        ):
            raise BinanceAPIError(200, "unexpected balance response format")
        row = next(
            (
                item
                for item in rows
                if str(item.get("asset") or "").upper() == normalized_asset
            ),
            None,
        )
        if row is None:
            return {
                "asset": normalized_asset,
                "balance": "0",
                "availableBalance": "0",
            }
        return row

    async def get_exchange_info(self) -> dict[str, Any]:
        if self.dry_run:
            return {"symbols": [{"symbol": symbol, "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
            ]} for symbol in self.settings.allowed_symbols]}
        if self._exchange_info is None:
            response = await self.client.get("/fapi/v1/exchangeInfo")
            self._raise_for_error(response)
            self._exchange_info = response.json()
        return self._exchange_info

    async def get_symbol_filters(self, symbol: str) -> dict[str, dict[str, Any]]:
        info = await self.get_exchange_info()
        item = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if item is None:
            raise ValueError(f"symbol {symbol} not found in exchange info")
        return {f["filterType"]: f for f in item["filters"]}

    async def cancel_all_open_orders(self, symbol: str) -> dict[str, Any]:
        if self.dry_run:
            return {"dryRun": True, "operation": "cancel_all", "symbol": symbol}
        return await self.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        return await self.signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol})

    async def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        if self.dry_run:
            return {
                "dryRun": True,
                "operation": "cancel_order",
                "symbol": symbol,
                "orderId": order_id,
            }
        return await self.signed_request(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
        )

    async def query_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        if self.dry_run:
            return {
                "dryRun": True,
                "symbol": symbol,
                "orderId": order_id,
                "status": "FILLED",
                "executedQty": "0",
            }
        return await self.signed_request(
            "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
        )

    async def query_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> dict[str, Any]:
        if self.dry_run:
            return {
                "dryRun": True,
                "symbol": symbol,
                "clientOrderId": client_order_id,
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "0",
            }
        return await self.signed_request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )

    async def cancel_all_algo_open_orders(self, symbol: str) -> dict[str, Any]:
        if self.dry_run:
            return {"dryRun": True, "operation": "cancel_all_algo", "symbol": symbol}
        return await self.signed_request(
            "DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}
        )

    async def get_open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        response = await self.signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}
        )
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            rows = response.get("orders") or response.get("rows") or []
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
        raise BinanceAPIError(200, "unexpected openAlgoOrders response format")

    async def cancel_algo_order(self, algo_id: int) -> dict[str, Any]:
        if self.dry_run:
            return {"dryRun": True, "operation": "cancel_algo", "algoId": algo_id}
        return await self.signed_request(
            "DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}
        )

    async def place_close_position_algo_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        trigger_price: Decimal,
        client_algo_id: str,
        working_type: str | None = None,
    ) -> dict[str, Any]:
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("unsupported close-position algo order type")
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "positionSide": "BOTH",
            "triggerPrice": decimal_string(trigger_price),
            "workingType": working_type or self.settings.algo_working_type,
            "closePosition": True,
            "priceProtect": self.settings.algo_price_protect,
            "clientAlgoId": client_algo_id,
            "newOrderRespType": "RESULT",
        }
        if self.dry_run:
            return {"dryRun": True, "algoId": 1, **params}
        response = await self.signed_request("POST", "/fapi/v1/algoOrder", params)
        if not isinstance(response, dict) or response.get("algoId") is None:
            raise BinanceAPIError(200, "Binance did not return algoId")
        if not self._is_true(response.get("closePosition")):
            await self.cancel_algo_order(int(response["algoId"]))
            raise BinanceAPIError(200, "Binance did not confirm closePosition")
        return response

    @staticmethod
    def _is_true(value: Any) -> bool:
        return value is True or (isinstance(value, str) and value.lower() == "true")

    async def cancel_opening_orders(self, symbol: str, side: str) -> dict[str, Any]:
        """Cancel non-reduce-only opening orders for one side in One-way Mode."""
        normalized_side = side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("opening order side must be BUY or SELL")
        if self.dry_run:
            return {
                "dryRun": True,
                "operation": "cancel_opening_orders",
                "symbol": symbol,
                "side": normalized_side,
                "canceledOrderIds": [],
            }

        orders = await self.get_open_orders(symbol)
        candidates = [
            order
            for order in orders
            if order.get("side", "").upper() == normalized_side
            and order.get("positionSide", "BOTH") == "BOTH"
            and not self._is_true(order.get("reduceOnly", False))
            and not self._is_true(order.get("closePosition", False))
        ]
        canceled = [
            await self.cancel_order(symbol, int(order["orderId"])) for order in candidates
        ]
        return {
            "operation": "cancel_opening_orders",
            "symbol": symbol,
            "side": normalized_side,
            "canceledOrderIds": [int(order["orderId"]) for order in candidates],
            "responses": canceled,
        }

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        reduce_only: bool,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": decimal_string(quantity),
            "reduceOnly": reduce_only,
            "newOrderRespType": "RESULT",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        if self.dry_run:
            return {
                "dryRun": True,
                "orderId": 1,
                "status": "FILLED",
                "executedQty": decimal_string(quantity),
                "avgPrice": "1",
                **params,
            }
        return await self.signed_request("POST", "/fapi/v1/order", params)

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": "BOTH",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": decimal_string(quantity),
            "price": decimal_string(price),
            "reduceOnly": False,
            "newOrderRespType": "RESULT",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        if self.dry_run:
            return {
                "dryRun": True,
                "orderId": 1,
                "status": "NEW",
                "executedQty": "0",
                **params,
            }
        return await self.signed_request("POST", "/fapi/v1/order", params)

    async def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        if not 1 <= leverage <= 125:
            raise ValueError("leverage must be between 1 and 125")
        params = {"symbol": symbol, "leverage": leverage}
        if self.dry_run:
            return {"dryRun": True, "maxNotionalValue": "0", **params}
        return await self.signed_request("POST", "/fapi/v1/leverage", params)

    async def place_reduce_only_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> dict[str, Any]:
        """Submit a limit order with reduceOnly hardcoded true."""
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": "BOTH",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": decimal_string(quantity),
            "price": decimal_string(price),
            "reduceOnly": True,
            "newOrderRespType": "RESULT",
        }
        if self.dry_run:
            return {"dryRun": True, **params}
        response = await self.signed_request("POST", "/fapi/v1/order", params)
        returned_flag = response.get("reduceOnly") if isinstance(response, dict) else None
        if not self._is_true(returned_flag):
            order_id = response.get("orderId") if isinstance(response, dict) else None
            if order_id is not None:
                await self.cancel_order(symbol, int(order_id))
            raise BinanceAPIError(200, "Binance did not confirm reduceOnly on reduce order")
        return response
