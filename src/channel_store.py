"""Per-owner channel connector config (Feishu first; WeCom later).

Each logged-in user owns at most one row per channel. A non-empty Feishu
``app_id`` may be bound to only one owner; others get AppIdTakenError.
Webhook routing: fixed ``POST /webhooks/feishu``; resolve config by
App ID / Verification Token (``id`` remains an internal primary key).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

from src.config import PROJECT_ROOT

CHANNEL_FEISHU = "feishu"
FEISHU_CALLBACK_PATH = "/webhooks/feishu"


class AppIdTakenError(Exception):
    """Another user already bound this Feishu App ID."""

    def __init__(self, app_id: str, owner_username: str) -> None:
        self.app_id = app_id
        self.owner_username = owner_username
        super().__init__(
            f"App ID「{app_id}」已被用户「{owner_username}」绑定，仅对方可修改"
        )


@dataclass(frozen=True)
class ChannelConfigRow:
    id: str
    owner_username: str
    channel: str
    enabled: bool
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    created_at: int
    updated_at: int

    def to_public_dict(self) -> dict[str, Any]:
        """API response shape: never expose secret plaintext."""
        return {
            "id": self.id,
            "owner_username": self.owner_username,
            "channel": self.channel,
            "enabled": self.enabled,
            "app_id": self.app_id,
            "app_secret_set": bool(self.app_secret.strip()),
            "verification_token_set": bool(self.verification_token.strip()),
            "encrypt_key_set": bool(self.encrypt_key.strip()),
            "callback_path": FEISHU_CALLBACK_PATH,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ChannelStore:
    """SQLite store for channel credentials, isolated by owner_username."""

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
                CREATE TABLE IF NOT EXISTS channel_configs (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    app_id TEXT NOT NULL DEFAULT '',
                    app_secret TEXT NOT NULL DEFAULT '',
                    verification_token TEXT NOT NULL DEFAULT '',
                    encrypt_key TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(owner_username, channel)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_owner "
                "ON channel_configs(owner_username)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_enabled "
                "ON channel_configs(channel, enabled)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_app_id "
                "ON channel_configs(channel, app_id)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_for_owner(
        self, owner_username: str, channel: str = CHANNEL_FEISHU
    ) -> ChannelConfigRow | None:
        owner = owner_username.strip()
        if not owner:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM channel_configs
                WHERE owner_username = ? AND channel = ?
                """,
                (owner, channel),
            ).fetchone()
        return _row_to_config(row) if row else None

    def get_by_id(self, config_id: str) -> ChannelConfigRow | None:
        cid = config_id.strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channel_configs WHERE id = ?",
                (cid,),
            ).fetchone()
        return _row_to_config(row) if row else None

    def find_by_app_id(
        self, app_id: str, *, channel: str = CHANNEL_FEISHU
    ) -> ChannelConfigRow | None:
        aid = app_id.strip()
        if not aid:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM channel_configs
                WHERE channel = ? AND app_id = ?
                LIMIT 1
                """,
                (channel, aid),
            ).fetchone()
        return _row_to_config(row) if row else None

    def find_by_verification_token(
        self, token: str, *, channel: str = CHANNEL_FEISHU
    ) -> ChannelConfigRow | None:
        tok = token.strip()
        if not tok:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM channel_configs
                WHERE channel = ? AND verification_token = ?
                LIMIT 1
                """,
                (channel, tok),
            ).fetchone()
        return _row_to_config(row) if row else None

    def list_enabled_feishu(self) -> list[ChannelConfigRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM channel_configs
                WHERE channel = ? AND enabled = 1
                """,
                (CHANNEL_FEISHU,),
            ).fetchall()
        return [_row_to_config(r) for r in rows]

    def list_feishu(self) -> list[ChannelConfigRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM channel_configs
                WHERE channel = ?
                """,
                (CHANNEL_FEISHU,),
            ).fetchall()
        return [_row_to_config(r) for r in rows]

    def find_app_id_holder(
        self,
        app_id: str,
        *,
        channel: str = CHANNEL_FEISHU,
        exclude_owner: str | None = None,
    ) -> ChannelConfigRow | None:
        """Return another owner's row that already binds this app_id."""
        aid = app_id.strip()
        if not aid:
            return None
        with self._lock:
            if exclude_owner:
                row = self._conn.execute(
                    """
                    SELECT * FROM channel_configs
                    WHERE channel = ? AND app_id = ? AND owner_username != ?
                    LIMIT 1
                    """,
                    (channel, aid, exclude_owner.strip()),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT * FROM channel_configs
                    WHERE channel = ? AND app_id = ?
                    LIMIT 1
                    """,
                    (channel, aid),
                ).fetchone()
        return _row_to_config(row) if row else None

    def upsert_feishu(
        self,
        owner_username: str,
        *,
        enabled: bool,
        app_id: str,
        app_secret: str | None,
        verification_token: str | None,
        encrypt_key: str | None,
    ) -> ChannelConfigRow:
        """Create or update Feishu config for this owner.

        Secret fields: ``None`` means keep existing.
        Non-empty ``app_id`` is exclusive to one owner (AppIdTakenError).
        Clear ``app_id`` (empty string while saving) to release the binding
        so others may claim it later.
        """
        owner = owner_username.strip()
        if not owner:
            raise ValueError("owner_username 不能为空")

        existing = self.get_for_owner(owner, CHANNEL_FEISHU)
        now = int(time.time())
        app_id_v = app_id.strip()

        if existing is None:
            secret = (app_secret or "").strip()
            token = (verification_token or "").strip()
            enc = (encrypt_key or "").strip()
            if enabled:
                _require_feishu_ready(app_id_v, secret, token)
            self._ensure_app_id_available(app_id_v, owner)
            config_id = uuid.uuid4().hex
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO channel_configs (
                        id, owner_username, channel, enabled,
                        app_id, app_secret, verification_token, encrypt_key,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config_id,
                        owner,
                        CHANNEL_FEISHU,
                        1 if enabled else 0,
                        app_id_v,
                        secret,
                        token,
                        enc,
                        now,
                        now,
                    ),
                )
            row = self.get_by_id(config_id)
            assert row is not None
            return row

        secret = (
            existing.app_secret
            if app_secret is None
            else app_secret.strip()
        )
        token = (
            existing.verification_token
            if verification_token is None
            else verification_token.strip()
        )
        enc = (
            existing.encrypt_key if encrypt_key is None else encrypt_key.strip()
        )
        if enabled:
            _require_feishu_ready(app_id_v, secret, token)
        self._ensure_app_id_available(app_id_v, owner)

        with self._lock:
            self._conn.execute(
                """
                UPDATE channel_configs SET
                    enabled = ?,
                    app_id = ?,
                    app_secret = ?,
                    verification_token = ?,
                    encrypt_key = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    1 if enabled else 0,
                    app_id_v,
                    secret,
                    token,
                    enc,
                    now,
                    existing.id,
                ),
            )
        row = self.get_by_id(existing.id)
        assert row is not None
        return row

    def _ensure_app_id_available(self, app_id: str, owner: str) -> None:
        if not app_id:
            return
        holder = self.find_app_id_holder(app_id, exclude_owner=owner)
        if holder is not None:
            raise AppIdTakenError(app_id, holder.owner_username)


def _require_feishu_ready(app_id: str, secret: str, token: str) -> None:
    missing: list[str] = []
    if not app_id:
        missing.append("app_id")
    if not secret:
        missing.append("app_secret")
    if not token:
        missing.append("verification_token")
    if missing:
        raise ValueError(
            "启用飞书渠道前请填写：" + "、".join(missing)
        )


def _row_to_config(row: sqlite3.Row) -> ChannelConfigRow:
    return ChannelConfigRow(
        id=str(row["id"]),
        owner_username=str(row["owner_username"]),
        channel=str(row["channel"]),
        enabled=bool(row["enabled"]),
        app_id=str(row["app_id"] or ""),
        app_secret=str(row["app_secret"] or ""),
        verification_token=str(row["verification_token"] or ""),
        encrypt_key=str(row["encrypt_key"] or ""),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


@lru_cache(maxsize=4)
def get_channel_store(db_path: str | Path) -> ChannelStore:
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return ChannelStore(path)
