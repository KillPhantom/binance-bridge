from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import EventStore
from app.main import create_app
from app.models import Position


def make_app(tmp_path, client=None):
    settings = Settings(
        webhook_secret="test-secret",
        allowed_symbols=frozenset({"BTCUSDT"}),
        sqlite_path=tmp_path / "events.db",
    )
    client = client or AsyncMock()
    client.dry_run = False
    client.close.return_value = None
    client.get_position.side_effect = [
        Position(symbol="BTCUSDT", amount=0),
        Position(symbol="BTCUSDT", amount=0.002),
    ]
    client.get_mark_price.return_value = Decimal("40000")
    client.get_symbol_filters.return_value = {
        "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"}
    }
    client.cancel_all_open_orders.return_value = {"code": 200}
    client.place_market_order.return_value = {"orderId": 42}
    store = EventStore(settings.sqlite_path)
    return create_app(settings, store, client), store, client


def payload(**changes):
    result = {
        "token": "test-secret",
        "event_id": "event-1",
        "symbol": "BTCUSDT",
        "action": "open_long",
        "notional": 80,
        "source": "tradingview",
        "strategy": "test",
    }
    result.update(changes)
    return result


def test_duplicate_success_does_not_place_duplicate_order(tmp_path):
    app, _, binance = make_app(tmp_path)
    with TestClient(app) as http:
        first = http.post("/webhook/tradingview", json=payload())
        second = http.post("/webhook/tradingview", json=payload())
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert binance.place_market_order.await_count == 1


def test_wrong_token_rejected(tmp_path):
    app, _, binance = make_app(tmp_path)
    with TestClient(app) as http:
        response = http.post("/webhook/tradingview", json=payload(token="wrong"))
    assert response.status_code == 401
    binance.place_market_order.assert_not_awaited()


def test_unsupported_symbol_rejected(tmp_path):
    app, _, binance = make_app(tmp_path)
    with TestClient(app) as http:
        response = http.post("/webhook/tradingview", json=payload(symbol="DOGEUSDT"))
    assert response.status_code == 400
    binance.place_market_order.assert_not_awaited()


def test_binance_failure_marks_event_failed(tmp_path):
    binance = AsyncMock()
    binance.dry_run = False
    binance.close.return_value = None
    binance.cancel_all_open_orders.side_effect = RuntimeError("exchange unavailable")
    app, store, _ = make_app(tmp_path, binance)
    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.post("/webhook/tradingview", json=payload())
    assert response.status_code == 500
    event = store.get("event-1")
    assert event["status"] == "failed"
    assert "exchange unavailable" in event["error"]
    assert "test-secret" not in event["payload_json"]


def test_failed_event_requires_explicit_retry(tmp_path):
    binance = AsyncMock()
    binance.dry_run = False
    binance.close.return_value = None
    binance.cancel_all_open_orders.side_effect = RuntimeError("exchange unavailable")
    app, _, _ = make_app(tmp_path, binance)
    with TestClient(app, raise_server_exceptions=False) as http:
        assert http.post("/webhook/tradingview", json=payload()).status_code == 500
        assert http.post("/webhook/tradingview", json=payload()).status_code == 409
