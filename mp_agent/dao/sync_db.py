"""Synchronous SQLAlchemy engine/session for legacy src stores.

Used while migrating the original sqlite3-based stores to SQLAlchemy.
Competitor analytics continues to use the async engine in ``db.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.config import DB_URL


def _normalize_sync_url(url: str) -> str:
    """Convert async SQLite dialect to sync dialect for the sync engine."""
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


_SYNC_URL = _normalize_sync_url(DB_URL)

_connect_args: dict = {}
if _SYNC_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

sync_engine = create_engine(
    _SYNC_URL,
    echo=False,
    connect_args=_connect_args,
    future=True,
)
_SyncSessionFactory = sessionmaker(sync_engine, expire_on_commit=False)


@contextmanager
def get_sync_session() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    session = _SyncSessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_sync_db() -> None:
    """Create all tables using the synchronous engine (SQLite-friendly bootstrap)."""
    from mp_agent.dao.models import Base

    Base.metadata.create_all(sync_engine)
