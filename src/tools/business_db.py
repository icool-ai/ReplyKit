"""Business DB: orders + tickets (SQLAlchemy)."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Generator

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from mp_agent.dao.models import Order
from mp_agent.dao.sync_db import sync_engine


_dummy_lock = Lock()

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

_engine: Engine | None = None


def configure_business_db(engine: Engine | None = None) -> None:
    """Open (or reopen) the business DB and ensure schema + seed data."""
    global _engine
    _engine = engine or sync_engine
    _seed_orders()


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError(
            "业务库未初始化：请先调用 configure_business_db(settings.business_db_path)。"
        )
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Return a transactional SQLAlchemy session for business DB operations."""
    with Session(get_engine()) as session:
        yield session


def get_lock() -> Lock:
    """Backward-compatible no-op lock (SQLAlchemy handles transaction isolation)."""
    return _dummy_lock


def _seed_orders() -> None:
    engine = get_engine()
    with Session(engine) as session:
        count = session.query(Order).count()
        if count > 0:
            return
        for row in SEED_ORDERS:
            session.add(
                Order(
                    order_id=row["order_id"],
                    status=row["status"],
                    carrier=row["carrier"],
                    tracking_no=row["tracking_no"],
                    eta=row["eta"],
                    last_event=row["last_event"],
                )
            )
        session.commit()
