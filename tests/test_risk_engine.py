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
    client.get_mark_price.return_value = Decimal("40000")
    client.get_symbol_filters.return_value = {
        "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"}
    }
    client.cancel_all_open_orders.return_value = {"code": 200}
    client.place_market_order.return_value = {"orderId": 1}
    return client


@pytest.mark.asyncio
async def test_open_long_from_flat_places_buy():
    client = make_client([position(0), position(0.002)])
    await RiskEngine(client, Settings()).open_long("BTCUSDT", 80)
    client.place_market_order.assert_awaited_once_with(
        "BTCUSDT", "BUY", Decimal("0.002"), False
    )


@pytest.mark.asyncio
async def test_open_short_from_flat_places_sell():
    client = make_client([position(0), position(-0.002)])
    await RiskEngine(client, Settings()).open_short("BTCUSDT", 80)
    client.place_market_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), False
    )


@pytest.mark.asyncio
async def test_open_long_while_short_closes_then_opens():
    client = make_client([position(-0.004), position(0), position(0.002)])
    await RiskEngine(client, Settings()).open_long("BTCUSDT", 80)
    assert client.place_market_order.await_args_list[0].args == (
        "BTCUSDT", "BUY", Decimal("0.004"), True
    )
    assert client.place_market_order.await_args_list[1].args == (
        "BTCUSDT", "BUY", Decimal("0.002"), False
    )


@pytest.mark.asyncio
async def test_open_short_while_long_closes_then_opens():
    client = make_client([position(0.004), position(0), position(-0.002)])
    await RiskEngine(client, Settings()).open_short("BTCUSDT", 80)
    assert client.place_market_order.await_args_list[0].args == (
        "BTCUSDT", "SELL", Decimal("0.004"), True
    )
    assert client.place_market_order.await_args_list[1].args == (
        "BTCUSDT", "SELL", Decimal("0.002"), False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["close_long", "close_short"])
async def test_close_when_flat_does_not_order(method: str):
    client = make_client([position(0), position(0)])
    await getattr(RiskEngine(client, Settings()), method)("BTCUSDT")
    client.place_market_order.assert_not_awaited()
    client.cancel_all_open_orders.assert_awaited_once_with("BTCUSDT")


def test_quantity_rounding_rounds_down():
    assert round_step_size(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
