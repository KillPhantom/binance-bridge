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
                    side TEXT,
                    reduce_only INTEGER,
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
            if "side" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN side TEXT")
            if "reduce_only" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN reduce_only INTEGER")
            self._initialize_brackets(db)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_actions (
                    id INTEGER PRIMARY KEY,
                    request_id TEXT UNIQUE NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_orders (
                    order_id INTEGER PRIMARY KEY,
                    client_order_id TEXT UNIQUE NOT NULL,
                    request_id TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _create_brackets_table(db: sqlite3.Connection, name: str = "brackets") -> None:
        db.execute(
            f"""
            CREATE TABLE {name} (
                id INTEGER PRIMARY KEY,
                event_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                entry_order_id INTEGER,
                entry_client_order_id TEXT,
                entry_side TEXT NOT NULL,
                stop_loss_price TEXT,
                take_profit_price TEXT,
                working_type TEXT NOT NULL DEFAULT 'CONTRACT_PRICE',
                source TEXT NOT NULL DEFAULT 'tradingview',
                entry_timeout_seconds REAL,
                status TEXT NOT NULL,
                stop_algo_id INTEGER,
                take_profit_algo_id INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _initialize_brackets(self, db: sqlite3.Connection) -> None:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brackets'"
        ).fetchone()
        if not exists:
            self._create_brackets_table(db)
            return
        info = db.execute("PRAGMA table_info(brackets)").fetchall()
        columns = {row["name"]: row for row in info}
        current = {
            "entry_client_order_id",
            "working_type",
            "source",
            "entry_timeout_seconds",
        }.issubset(columns)
        nullable_protection = (
            "entry_order_id" in columns
            and columns["entry_order_id"]["notnull"] == 0
            and "stop_loss_price" in columns
            and columns["stop_loss_price"]["notnull"] == 0
            and "take_profit_price" in columns
            and columns["take_profit_price"]["notnull"] == 0
        )
        if current and nullable_protection:
            return

        self._create_brackets_table(db, "brackets_v2")
        db.execute(
            """
            INSERT INTO brackets_v2
            (id, event_id, symbol, entry_order_id, entry_client_order_id,
             entry_side, stop_loss_price, take_profit_price, working_type,
             source, entry_timeout_seconds, status, stop_algo_id,
             take_profit_algo_id, error, created_at, updated_at)
            SELECT id, event_id, symbol, entry_order_id, NULL,
                   entry_side, stop_loss_price, take_profit_price,
                   'CONTRACT_PRICE', 'tradingview', 60,
                   status, stop_algo_id, take_profit_algo_id, error,
                   created_at, updated_at
            FROM brackets
            """
        )
        db.execute("DROP TABLE brackets")
        db.execute("ALTER TABLE brackets_v2 RENAME TO brackets")

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
                    (event_id, received_at, symbol, action, side, reduce_only,
                     price, amount, payload_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing')""",
                    (
                        signal.event_id,
                        now,
                        signal.symbol,
                        signal.side,
                        signal.side,
                        int(signal.reduce_only),
                        float(signal.price),
                        float(signal.amount),
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
                """UPDATE events SET received_at=?, symbol=?, action=?, side=?,
                reduce_only=?, price=?, amount=?,
                payload_json=?, status='processing', error=NULL, position_before_json=NULL,
                position_after_json=NULL, binance_responses_json=NULL WHERE event_id=?""",
                (
                    now,
                    signal.symbol,
                    signal.side,
                    signal.side,
                    int(signal.reduce_only),
                    float(signal.price),
                    float(signal.amount),
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

    def create_bracket(
        self,
        event_id: str,
        symbol: str,
        entry_order_id: int,
        entry_side: str,
        stop_loss_price: str,
        take_profit_price: str,
        *,
        working_type: str = "CONTRACT_PRICE",
        entry_timeout_seconds: float | None = 60,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                """UPDATE brackets SET status='superseded', updated_at=?
                WHERE symbol=? AND status IN ('awaiting_entry', 'protecting', 'protected')""",
                (now, symbol),
            )
            db.execute(
                """INSERT INTO brackets
                (event_id, symbol, entry_order_id, entry_side, stop_loss_price,
                 take_profit_price, working_type, source, entry_timeout_seconds,
                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'tradingview', ?,
                        'awaiting_entry', ?, ?)""",
                (
                    event_id,
                    symbol,
                    entry_order_id,
                    entry_side,
                    stop_loss_price,
                    take_profit_price,
                    working_type,
                    entry_timeout_seconds,
                    now,
                    now,
                ),
            )

    def create_manual_bracket(
        self,
        event_id: str,
        symbol: str,
        entry_order_id: int | None,
        entry_client_order_id: str | None,
        entry_side: str,
        stop_loss_price: str | None,
        take_profit_price: str | None,
        *,
        status: str,
        stop_algo_id: int | None = None,
        take_profit_algo_id: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                """UPDATE brackets SET status='superseded', updated_at=?
                WHERE symbol=? AND status IN
                ('awaiting_entry', 'protecting', 'protected', 'monitoring')""",
                (now, symbol),
            )
            cursor = db.execute(
                """INSERT INTO brackets
                (event_id, symbol, entry_order_id, entry_client_order_id,
                 entry_side, stop_loss_price, take_profit_price, working_type,
                 source, entry_timeout_seconds, status, stop_algo_id,
                 take_profit_algo_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'MARK_PRICE', 'manual', NULL,
                        ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    symbol,
                    entry_order_id,
                    entry_client_order_id,
                    entry_side,
                    stop_loss_price,
                    take_profit_price,
                    status,
                    stop_algo_id,
                    take_profit_algo_id,
                    now,
                    now,
                ),
            )
            bracket_id = int(cursor.lastrowid)
        result = self.get_bracket(bracket_id)
        if result is None:
            raise RuntimeError("manual bracket was not persisted")
        return result

    def deactivate_active_brackets(self, symbol: str, status: str) -> None:
        if status not in {"superseded", "manual_reduce"}:
            raise ValueError("invalid bracket deactivation status")
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                """UPDATE brackets SET status=?, updated_at=?
                WHERE symbol=? AND status IN
                ('awaiting_entry', 'protecting', 'protected', 'monitoring')""",
                (status, now, symbol),
            )

    def list_active_brackets(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM brackets
                WHERE status IN
                ('awaiting_entry', 'protecting', 'protected', 'monitoring')
                ORDER BY id"""
            ).fetchall()
            return [dict(row) for row in rows]

    def get_active_bracket(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM brackets
                WHERE symbol=? AND status IN
                ('awaiting_entry', 'protecting', 'protected', 'monitoring')
                ORDER BY id DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None

    def get_bracket(self, bracket_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM brackets WHERE id=?", (bracket_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_bracket(self, bracket_id: int, **changes: Any) -> None:
        allowed = {
            "status",
            "stop_algo_id",
            "take_profit_algo_id",
            "stop_loss_price",
            "take_profit_price",
            "error",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"invalid bracket fields: {sorted(invalid)}")
        if not changes:
            return
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.connect() as db:
            db.execute(
                f"UPDATE brackets SET {assignments} WHERE id=?",
                (*changes.values(), bracket_id),
            )

    def claim_manual_action(
        self, request_id: str, action: str, payload: dict[str, Any]
    ) -> ClaimResult:
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM manual_actions WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO manual_actions
                    (request_id, action, payload_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'processing', ?, ?)""",
                    (request_id, action, encoded, now, now),
                )
                return "claimed"
            if row["status"] == "success":
                return "duplicate_success"
            if row["status"] == "processing":
                return "processing"
            return "failed_needs_retry"

    def finish_manual_action(
        self,
        request_id: str,
        *,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        status = "failed" if error else "success"
        with self.connect() as db:
            db.execute(
                """UPDATE manual_actions
                SET status=?, response_json=?, error=?, updated_at=?
                WHERE request_id=?""",
                (
                    status,
                    json.dumps(response) if response is not None else None,
                    error[:4000] if error else None,
                    now,
                    request_id,
                ),
            )

    def get_manual_action(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM manual_actions WHERE request_id=?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def record_manual_order(
        self,
        *,
        order_id: int,
        client_order_id: str,
        request_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None,
        status: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO manual_orders
                (order_id, client_order_id, request_id, symbol, side, order_type,
                 quantity, price, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT created_at FROM manual_orders
                                  WHERE order_id=?), ?), ?)""",
                (
                    order_id,
                    client_order_id,
                    request_id,
                    symbol,
                    side,
                    order_type,
                    quantity,
                    price,
                    status,
                    order_id,
                    now,
                    now,
                ),
            )

    def get_manual_order(self, order_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM manual_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_manual_order_status(self, order_id: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                "UPDATE manual_orders SET status=?, updated_at=? WHERE order_id=?",
                (status, now, order_id),
            )

    def list_manual_orders(self, symbol: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM manual_orders WHERE symbol=?
                ORDER BY created_at DESC LIMIT 50""",
                (symbol,),
            ).fetchall()
            return [dict(row) for row in rows]
