import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.binance_client import BinanceAPIError
from app.bracket_worker import BracketWorker
from app.config import Settings
from app.db import EventStore
from app.models import Position


def create_bracket(
    store: EventStore,
    event_id: str = "entry-1",
    symbol: str = "BTCUSDT",
    entry_order_id: int = 101,
    stop_loss_price: str = "39000",
    take_profit_price: str = "41000",
) -> dict:
    store.create_bracket(
        event_id=event_id,
        symbol=symbol,
        entry_order_id=entry_order_id,
        entry_side="BUY",
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
    )
    return next(
        bracket
        for bracket in store.list_active_brackets()
        if bracket["event_id"] == event_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_status", ["PARTIALLY_FILLED", "EXPIRED"])
async def test_executed_market_entry_is_protected_without_canceling_remainder(
    tmp_path, entry_status
):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(
        store, stop_loss_price="39050", take_profit_price="41050"
    )
    client = AsyncMock()
    client.query_order.return_value = {
        "orderId": 101,
        "status": entry_status,
        "executedQty": "0.002",
    }
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

    client.cancel_order.assert_not_awaited()
    assert client.place_close_position_algo_order.await_args_list[0].args[2:4] == (
        "STOP_MARKET",
        Decimal("39050"),
    )
    assert client.place_close_position_algo_order.await_args_list[1].args[2:4] == (
        "TAKE_PROFIT_MARKET",
        Decimal("41050"),
    )
    updated = store.get_bracket(bracket["id"])
    assert updated["status"] == "protected"
    assert updated["stop_algo_id"] == 201
    assert updated["take_profit_algo_id"] == 202


@pytest.mark.asyncio
async def test_immediate_trigger_rejection_closes_position_with_market_order(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    client = AsyncMock()
    client.query_order.return_value = {
        "orderId": 101,
        "status": "FILLED",
        "executedQty": "0.002",
    }
    client.get_position.return_value = Position(symbol="BTCUSDT", amount="0.002")
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    client.get_symbol_filters.return_value = {
        "PRICE_FILTER": {"tickSize": "0.10"}
    }
    client.place_close_position_algo_order.side_effect = BinanceAPIError(
        400, "Order would immediately trigger."
    )
    client.place_market_order.return_value = {"orderId": 301, "status": "FILLED"}
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.place_market_order.assert_awaited_once_with(
        "BTCUSDT", "SELL", Decimal("0.002"), True
    )
    assert client.cancel_all_algo_open_orders.await_count == 2
    updated = store.get_bracket(bracket["id"])
    assert updated["status"] == "closed"
    assert updated["stop_algo_id"] is None
    assert updated["take_profit_algo_id"] is None
    assert updated["error"] is None


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
async def test_manual_protection_exit_cancels_partially_filled_gtc_remainder(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = store.create_manual_bracket(
        event_id="manual-limit-1",
        symbol="ETHUSDT",
        entry_order_id=501,
        entry_client_order_id="mt-limit-1",
        entry_side="BUY",
        stop_loss_price="1900",
        take_profit_price="2200",
        status="protected",
        stop_algo_id=601,
        take_profit_algo_id=602,
    )
    client = AsyncMock()
    client.get_position.return_value = Position(symbol="ETHUSDT", amount="0")
    client.query_order.return_value = {
        "orderId": 501,
        "status": "PARTIALLY_FILLED",
        "executedQty": "0.1",
    }
    client.cancel_order.return_value = {"status": "CANCELED"}
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    worker = BracketWorker(client, store, Settings())

    await worker._process(bracket)

    client.cancel_order.assert_awaited_once_with("ETHUSDT", 501)
    client.cancel_all_algo_open_orders.assert_awaited_once_with("ETHUSDT")
    assert store.get_bracket(bracket["id"])["status"] == "closed"


@pytest.mark.asyncio
async def test_run_prioritizes_entry_and_throttles_protected_reconciliation(tmp_path):
    store = EventStore(tmp_path / "events.db")
    protected = create_bracket(
        store, event_id="entry-btc", symbol="BTCUSDT", entry_order_id=101
    )
    store.update_bracket(
        protected["id"],
        status="protected",
        stop_algo_id=201,
        take_profit_algo_id=202,
    )
    awaiting = create_bracket(
        store, event_id="entry-eth", symbol="ETHUSDT", entry_order_id=102
    )
    client = AsyncMock()
    client.query_order.return_value = {
        "orderId": 102,
        "status": "FILLED",
        "executedQty": "0.002",
    }
    client.get_position.return_value = Position(symbol="ETHUSDT", amount="0.002")
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    client.get_symbol_filters.return_value = {
        "PRICE_FILTER": {"tickSize": "0.10"}
    }
    worker = BracketWorker(
        client,
        store,
        Settings(bracket_poll_interval=60, protected_reconcile_interval=60),
    )
    worker._last_protected_reconcile[protected["id"]] = (
        asyncio.get_running_loop().time()
    )
    algo_ids = iter([301, 302])

    async def place_algo(*args, **kwargs):
        algo_id = next(algo_ids)
        if algo_id == 302:
            worker.stop()
        return {"algoId": algo_id, "closePosition": True}

    client.place_close_position_algo_order.side_effect = place_algo

    await worker.run()

    client.query_order.assert_awaited_once_with("ETHUSDT", awaiting["entry_order_id"])
    client.get_position.assert_awaited_once_with("ETHUSDT")
    assert store.get_bracket(awaiting["id"])["status"] == "protected"


@pytest.mark.asyncio
async def test_unfilled_entry_is_not_canceled_before_one_minute_timeout(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    bracket["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=30)
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
async def test_unfilled_entry_is_canceled_after_one_minute_timeout(tmp_path):
    store = EventStore(tmp_path / "events.db")
    bracket = create_bracket(store)
    bracket["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=61)
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
        datetime.now(timezone.utc) - timedelta(seconds=61)
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
