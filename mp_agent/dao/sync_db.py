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


def _ensure_faq_acl_columns() -> None:
    """SQLite-friendly additive migration for FAQ document ACL fields."""
    if not _SYNC_URL.startswith("sqlite"):
        return
    from sqlalchemy import text

    with sync_engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(faqs)")).fetchall()
        if not rows:
            return
        names = {str(r[1]) for r in rows}
        alters: list[str] = []
        if "owner_username" not in names:
            alters.append(
                "ALTER TABLE faqs ADD COLUMN owner_username VARCHAR(32) "
                "DEFAULT '' NOT NULL"
            )
        if "visibility" not in names:
            alters.append(
                "ALTER TABLE faqs ADD COLUMN visibility VARCHAR(16) "
                "DEFAULT 'public' NOT NULL"
            )
        if "allow_egress" not in names:
            alters.append(
                "ALTER TABLE faqs ADD COLUMN allow_egress BOOLEAN "
                "DEFAULT 1 NOT NULL"
            )
        for stmt in alters:
            conn.execute(text(stmt))


def _fix_sqlite_bigint_autoincrement_pks() -> None:
    """Rebuild tables whose PK is BIGINT (SQLite cannot autoincrement those).

    Historical ``create_all`` emitted ``id BIGINT PRIMARY KEY`` for ChatLog /
    ChatMessage; inserts then fail with NOT NULL on ``id``. Drop and recreate
    with INTEGER PRIMARY KEY (via model ``with_variant``).
    """
    if not _SYNC_URL.startswith("sqlite"):
        return
    from sqlalchemy import text

    from mp_agent.dao.models import Base, ChatLog, ChatMessage

    targets = ("chat_messages", "chat_logs")
    dropped: list[str] = []
    with sync_engine.begin() as conn:
        for name in targets:
            row = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name=:n"
                ),
                {"n": name},
            ).fetchone()
            if not row or not row[0]:
                continue
            ddl = " ".join(str(row[0]).upper().split())
            if "ID BIGINT" not in ddl:
                continue
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)

    if not dropped:
        return
    tables = []
    if "chat_messages" in dropped:
        tables.append(ChatMessage.__table__)
    if "chat_logs" in dropped:
        tables.append(ChatLog.__table__)
    Base.metadata.create_all(sync_engine, tables=tables)


def init_sync_db() -> None:
    """Create all tables using the synchronous engine (SQLite-friendly bootstrap)."""
    from mp_agent.dao.models import Base

    Base.metadata.create_all(sync_engine)
    _ensure_faq_acl_columns()
    _fix_sqlite_bigint_autoincrement_pks()
