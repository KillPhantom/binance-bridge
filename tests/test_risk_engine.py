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
async def test_open_long_from_flat_places_buy():
    client = make_client([position(0), position(0.002)])
    await RiskEngine(client, Settings()).open_long(
        "BTCUSDT", Decimal("40000"), Decimal("0.002")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000"), False
    )
    client.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_short_from_flat_places_sell():
    client = make_client([position(0), position(-0.002)])
    await RiskEngine(client, Settings()).open_short(
        "BTCUSDT", Decimal("40000"), Decimal("0.002")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000"), False
    )
    client.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_long_while_short_closes_then_opens():
    client = make_client([position(-0.004), position(0), position(0.002)])
    await RiskEngine(client, Settings()).open_long(
        "BTCUSDT", Decimal("40000"), Decimal("0.002")
    )
    assert client.place_market_order.await_args_list[0].args == (
        "BTCUSDT", "BUY", Decimal("0.004"), True
    )
    assert client.place_limit_order.await_args_list[0].args == (
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000"), False
    )


@pytest.mark.asyncio
async def test_open_short_while_long_closes_then_opens():
    client = make_client([position(0.004), position(0), position(-0.002)])
    await RiskEngine(client, Settings()).open_short(
        "BTCUSDT", Decimal("40000"), Decimal("0.002")
    )
    assert client.place_market_order.await_args_list[0].args == (
        "BTCUSDT", "SELL", Decimal("0.004"), True
    )
    assert client.place_limit_order.await_args_list[0].args == (
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000"), False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "opening_side"),
    [("close_long", "BUY"), ("close_short", "SELL")],
)
async def test_close_when_flat_does_not_order(method: str, opening_side: str):
    client = make_client([position(0), position(0)])
    await getattr(RiskEngine(client, Settings()), method)("BTCUSDT")
    client.place_market_order.assert_not_awaited()
    client.cancel_opening_orders.assert_awaited_once_with("BTCUSDT", opening_side)
    client.cancel_all_open_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_short_cancels_opening_short_orders_before_position_read():
    client = make_client([position(-0.002), position(0)])
    parent = AsyncMock()
    parent.attach_mock(client.cancel_opening_orders, "cancel_opening_orders")
    parent.attach_mock(client.get_position, "get_position")
    await RiskEngine(client, Settings()).close_short("BTCUSDT")
    assert parent.mock_calls[0].args == ("BTCUSDT", "SELL")
    assert parent.mock_calls[1].args == ("BTCUSDT",)


@pytest.mark.asyncio
async def test_close_long_cancels_opening_long_orders_before_position_read():
    client = make_client([position(0.002), position(0)])
    parent = AsyncMock()
    parent.attach_mock(client.cancel_opening_orders, "cancel_opening_orders")
    parent.attach_mock(client.get_position, "get_position")
    await RiskEngine(client, Settings()).close_long("BTCUSDT")
    assert parent.mock_calls[0].args == ("BTCUSDT", "BUY")
    assert parent.mock_calls[1].args == ("BTCUSDT",)


def test_quantity_rounding_rounds_down():
    assert round_step_size(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")


@pytest.mark.asyncio
async def test_open_order_rounds_passed_price_and_amount_down_to_filters():
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).open_long(
        "BTCUSDT", Decimal("40000.19"), Decimal("0.0029")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000.10"), False
    )
