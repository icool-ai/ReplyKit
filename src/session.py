"""Session store: chat history + bot session snapshot, owned by username (SQLAlchemy)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import Engine, and_, func, select
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, utc_now
from mp_agent.dao.models import ChatSession
from mp_agent.dao.sync_db import sync_engine


@dataclass
class SessionData:
    history: list[dict[str, str]] = field(default_factory=list)
    bot_state: dict[str, Any] = field(default_factory=dict)
    username: str | None = None
    title: str = ""
    created_at: int = 0
    updated_at: int = 0


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    title: str
    preview: str
    updated_at: int
    created_at: int


@dataclass(frozen=True)
class SessionPage:
    items: list[SessionSummary]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1


def _normalize_session_id(session_id: str) -> str:
    return (session_id or "").strip() or "default"


def _parse_history(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "")
        if role and content is not None:
            out.append({"role": role, "content": content})
    return out


def _parse_bot_state(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _title_from_history(history: list[dict[str, str]], existing: str = "") -> str:
    if (existing or "").strip():
        return existing.strip()[:60]
    for msg in history:
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            text = msg["content"].strip().replace("\n", " ")
            return text[:30] + ("…" if len(text) > 30 else "")
    return "新会话"


def _preview_from_history(history: list[dict[str, str]]) -> str:
    for msg in reversed(history):
        if msg.get("role") == "assistant" and (msg.get("content") or "").strip():
            text = msg["content"].strip().replace("\n", " ")
            return text[:40] + ("…" if len(text) > 40 else "")
    return ""


class SessionStore(Protocol):
    def get(self, session_id: str) -> SessionData: ...

    def save(
        self,
        session_id: str,
        *,
        history: list[dict[str, str]],
        bot_state: dict[str, Any],
        username: str | None = None,
        title: str | None = None,
    ) -> None: ...

    def clear(self, session_id: str) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionData] = {}
        self._lock = __import__("threading").Lock()

    def get(self, session_id: str) -> SessionData:
        key = _normalize_session_id(session_id)
        with self._lock:
            if key not in self._sessions:
                self._sessions[key] = SessionData()
            session = self._sessions[key]
            return SessionData(
                history=list(session.history),
                bot_state=dict(session.bot_state),
                username=session.username,
                title=session.title,
                created_at=session.created_at,
                updated_at=session.updated_at,
            )

    def save(
        self,
        session_id: str,
        *,
        history: list[dict[str, str]],
        bot_state: dict[str, Any],
        username: str | None = None,
        title: str | None = None,
    ) -> None:
        key = _normalize_session_id(session_id)
        now = int(time.time())
        with self._lock:
            prev = self._sessions.get(key)
            owner = username if username is not None else (prev.username if prev else None)
            created = prev.created_at if prev and prev.created_at else now
            resolved_title = _title_from_history(
                history, title if title is not None else (prev.title if prev else "")
            )
            self._sessions[key] = SessionData(
                history=list(history),
                bot_state=dict(bot_state),
                username=owner,
                title=resolved_title,
                created_at=created,
                updated_at=now,
            )

    def clear(self, session_id: str) -> None:
        key = _normalize_session_id(session_id)
        with self._lock:
            self._sessions.pop(key, None)

    def list_for_user(
        self, username: str, *, page: int = 1, page_size: int = 20
    ) -> SessionPage:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        with self._lock:
            owned = [
                (sid, data)
                for sid, data in self._sessions.items()
                if data.username == username
            ]
        owned.sort(key=lambda x: x[1].updated_at, reverse=True)
        total = len(owned)
        start = (page - 1) * page_size
        chunk = owned[start : start + page_size]
        items = [
            SessionSummary(
                session_id=sid,
                title=data.title or "新会话",
                preview=_preview_from_history(data.history),
                updated_at=data.updated_at,
                created_at=data.created_at,
            )
            for sid, data in chunk
        ]
        return SessionPage(items=items, total=total, page=page, page_size=page_size)

    def get_for_user(self, session_id: str, username: str) -> SessionData | None:
        data = self.get(session_id)
        if data.username != username:
            return None
        key = _normalize_session_id(session_id)
        with self._lock:
            if key not in self._sessions or self._sessions[key].username != username:
                return None
        return data

    def delete_for_user(self, session_id: str, username: str) -> bool:
        key = _normalize_session_id(session_id)
        with self._lock:
            data = self._sessions.get(key)
            if data is None or data.username != username:
                return False
            del self._sessions[key]
            return True


class SqliteSessionStore:
    """Persist sessions via SQLAlchemy ORM (drop-in replacement for old sqlite3 impl)."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or sync_engine

    def _row_to_data(self, s: ChatSession | None) -> SessionData:
        if s is None:
            return SessionData()
        history = _parse_history(s.history_json or [])
        bot_state = _parse_bot_state(s.bot_state_json or {})
        return SessionData(
            history=history,
            bot_state=bot_state,
            username=s.username,
            title=s.title or "",
            created_at=dt_to_unix(s.created_at),
            updated_at=dt_to_unix(s.updated_at),
        )

    def get(self, session_id: str) -> SessionData:
        key = _normalize_session_id(session_id)
        with Session(self._engine) as db:
            row = db.scalar(
                select(ChatSession).where(ChatSession.session_id == key)
            )
            return self._row_to_data(row)

    def save(
        self,
        session_id: str,
        *,
        history: list[dict[str, str]],
        bot_state: dict[str, Any],
        username: str | None = None,
        title: str | None = None,
    ) -> None:
        key = _normalize_session_id(session_id)
        now_dt = utc_now()
        now_unix = int(time.time())

        with Session(self._engine) as db:
            row = db.scalar(
                select(ChatSession).where(ChatSession.session_id == key)
            )
            owner = username
            if owner is None and row is not None and row.username:
                owner = row.username
            existing_title = row.title if row is not None else ""
            resolved_title = _title_from_history(
                history, title if title is not None else existing_title
            )
            created_at_dt = (
                row.created_at
                if row is not None and row.created_at and dt_to_unix(row.created_at) > 0
                else now_dt
            )

            if row is None:
                db.add(
                    ChatSession(
                        session_id=key,
                        username=owner,
                        title=resolved_title,
                        history_json=list(history),
                        bot_state_json=dict(bot_state),
                        created_at=created_at_dt,
                        updated_at=now_dt,
                    )
                )
            else:
                row.username = owner
                row.title = resolved_title
                row.history_json = list(history)
                row.bot_state_json = dict(bot_state)
                row.created_at = created_at_dt
                row.updated_at = now_dt
            db.commit()

    def clear(self, session_id: str) -> None:
        key = _normalize_session_id(session_id)
        with Session(self._engine) as db:
            row = db.scalar(
                select(ChatSession).where(ChatSession.session_id == key)
            )
            if row is not None:
                db.delete(row)
                db.commit()

    def list_for_user(
        self, username: str, *, page: int = 1, page_size: int = 20
    ) -> SessionPage:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        user = (username or "").strip()

        with Session(self._engine) as db:
            total = db.scalar(
                select(func.count()).select_from(ChatSession).where(
                    ChatSession.username == user
                )
            ) or 0
            offset = (page - 1) * page_size
            rows = db.execute(
                select(ChatSession)
                .where(ChatSession.username == user)
                .order_by(ChatSession.updated_at.desc())
                .limit(page_size)
                .offset(offset)
            ).scalars().all()

            items: list[SessionSummary] = []
            for row in rows:
                history = _parse_history(row.history_json or [])
                items.append(
                    SessionSummary(
                        session_id=row.session_id,
                        title=row.title or "新会话",
                        preview=_preview_from_history(history),
                        updated_at=dt_to_unix(row.updated_at),
                        created_at=dt_to_unix(row.created_at),
                    )
                )
            return SessionPage(
                items=items, total=total, page=page, page_size=page_size
            )

    def get_for_user(self, session_id: str, username: str) -> SessionData | None:
        key = _normalize_session_id(session_id)
        user = (username or "").strip()
        with Session(self._engine) as db:
            row = db.scalar(
                select(ChatSession).where(
                    and_(ChatSession.session_id == key, ChatSession.username == user)
                )
            )
            if row is None:
                return None
            return self._row_to_data(row)

    def delete_for_user(self, session_id: str, username: str) -> bool:
        key = _normalize_session_id(session_id)
        user = (username or "").strip()
        with Session(self._engine) as db:
            row = db.scalar(
                select(ChatSession).where(
                    and_(ChatSession.session_id == key, ChatSession.username == user)
                )
            )
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True

    def close(self) -> None:
        return None
