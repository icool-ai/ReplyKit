"""Session store: chat history + bot session snapshot, owned by username."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Protocol


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


def _iso_to_unix(raw: object) -> int:
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return 0


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
        self._lock = Lock()

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
        # empty brand-new get creates empty session without username — treat missing row
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
    """Persist sessions in a local SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    history_json TEXT NOT NULL DEFAULT '[]',
                    bot_state_json TEXT NOT NULL DEFAULT '{}',
                    username TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cols = {
                str(r[1])
                for r in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "username" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN username TEXT")
            if "title" not in cols:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            if "created_at" not in cols:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0"
                )
            # migrate legacy TEXT updated_at → keep column; also store unix in created/updated
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_user_updated "
                "ON sessions(username, updated_at DESC)"
            )

    def get(self, session_id: str) -> SessionData:
        key = _normalize_session_id(session_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT history_json, bot_state_json, username, title,
                       created_at, updated_at
                FROM sessions WHERE session_id = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return SessionData()
        try:
            history = _parse_history(json.loads(row["history_json"] or "[]"))
        except json.JSONDecodeError:
            history = []
        try:
            bot_state = _parse_bot_state(json.loads(row["bot_state_json"] or "{}"))
        except json.JSONDecodeError:
            bot_state = {}
        return SessionData(
            history=history,
            bot_state=bot_state,
            username=(str(row["username"]) if row["username"] else None),
            title=str(row["title"] or ""),
            created_at=_iso_to_unix(row["created_at"]),
            updated_at=_iso_to_unix(row["updated_at"]),
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
        history_json = json.dumps(list(history), ensure_ascii=False)
        bot_state_json = json.dumps(dict(bot_state), ensure_ascii=False)
        with self._lock:
            prev = self._conn.execute(
                "SELECT username, title, created_at FROM sessions WHERE session_id = ?",
                (key,),
            ).fetchone()
            owner = username
            if owner is None and prev is not None and prev["username"]:
                owner = str(prev["username"])
            existing_title = str(prev["title"]) if prev is not None else ""
            resolved_title = _title_from_history(
                history, title if title is not None else existing_title
            )
            created = (
                _iso_to_unix(prev["created_at"])
                if prev is not None and prev["created_at"]
                else now
            )
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, history_json, bot_state_json,
                        username, title, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        history_json = excluded.history_json,
                        bot_state_json = excluded.bot_state_json,
                        username = COALESCE(excluded.username, sessions.username),
                        title = excluded.title,
                        created_at = CASE
                            WHEN sessions.created_at IS NULL OR sessions.created_at = 0
                            THEN excluded.created_at
                            ELSE sessions.created_at
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        history_json,
                        bot_state_json,
                        owner,
                        resolved_title,
                        created,
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def clear(self, session_id: str) -> None:
        key = _normalize_session_id(session_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (key,),
            )

    def list_for_user(
        self, username: str, *, page: int = 1, page_size: int = 20
    ) -> SessionPage:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        user = (username or "").strip()
        with self._lock:
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE username = ?",
                    (user,),
                ).fetchone()["n"]
            )
            offset = (page - 1) * page_size
            rows = self._conn.execute(
                """
                SELECT session_id, title, history_json, created_at, updated_at
                FROM sessions
                WHERE username = ?
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (user, page_size, offset),
            ).fetchall()
        items: list[SessionSummary] = []
        for row in rows:
            try:
                history = _parse_history(json.loads(row["history_json"] or "[]"))
            except json.JSONDecodeError:
                history = []
            items.append(
                SessionSummary(
                    session_id=str(row["session_id"]),
                    title=str(row["title"] or "") or "新会话",
                    preview=_preview_from_history(history),
                    updated_at=_iso_to_unix(row["updated_at"]),
                    created_at=_iso_to_unix(row["created_at"]),
                )
            )
        return SessionPage(
            items=items, total=total, page=page, page_size=page_size
        )

    def get_for_user(self, session_id: str, username: str) -> SessionData | None:
        key = _normalize_session_id(session_id)
        user = (username or "").strip()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT history_json, bot_state_json, username, title,
                       created_at, updated_at
                FROM sessions
                WHERE session_id = ? AND username = ?
                """,
                (key, user),
            ).fetchone()
        if row is None:
            return None
        try:
            history = _parse_history(json.loads(row["history_json"] or "[]"))
        except json.JSONDecodeError:
            history = []
        try:
            bot_state = _parse_bot_state(json.loads(row["bot_state_json"] or "{}"))
        except json.JSONDecodeError:
            bot_state = {}
        return SessionData(
            history=history,
            bot_state=bot_state,
            username=str(row["username"]),
            title=str(row["title"] or ""),
            created_at=_iso_to_unix(row["created_at"]),
            updated_at=_iso_to_unix(row["updated_at"]),
        )

    def delete_for_user(self, session_id: str, username: str) -> bool:
        key = _normalize_session_id(session_id)
        user = (username or "").strip()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ? AND username = ?",
                (key, user),
            )
            return (cur.rowcount or 0) > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
