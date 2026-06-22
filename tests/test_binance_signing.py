import hashlib
import hmac
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest

from app.binance_client import BinanceAPIError, BinanceClient
from app.config import Settings
from app.models import Position


@pytest.mark.asyncio
async def test_signed_request_uses_hmac_sha256(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("app.binance_client.time.time", lambda: 1700000000.0)
    settings = Settings(
        binance_api_key="key",
        binance_api_secret="secret",
        dry_run=False,
    )
    http = httpx.AsyncClient(
        base_url=settings.binance_base_url, transport=httpx.MockTransport(handler)
    )
    client = BinanceClient(settings, http)
    await client.signed_request("GET", "/fapi/v3/positionRisk", {"symbol": "BTCUSDT"})

    query = urlencode({"symbol": "BTCUSDT", "timestamp": 1700000000000, "recvWindow": 5000})
    expected = hmac.new(b"secret", query.encode(), hashlib.sha256).hexdigest()
    assert seen["request"].headers["X-MBX-APIKEY"] == "key"
    assert seen["request"].url.params["signature"] == expected
    await http.aclose()


@pytest.mark.asyncio
async def test_cancel_opening_orders_preserves_close_and_opposite_orders():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.get_open_orders = AsyncMock(return_value=[
        {"orderId": 1, "side": "SELL", "positionSide": "BOTH", "reduceOnly": False},
        {"orderId": 2, "side": "SELL", "positionSide": "BOTH", "reduceOnly": True},
        {"orderId": 3, "side": "SELL", "positionSide": "BOTH", "closePosition": True},
        {"orderId": 4, "side": "BUY", "positionSide": "BOTH", "reduceOnly": False},
    ])
    client.cancel_order = AsyncMock(return_value={"status": "CANCELED"})

    result = await client.cancel_opening_orders("BTCUSDT", "SELL")

    assert result["canceledOrderIds"] == [1]
    client.cancel_order.assert_awaited_once_with("BTCUSDT", 1)
    await client.close()


@pytest.mark.asyncio
async def test_empty_position_response_is_flat_only_after_one_way_confirmation():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.signed_request = AsyncMock(
        side_effect=[[], {"dualSidePosition": False}]
    )

    result = await client.get_position("BTCUSDT")

    assert result == Position(
        symbol="BTCUSDT",
        amount=0,
        raw={
            "symbol": "BTCUSDT",
            "positionAmt": "0",
            "positionSide": "BOTH",
            "inferredFlatFromEmptyResponse": True,
        },
    )
    assert client.signed_request.await_args_list[1].args == (
        "GET", "/fapi/v1/positionSide/dual"
    )
    await client.close()


@pytest.mark.asyncio
async def test_empty_position_response_is_rejected_in_hedge_mode():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.signed_request = AsyncMock(
        side_effect=[[], {"dualSidePosition": True}]
    )

    with pytest.raises(BinanceAPIError, match="One-way Mode"):
        await client.get_position("BTCUSDT")
    await client.close()


@pytest.mark.asyncio
async def test_limit_order_sends_passed_price_and_amount_as_gtc():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.signed_request = AsyncMock(return_value={"orderId": 9})

    await client.place_limit_order(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000.10")
    )

    assert client.signed_request.await_args.args == (
        "POST",
        "/fapi/v1/order",
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "BOTH",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "0.002",
            "price": "40000.10",
            "reduceOnly": False,
            "newOrderRespType": "RESULT",
        },
    )
    await client.close()


@pytest.mark.asyncio
async def test_reduce_limit_order_hardcodes_reduce_only_true():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.signed_request = AsyncMock(
        return_value={"orderId": 10, "reduceOnly": True}
    )

    await client.place_reduce_only_limit_order(
        "BTCUSDT", "SELL", Decimal("0.001"), Decimal("64043.90")
    )

    params = client.signed_request.await_args.args[2]
    assert params["side"] == "SELL"
    assert params["positionSide"] == "BOTH"
    assert params["reduceOnly"] is True
    await client.close()


@pytest.mark.asyncio
async def test_reduce_limit_cancels_if_binance_does_not_confirm_reduce_only():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.signed_request = AsyncMock(
        return_value={"orderId": 11, "reduceOnly": False}
    )
    client.cancel_order = AsyncMock(return_value={"status": "CANCELED"})

    with pytest.raises(BinanceAPIError, match="did not confirm reduceOnly"):
        await client.place_reduce_only_limit_order(
            "BTCUSDT", "SELL", Decimal("0.001"), Decimal("64043.90")
        )

    client.cancel_order.assert_awaited_once_with("BTCUSDT", 11)
    await client.close()


@pytest.mark.asyncio
async def test_close_position_algo_uses_current_futures_algo_endpoint():
    settings = Settings(
        binance_api_key="key", binance_api_secret="secret", dry_run=False
    )
    client = BinanceClient(settings)
    client.signed_request = AsyncMock(
        return_value={"algoId": 301, "closePosition": True}
    )

    await client.place_close_position_algo_order(
        "BTCUSDT",
        "SELL",
        "STOP_MARKET",
        Decimal("39000"),
        "tvb1sl",
    )

    method, path, params = client.signed_request.await_args.args
    assert (method, path) == ("POST", "/fapi/v1/algoOrder")
    assert params["algoType"] == "CONDITIONAL"
    assert params["positionSide"] == "BOTH"
    assert params["closePosition"] is True
    assert "quantity" not in params
    assert "reduceOnly" not in params
    await client.close()
