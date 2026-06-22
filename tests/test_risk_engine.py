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
    client.place_reduce_only_limit_order.return_value = {"orderId": 3, "reduceOnly": True}
    return client


@pytest.mark.asyncio
async def test_buy_open_from_flat_places_limit_buy():
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "buy", Decimal("40000"), Decimal("80")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000")
    )
    client.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sell_open_from_flat_places_limit_sell():
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).open_order(
        "BTCUSDT", "sell", Decimal("40000"), Decimal("80")
    )
    client.place_limit_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000")
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
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000")
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
        "BTCUSDT", "SELL", Decimal("0.002"), Decimal("40000")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "opening_side_to_cancel"),
    [("sell", "BUY"), ("buy", "SELL")],
)
async def test_reduce_only_when_flat_cancels_old_opener_without_reduce_order(
    side: str, opening_side_to_cancel: str
):
    client = make_client([position(0), position(0)])
    await RiskEngine(client, Settings()).reduce_order(
        "BTCUSDT", side, Decimal("40000")
    )
    client.cancel_opening_orders.assert_awaited_once_with(
        "BTCUSDT", opening_side_to_cancel
    )
    client.place_limit_order.assert_not_awaited()
    client.place_reduce_only_limit_order.assert_not_awaited()
    client.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_reduce_only_sell_cancels_long_openers_then_limits_long_close():
    client = make_client([position(0.005), position(0.005)])
    parent = AsyncMock()
    parent.attach_mock(client.cancel_opening_orders, "cancel_opening_orders")
    parent.attach_mock(client.get_position, "get_position")
    await RiskEngine(client, Settings()).reduce_order(
        "BTCUSDT", "sell", Decimal("40000")
    )
    assert parent.mock_calls[0].args == ("BTCUSDT", "BUY")
    assert parent.mock_calls[1].args == ("BTCUSDT",)
    client.place_reduce_only_limit_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.005"), Decimal("40000")
    )


@pytest.mark.asyncio
async def test_reduce_only_buy_uses_entire_live_short_position():
    client = make_client([position(-0.003), position(-0.003)])
    await RiskEngine(client, Settings()).reduce_order(
        "BTCUSDT", "buy", Decimal("40000")
    )
    client.cancel_opening_orders.assert_awaited_once_with("BTCUSDT", "SELL")
    client.place_reduce_only_limit_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.003"), Decimal("40000")
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
        "BTCUSDT", "BUY", Decimal("0.002"), Decimal("40000.10")
    )
