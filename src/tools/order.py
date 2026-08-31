"""Order lookup tool (SQLAlchemy-backed; swap for real HTTP later)."""

from __future__ import annotations

from sqlalchemy import select

from mp_agent.dao.models import Order
from src.tools.business_db import get_lock, get_session


def lookup_order(order_id: str) -> dict[str, str] | None:
    oid = (order_id or "").strip().upper()
    if not oid:
        return None
    with get_lock():
        with get_session() as session:
            order = session.scalar(select(Order).where(Order.order_id == oid))
            if order is None:
                return None
            return {
                "order_id": order.order_id,
                "status": order.status,
                "carrier": order.carrier,
                "tracking_no": order.tracking_no,
                "eta": order.eta,
                "last_event": order.last_event,
            }


def list_order_ids() -> list[str]:
    with get_lock():
        with get_session() as session:
            rows = session.execute(select(Order.order_id).order_by(Order.order_id))
            return [r[0] for r in rows]
