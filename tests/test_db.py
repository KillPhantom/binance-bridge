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
        action="open_long",
        price="40000.10",
        amount="0.002",
    )
    assert store.claim(signal, signal.model_dump(mode="json")) == "claimed"

    event = store.get("limit-1")
    assert event["price"] == 40000.10
    assert event["amount"] == 0.002
