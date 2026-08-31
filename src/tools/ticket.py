"""Ticket creation tool (SQLAlchemy-backed; swap for real HTTP later)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from mp_agent.dao.models import Ticket
from src.tools.business_db import get_lock, get_session


def _next_ticket_id(session: Session, prefix: str) -> str:
    last = session.scalar(
        select(Ticket.ticket_id)
        .where(Ticket.ticket_id.like(f"{prefix}%"))
        .order_by(Ticket.ticket_id.desc())
        .limit(1)
    )
    if last is None:
        seq = 1
    else:
        try:
            seq = int(str(last)[len(prefix) :]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}{seq:04d}"


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

    with get_lock():
        with get_session() as session:
            ticket_id = _next_ticket_id(session, prefix)
            session.add(
                Ticket(
                    ticket_id=ticket_id,
                    description=text,
                    order_id=oid,
                    status="open",
                )
            )
            session.commit()
    return ticket_id
