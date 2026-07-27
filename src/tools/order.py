"""Order lookup tool (SQLite-backed; swap for real HTTP later)."""

from __future__ import annotations

from src.tools.business_db import get_connection, get_lock


def lookup_order(order_id: str) -> dict[str, str] | None:
    oid = (order_id or "").strip().upper()
    if not oid:
        return None
    with get_lock():
        row = get_connection().execute(
            """
            SELECT order_id, status, carrier, tracking_no, eta, last_event
            FROM orders WHERE order_id = ?
            """,
            (oid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "order_id": str(row["order_id"]),
        "status": str(row["status"]),
        "carrier": str(row["carrier"]),
        "tracking_no": str(row["tracking_no"]),
        "eta": str(row["eta"]),
        "last_event": str(row["last_event"]),
    }


def list_order_ids() -> list[str]:
    with get_lock():
        rows = get_connection().execute(
            "SELECT order_id FROM orders ORDER BY order_id"
        ).fetchall()
    return [str(r["order_id"]) for r in rows]
