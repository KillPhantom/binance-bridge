from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import BinanceAccount, Settings
from app.main import create_app
from app.models import Position


TOKEN = "w2-manual-token-1234567890-abcdef"


def make_binance():
    client = AsyncMock()
    client.dry_run = False
    client.close.return_value = None
    client.get_mark_price.return_value = Decimal("2000")
    client.get_asset_balance.return_value = {
        "asset": "USDT",
        "balance": "123.45000000",
        "availableBalance": "98.76000000",
    }
    client.get_position.return_value = Position(
        symbol="ETHUSDT",
        amount=Decimal("0"),
        raw={
            "symbol": "ETHUSDT",
            "positionAmt": "0",
            "positionSide": "BOTH",
            "entryPrice": "0",
            "markPrice": "2000",
            "unRealizedProfit": "0",
            "liquidationPrice": "0",
            "leverage": "20",
            "marginType": "cross",
            "notional": "0",
        },
    )
    client.get_open_orders.return_value = []
    client.get_open_algo_orders.return_value = []
    client.get_symbol_filters.return_value = {
        "MARKET_LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
        "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001"},
        "PRICE_FILTER": {"tickSize": "0.10", "minPrice": "0.10"},
        "MIN_NOTIONAL": {"notional": "5"},
    }
    client.change_leverage.return_value = {
        "symbol": "ETHUSDT",
        "leverage": 20,
    }
    client.cancel_all_open_orders.return_value = {"code": 200}
    client.cancel_all_algo_open_orders.return_value = {"code": 200}
    client.cancel_algo_order.return_value = {"code": 200}
    client.cancel_order.return_value = {"status": "CANCELED"}
    client.place_market_order.return_value = {
        "orderId": 1001,
        "status": "FILLED",
        "executedQty": "0.05",
        "avgPrice": "2000",
    }
    client.place_limit_order.return_value = {
        "orderId": 1002,
        "status": "NEW",
        "executedQty": "0",
    }
    return client


def make_manual_app(tmp_path, client=None):
    client = client or make_binance()
    settings = Settings(
        webhook_secret="test-secret",
        allowed_symbols=frozenset({"ETHUSDT"}),
        sqlite_path=tmp_path / "events.db",
        dry_run=False,
        bracket_poll_interval=60,
        binance_accounts=(
            BinanceAccount(
                name="w2",
                api_key="key",
                api_secret="secret",
                manual_token=TOKEN,
                allowed_symbols=frozenset({"ETHUSDT"}),
            ),
        ),
    )
    return create_app(settings, clients={"w2": client}), client


def auth_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def order_payload(**changes):
    payload = {
        "clientRequestId": "request-open-0001",
        "side": "BUY",
        "orderType": "MARKET",
        "amount": "100",
        "amountUnit": "USDT",
        "leverage": 20,
    }
    payload.update(changes)
    return payload


def test_trade_page_and_static_token_auth(tmp_path):
    app, _ = make_manual_app(tmp_path)
    with TestClient(app) as http:
        page = http.get("/trade")
        missing = http.post("/api/manual/auth")
        wrong = http.post(
            "/api/manual/auth",
            headers=auth_headers("x" * 32),
        )
        valid = http.post("/api/manual/auth", headers=auth_headers())

    assert page.status_code == 200
    assert "ETHUSDT 手动交易" in page.text
    assert "账户余额" in page.text
    assert 'id="walletBalance"' in page.text
    assert 'id="availableBalance"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert valid.json() == {"ok": True, "account": "w2", "symbol": "ETHUSDT"}


