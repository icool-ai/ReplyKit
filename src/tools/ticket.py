"""Ticket creation tool (SQLite-backed; swap for real HTTP later)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.tools.business_db import get_connection, get_lock


def create_ticket(
    description: str,
    order_id: str | None = None,
) -> str:
    """Insert a ticket and return its ticket_id (e.g. TKT202607210001)."""
    text = (description or "").strip()
    if not text:
        raise ValueError("工单描述不能为空")

    oid = (order_id or "").strip().upper() or None
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"TKT{day}"
    now = datetime.now(timezone.utc).isoformat()

    with get_lock():
        conn = get_connection()
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                """
                SELECT ticket_id FROM tickets
                WHERE ticket_id LIKE ?
                ORDER BY ticket_id DESC
                LIMIT 1
                """,
                (f"{prefix}%",),
            ).fetchone()
            if row is None:
                seq = 1
            else:
                last = str(row["ticket_id"])
                try:
                    seq = int(last[len(prefix) :]) + 1
                except ValueError:
                    seq = 1
            ticket_id = f"{prefix}{seq:04d}"
            conn.execute(
                """
                INSERT INTO tickets
                    (ticket_id, description, order_id, status, created_at)
                VALUES (?, ?, ?, 'open', ?)
                """,
                (ticket_id, text, oid, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return ticket_id
