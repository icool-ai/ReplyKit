"""SQLite business DB: orders + tickets (P2-3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Lock

_lock = Lock()
_conn: sqlite3.Connection | None = None
_db_path: Path | None = None

SEED_ORDERS: list[dict[str, str]] = [
    {
        "order_id": "ORD10001",
        "status": "运输中",
        "carrier": "顺丰速运",
        "tracking_no": "SF1234567890",
        "eta": "预计明天送达",
        "last_event": "已到达【上海转运中心】",
    },
    {
        "order_id": "ORD10002",
        "status": "已签收",
        "carrier": "中通快递",
        "tracking_no": "ZT9876543210",
        "eta": "昨日已签收",
        "last_event": "本人已签收",
    },
    {
        "order_id": "ORD10003",
        "status": "待发货",
        "carrier": "—",
        "tracking_no": "—",
        "eta": "现货订单，预计 24 小时内发货",
        "last_event": "仓库拣货中",
    },
]


def configure_business_db(db_path: Path) -> None:
    """Open (or reopen) the business DB and ensure schema + seed data."""
    global _conn, _db_path
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None and _db_path == path:
            return
        if _conn is not None:
            _conn.close()
        _db_path = path
        _conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
        )
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
        _seed_orders(_conn)


def get_connection() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError(
            "业务库未初始化：请先调用 configure_business_db(settings.business_db_path)。"
        )
    return _conn


def get_lock() -> Lock:
    return _lock


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            carrier TEXT NOT NULL DEFAULT '',
            tracking_no TEXT NOT NULL DEFAULT '',
            eta TEXT NOT NULL DEFAULT '',
            last_event TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )
        """
    )


def _seed_orders(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    if count > 0:
        return
    for row in SEED_ORDERS:
        conn.execute(
            """
            INSERT INTO orders
                (order_id, status, carrier, tracking_no, eta, last_event)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["order_id"],
                row["status"],
                row["carrier"],
                row["tracking_no"],
                row["eta"],
                row["last_event"],
            ),
        )
