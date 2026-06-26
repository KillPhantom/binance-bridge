from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.bracket_worker import BracketWorker
from app.config import Settings
from app.db import EventStore
from app.models import Position


def create_bracket(store: EventStore) -> dict:
    store.create_bracket(
        event_id="entry-1",
        symbol="BTCUSDT",
        entry_order_id=101,
        entry_side="BUY",
        stop_loss_price="39000",
        take_profit_price="41000",
    )
    return store.list_active_brackets()[0]


@pytest.mark.asyncio
async def test_partial_fill_cancels_remainder_and_installs_two_close_all_algos(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    client = AsyncMock()
    client.query_order.return_value = {
        "orderId": 101,
        "status": "PARTIALLY_FILLED",
        "executedQty": "0.002",
    }
    client.cancel_order.return_value = {"status": "CANCELED"}
    client.get_position.return_value = Position(symbol="BTCUSDT", amount="0.002")
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    client.get_symbol_filters.return_value = {
        "PRICE_FILTER": {"tickSize": "0.10"}
    }
    client.place_close_position_algo_order.side_effect = [
        {"algoId": 201, "closePosition": True},
        {"algoId": 202, "closePosition": True},
    ]
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.cancel_order.assert_awaited_once_with("BTCUSDT", 101)
    assert client.place_close_position_algo_order.await_args_list[0].args[2] == "STOP_MARKET"
    assert client.place_close_position_algo_order.await_args_list[1].args[2] == "TAKE_PROFIT_MARKET"
    updated = store.get_bracket(bracket["id"])
    assert updated["status"] == "protected"
    assert updated["stop_algo_id"] == 201
    assert updated["take_profit_algo_id"] == 202


@pytest.mark.asyncio
async def test_flat_position_cancels_remaining_sibling_algo(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    store.update_bracket(
        bracket["id"],
        status="protected",
        stop_algo_id=201,
        take_profit_algo_id=202,
    )
    bracket = store.get_bracket(bracket["id"])
    client = AsyncMock()
    client.get_position.return_value = Position(symbol="BTCUSDT", amount="0")
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.cancel_all_algo_open_orders.assert_awaited_once_with("BTCUSDT")
    assert store.get_bracket(bracket["id"])["status"] == "closed"


@pytest.mark.asyncio
async def test_unfilled_entry_is_not_canceled_before_thirty_minute_timeout(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    bracket["created_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=15)
    ).isoformat()
    client = AsyncMock()
    client.query_order.return_value = {
        "orderId": 101,
        "status": "NEW",
        "executedQty": "0",
    }
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.cancel_order.assert_not_awaited()
    assert store.get_bracket(bracket["id"])["status"] == "awaiting_entry"


@pytest.mark.asyncio
async def test_unfilled_entry_is_canceled_after_thirty_minute_timeout(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    bracket["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1801)
    ).isoformat()
    client = AsyncMock()
    client.query_order.side_effect = [
        {"orderId": 101, "status": "NEW", "executedQty": "0"},
        {"orderId": 101, "status": "CANCELED", "executedQty": "0"},
    ]
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.cancel_order.assert_awaited_once_with("BTCUSDT", 101)
    assert store.get_bracket(bracket["id"])["status"] == "entry_timed_out"


@pytest.mark.asyncio
async def test_timeout_race_fill_is_protected(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    bracket["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1801)
    ).isoformat()
    client = AsyncMock()
    client.query_order.side_effect = [
        {"orderId": 101, "status": "NEW", "executedQty": "0"},
        {"orderId": 101, "status": "FILLED", "executedQty": "0.002"},
    ]
    client.get_position.return_value = Position(symbol="BTCUSDT", amount="0.002")
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    client.get_symbol_filters.return_value = {
        "PRICE_FILTER": {"tickSize": "0.10"}
    }
    client.place_close_position_algo_order.side_effect = [
        {"algoId": 201},
        {"algoId": 202},
    ]
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.cancel_order.assert_awaited_once_with("BTCUSDT", 101)
    assert store.get_bracket(bracket["id"])["status"] == "protected"
