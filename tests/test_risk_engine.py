from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.models import Position
from app.risk_engine import RiskEngine
from app.utils import round_step_size


def position(amount: float) -> Position:
    return Position(symbol="BTCUSDT", amount=amount)


def make_client(positions: list[Position]):
    client = AsyncMock()
    client.dry_run = False
    client.get_position.side_effect = positions
    client.get_symbol_filters.return_value = {
        "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
        "PRICE_FILTER": {"tickSize": "0.10", "minPrice": "0.10"},
    }
    client.cancel_all_open_orders.return_value = {"code": 200}
    client.cancel_opening_orders.return_value = {"canceledOrderIds": []}
    client.place_market_order.return_value = {"orderId": 1}
    client.place_limit_order.return_value = {"orderId": 2}
    return client


@pytest.mark.asyncio
async def test_buy_open_from_flat_places_limit_buy():
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "buy", Decimal("40000"), Decimal("80")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000"), False
    )
    client.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sell_open_from_flat_places_limit_sell():
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "sell", Decimal("40000"), Decimal("80")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000"), False
    )


@pytest.mark.asyncio
async def test_buy_open_while_short_market_closes_then_places_limit_buy():
    client = make_client([position(-0.004), position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "buy", Decimal("40000"), Decimal("80")
    )
    client.place_market_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.004"), True
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000"), False
    )


@pytest.mark.asyncio
async def test_sell_open_while_long_market_closes_then_places_limit_sell():
    client = make_client([position(0.004), position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "sell", Decimal("40000"), Decimal("80")
    )
    client.place_market_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.004"), True
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000"), False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_reduce_only_when_flat_does_not_place_order(side: str):
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).reduce_order(
        "BTCUSDT", side, Decimal("40000"), Decimal("80")
    )
    client.cancel_opening_orders.assert_awaited_once_with("BTCUSDT", side.upper())
    client.place_limit_order.assert_not_awaited()
    client.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_reduce_only_sell_cancels_short_openers_then_limits_long_close():
    client = make_client([position(0.002), position(0.002)])
    parent = AsyncMock()
    parent.attach_mock(client.cancel_opening_orders, "cancel_opening_orders")
    parent.attach_mock(client.get_position, "get_position")
    await RiskEngine(client, Settings()).reduce_order(
        "BTCUSDT", "sell", Decimal("40000"), Decimal("80")
    )
    assert parent.mock_calls[0].args == ("BTCUSDT", "SELL")
    assert parent.mock_calls[1].args == ("BTCUSDT",)
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000"), True
    )


@pytest.mark.asyncio
async def test_reduce_only_buy_limits_short_close_and_caps_to_position():
    client = make_client([position(-0.001), position(-0.001)])
    await RiskEngine(client, Settings()).reduce_order(
        "BTCUSDT", "buy", Decimal("40000"), Decimal("80")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.001"), Decimal("40000"), True
    )


def test_quantity_rounding_rounds_down():
    assert round_step_size(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")


@pytest.mark.asyncio
async def test_notional_amount_and_price_round_down_to_filters():
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "buy", Decimal("40000.19"), Decimal("116")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000.10"), False
    )
