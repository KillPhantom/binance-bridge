from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .models import TradingViewSignal


ClaimResult = Literal["claimed", "duplicate_success", "processing", "failed_needs_retry"]


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    event_id TEXT UNIQUE NOT NULL,
                    received_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    notional REAL,
                    price REAL,
                    amount REAL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    position_before_json TEXT,
                    position_after_json TEXT,
                    binance_responses_json TEXT
                )
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()
            }
            if "price" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN price REAL")
            if "amount" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN amount REAL")

    def claim(self, signal: TradingViewSignal, payload: dict[str, Any]) -> ClaimResult:
        now = datetime.now(timezone.utc).isoformat()
        safe_payload = dict(payload)
        safe_payload["token"] = "[REDACTED]"
        encoded = json.dumps(safe_payload, separators=(",", ":"), sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM events WHERE event_id = ?", (signal.event_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO events
                    (event_id, received_at, symbol, action, price, amount, payload_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'processing')""",
                    (
                        signal.event_id,
                        now,
                        signal.symbol,
                        signal.action,
                        float(signal.price) if signal.price is not None else None,
                        float(signal.amount) if signal.amount is not None else None,
                        encoded,
                    ),
                )
                return "claimed"
            if row["status"] == "success":
                return "duplicate_success"
            if row["status"] == "processing":
                return "processing"
            if not signal.retry:
                return "failed_needs_retry"
            db.execute(
                """UPDATE events SET received_at=?, symbol=?, action=?, price=?, amount=?,
                payload_json=?, status='processing', error=NULL, position_before_json=NULL,
                position_after_json=NULL, binance_responses_json=NULL WHERE event_id=?""",
                (
                    now,
                    signal.symbol,
                    signal.action,
                    float(signal.price) if signal.price is not None else None,
                    float(signal.amount) if signal.amount is not None else None,
                    encoded,
                    signal.event_id,
                ),
            )
            return "claimed"

    def mark_success(
        self,
        event_id: str,
        position_before: dict[str, Any],
        position_after: dict[str, Any],
        responses: list[dict[str, Any]],
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE events SET status='success', error=NULL,
                position_before_json=?, position_after_json=?, binance_responses_json=?
                WHERE event_id=?""",
                (json.dumps(position_before), json.dumps(position_after), json.dumps(responses), event_id),
            )

    def mark_failed(self, event_id: str, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE events SET status='failed', error=? WHERE event_id=?",
                (error[:4000], event_id),
            )

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return dict(row) if row else None