def test_token_is_bound_to_one_account(tmp_path):
    primary = make_binance()
    w2 = make_binance()
    settings = Settings(
        webhook_secret="test-secret",
        sqlite_path=tmp_path / "events.db",
        dry_run=False,
        binance_accounts=(
            BinanceAccount(
                name="primary",
                api_key="key-1",
                api_secret="secret-1",
                manual_token="primary-token-1234567890-abcdefgh",
                allowed_symbols=frozenset({"ETHUSDT"}),
            ),
            BinanceAccount(
                name="w2",
                api_key="key-2",
                api_secret="secret-2",
                manual_token=TOKEN,
                allowed_symbols=frozenset({"ETHUSDT"}),
            ),
        ),
    )
    app = create_app(settings, clients={"primary": primary, "w2": w2})

    with TestClient(app) as http:
        response = http.get("/api/manual/state", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["account"] == "w2"
    assert response.json()["balance"] == {
        "asset": "USDT",
        "walletBalance": "123.45000000",
        "availableBalance": "98.76000000",
    }
    w2.get_position.assert_awaited_once_with("ETHUSDT")
    w2.get_asset_balance.assert_awaited_once_with("USDT")
    primary.get_position.assert_not_awaited()
    primary.get_asset_balance.assert_not_awaited()


def test_duplicate_manual_order_is_idempotent_and_usdt_is_converted(tmp_path):
    client = make_binance()
    flat = Position(symbol="ETHUSDT", amount=Decimal("0"))
    long = Position(symbol="ETHUSDT", amount=Decimal("0.05"))
    client.get_position.side_effect = [flat, long]
    app, client = make_manual_app(tmp_path, client)
    payload = order_payload()

    with TestClient(app) as http:
        first = http.post(
            "/api/manual/orders", headers=auth_headers(), json=payload
        )
        second = http.post(
            "/api/manual/orders", headers=auth_headers(), json=payload
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    client.change_leverage.assert_awaited_once_with("ETHUSDT", 20)
    placed = client.place_market_order.await_args
    assert placed.args[:4] == ("ETHUSDT", "BUY", Decimal("0.05"), False)
    assert placed.args[4].startswith("mt")
    assert client.place_market_order.await_count == 1


def test_limit_order_is_gtc_and_bracket_has_no_timeout(tmp_path):
    client = make_binance()
    app, client = make_manual_app(tmp_path, client)

    with TestClient(app) as http:
        response = http.post(
            "/api/manual/orders",
            headers=auth_headers(),
            json=order_payload(
                clientRequestId="request-limit-0001",
                orderType="LIMIT",
                limitPrice="1900",
                amount="0.02",
                amountUnit="ETH",
                stopLossPrice="1800",
                takeProfitPrice="2200",
            ),
        )

    assert response.status_code == 200
    client.place_limit_order.assert_awaited_once()
    args = client.place_limit_order.await_args.args
    assert args[:4] == ("ETHUSDT", "BUY", Decimal("0.02"), Decimal("1900"))
    runtime = app.state.accounts[0]
    bracket = runtime.store.get_active_bracket("ETHUSDT")
    assert bracket["status"] == "awaiting_entry"
    assert bracket["source"] == "manual"
    assert bracket["entry_timeout_seconds"] is None
    assert bracket["working_type"] == "MARK_PRICE"


def test_leverage_failure_prevents_order_submission(tmp_path):
    client = make_binance()
    client.change_leverage.side_effect = RuntimeError("leverage rejected")
    app, client = make_manual_app(tmp_path, client)

    with TestClient(app) as http:
        response = http.post(
            "/api/manual/orders",
            headers=auth_headers(),
            json=order_payload(clientRequestId="leverage-failure-0001"),
        )

    assert response.status_code == 502
    client.place_market_order.assert_not_awaited()
    client.place_limit_order.assert_not_awaited()


def test_order_timeout_recovers_by_client_order_id_without_resubmitting(tmp_path):
    client = make_binance()
    client.get_position.side_effect = [
        Position(symbol="ETHUSDT", amount=Decimal("0")),
        Position(symbol="ETHUSDT", amount=Decimal("0.05")),
    ]
    client.place_market_order.side_effect = httpx.ReadTimeout("timed out")
    client.query_order_by_client_id.return_value = {
        "orderId": 1003,
        "status": "FILLED",
        "executedQty": "0.05",
        "avgPrice": "2000",
    }
    app, client = make_manual_app(tmp_path, client)

    with TestClient(app) as http:
        response = http.post(
            "/api/manual/orders",
            headers=auth_headers(),
            json=order_payload(clientRequestId="timeout-recovery-0001"),
        )

    assert response.status_code == 200
    assert client.place_market_order.await_count == 1
    client.query_order_by_client_id.assert_awaited_once()


@pytest.mark.parametrize(
    ("stop", "take", "expected_types"),
    [
        (None, None, []),
        ("1900", None, ["STOP_MARKET"]),
        (None, "2200", ["TAKE_PROFIT_MARKET"]),
        ("1900", "2200", ["STOP_MARKET", "TAKE_PROFIT_MARKET"]),
    ],
)
def test_position_protection_supports_all_optional_combinations(
    tmp_path, stop, take, expected_types
):
    client = make_binance()
    client.get_position.return_value = Position(
        symbol="ETHUSDT",
        amount=Decimal("0.5"),
        raw={"markPrice": "2000", "positionAmt": "0.5"},
    )
    ids = iter([201, 202])

    async def place_algo(*args, **kwargs):
        return {"algoId": next(ids), "closePosition": True}

    client.place_close_position_algo_order.side_effect = place_algo
    app, client = make_manual_app(tmp_path, client)
    payload = {"clientRequestId": f"protect-{stop}-{take}"}
    if stop:
        payload["stopLossPrice"] = stop
    if take:
        payload["takeProfitPrice"] = take

    with TestClient(app) as http:
        response = http.put(
            "/api/manual/positions/ETHUSDT/protection",
            headers=auth_headers(),
            json=payload,
        )

    assert response.status_code == 200
    actual_types = [
        call.args[2] for call in client.place_close_position_algo_order.await_args_list
    ]
    assert actual_types == expected_types
    for call in client.place_close_position_algo_order.await_args_list:
        assert call.kwargs["working_type"] == "MARK_PRICE"


def test_failed_protection_replacement_cancels_new_order_and_keeps_old_record(
    tmp_path,
):
    client = make_binance()
    client.get_position.return_value = Position(
        symbol="ETHUSDT",
        amount=Decimal("0.5"),
        raw={"markPrice": "2000", "positionAmt": "0.5"},
    )
    client.place_close_position_algo_order.side_effect = [
        {"algoId": 301, "closePosition": True},
        RuntimeError("take profit rejected"),
    ]
    app, client = make_manual_app(tmp_path, client)
    store = app.state.accounts[0].store
    old = store.create_manual_bracket(
        "old-protection",
        "ETHUSDT",
        None,
        None,
        "BUY",
        "1850",
        "2250",
        status="protected",
        stop_algo_id=201,
        take_profit_algo_id=202,
    )

    with TestClient(app) as http:
        response = http.put(
            "/api/manual/positions/ETHUSDT/protection",
            headers=auth_headers(),
            json={
                "clientRequestId": "replace-protection-fail-0001",
                "stopLossPrice": "1900",
                "takeProfitPrice": "2200",
            },
        )

    assert response.status_code == 502
    client.cancel_algo_order.assert_awaited_once_with(301)
    active = store.get_active_bracket("ETHUSDT")
    assert active["id"] == old["id"]
    assert active["stop_loss_price"] == "1850"
    assert active["take_profit_price"] == "2250"


def test_market_close_cancels_orders_and_uses_full_reduce_only_quantity(tmp_path):
    client = make_binance()
    client.get_position.side_effect = [
        Position(symbol="ETHUSDT", amount=Decimal("-0.75")),
        Position(symbol="ETHUSDT", amount=Decimal("0")),
    ]
    app, client = make_manual_app(tmp_path, client)

    with TestClient(app) as http:
        response = http.post(
            "/api/manual/positions/ETHUSDT/close",
            headers=auth_headers(),
            json={"clientRequestId": "close-position-0001"},
        )

    assert response.status_code == 200
    client.cancel_all_open_orders.assert_awaited_once_with("ETHUSDT")
    client.cancel_all_algo_open_orders.assert_awaited_once_with("ETHUSDT")
    args = client.place_market_order.await_args.args
    assert args[:4] == ("ETHUSDT", "BUY", Decimal("0.75"), True)


def test_cancel_rejects_untracked_binance_order(tmp_path):
    app, client = make_manual_app(tmp_path)
    with TestClient(app) as http:
        response = http.request(
            "DELETE",
            "/api/manual/orders/9999",
            headers=auth_headers(),
            json={"clientRequestId": "cancel-order-9999"},
        )

    assert response.status_code == 404
    client.cancel_order.assert_not_awaited()


def test_duplicate_manual_tokens_fail_startup(tmp_path):
    settings = Settings(
        webhook_secret="test-secret",
        sqlite_path=tmp_path / "events.db",
        dry_run=False,
        binance_accounts=(
            BinanceAccount(
                name="a",
                api_key="key-a",
                api_secret="secret-a",
                manual_token=TOKEN,
            ),
            BinanceAccount(
                name="b",
                api_key="key-b",
                api_secret="secret-b",
                manual_token=TOKEN,
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="must be unique"):
        create_app(settings, clients={"a": make_binance(), "b": make_binance()})
