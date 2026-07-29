import sqlite3

from app.db import EventStore
from app.models import TradingViewSignal


def test_existing_events_table_migrates_price_and_amount(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                event_id TEXT UNIQUE NOT NULL,
                received_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                notional REAL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                position_before_json TEXT,
                position_after_json TEXT,
                binance_responses_json TEXT
            )"""
        )

    store = EventStore(path)
    signal = TradingViewSignal(
        token="secret",
        event_id="limit-1",
        symbol="BTCUSDT",
        side="buy",
        positionSide="BOTH",
        investmentType="notional_value",
        price="40000.10",
        amount="80",
        reduceOnly=False,
        stopLossPrice="39000",
        takeProfitPrice="41000",
    )
    assert store.claim(signal, signal.model_dump(mode="json")) == "claimed"

    event = store.get("limit-1")
    assert event["price"] == 40000.10
    assert event["amount"] == 80
    assert event["side"] == "buy"
    assert event["reduce_only"] == 0


def test_legacy_brackets_table_migrates_to_optional_manual_schema(tmp_path):
    path = tmp_path / "old-brackets.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE brackets (
                id INTEGER PRIMARY KEY,
                event_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                entry_order_id INTEGER NOT NULL,
                entry_side TEXT NOT NULL,
                stop_loss_price TEXT NOT NULL,
                take_profit_price TEXT NOT NULL,
                status TEXT NOT NULL,
                stop_algo_id INTEGER,
                take_profit_algo_id INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """INSERT INTO brackets
            (event_id, symbol, entry_order_id, entry_side, stop_loss_price,
             take_profit_price, status, created_at, updated_at)
            VALUES ('legacy-1', 'ETHUSDT', 10, 'BUY', '1900', '2200',
                    'protected', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00')"""
        )

    store = EventStore(path)
    legacy = store.get_active_bracket("ETHUSDT")
    manual = store.create_manual_bracket(
        "manual-1",
        "ETHUSDT",
        None,
        None,
        "BUY",
        "1900",
        None,
        status="protected",
        stop_algo_id=20,
    )

    assert legacy["source"] == "tradingview"
    assert legacy["working_type"] == "CONTRACT_PRICE"
    assert legacy["entry_timeout_seconds"] == 60
    assert manual["entry_order_id"] is None
    assert manual["take_profit_price"] is None
    assert manual["source"] == "manual"
