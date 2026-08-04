"""Per-owner channel connector config (Feishu first; WeCom later).

Each logged-in user owns at most one row per channel. A non-empty Feishu
``app_id`` may be bound to only one owner; others get AppIdTakenError.
Webhook routing: fixed ``POST /webhooks/feishu``; resolve config by
App ID / Verification Token (``id`` remains an internal primary key).
"""

from __future__ import annotations

import secrets
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
SETTING_DIFY_RETRIEVAL_API_KEY = "dify_retrieval_api_key"  # legacy single-key


@dataclass(frozen=True)
class DifyApiKeyRow:
    id: str
    name: str
    endpoint: str
    knowledge_id: str
    api_key: str
    created_at: int
    updated_at: int
    last_used_at: int | None

    def to_public_dict(self, *, api_key_plaintext: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "knowledge_id": self.knowledge_id,
            "api_key_masked": _mask_api_key(self.api_key),
            "api_key_set": True,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
        }
        if api_key_plaintext is not None:
            out["api_key"] = api_key_plaintext
        return out


def _mask_api_key(api_key: str) -> str:
    key = api_key.strip()
    if len(key) <= 12:
        return "*" * len(key) if key else ""
    return f"{key[:10]}{'*' * max(8, len(key) - 14)}{key[-4:]}"


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


@dataclass(frozen=True)
class FeishuUserTokenRow:
    config_id: str
    open_id: str
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int | None
    updated_at: int


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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_user_tokens (
                    config_id TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    access_token TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    refresh_expires_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (config_id, open_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dify_api_keys (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL,
                    api_key TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_used_at INTEGER
                )
                """
            )
            self._migrate_legacy_dify_key()

    def _migrate_legacy_dify_key(self) -> None:
        """One-shot: move platform_settings single key into dify_api_keys."""
        row = self._conn.execute(
            "SELECT value, updated_at FROM platform_settings WHERE key = ?",
            (SETTING_DIFY_RETRIEVAL_API_KEY,),
        ).fetchone()
        if row is None:
            return
        legacy = str(row["value"] or "").strip()
        if not legacy:
            self._conn.execute(
                "DELETE FROM platform_settings WHERE key = ?",
                (SETTING_DIFY_RETRIEVAL_API_KEY,),
            )
            return
        exists = self._conn.execute(
            "SELECT 1 FROM dify_api_keys WHERE api_key = ? LIMIT 1",
            (legacy,),
        ).fetchone()
        if exists is None:
            now = int(row["updated_at"] or time.time())
            self._conn.execute(
                """
                INSERT INTO dify_api_keys
                (id, name, endpoint, knowledge_id, api_key,
                 created_at, updated_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    uuid.uuid4().hex,
                    "迁移自旧配置",
                    "",
                    "faq",
                    legacy,
                    now,
                    now,
                ),
            )
        self._conn.execute(
            "DELETE FROM platform_settings WHERE key = ?",
            (SETTING_DIFY_RETRIEVAL_API_KEY,),
        )

    def get_setting(self, key: str) -> tuple[str, int] | None:
        """Return ``(value, updated_at)`` or None if missing / empty key."""
        k = key.strip()
        if not k:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT value, updated_at FROM platform_settings WHERE key = ?",
                (k,),
            ).fetchone()
        if row is None:
            return None
        return str(row["value"] or ""), int(row["updated_at"] or 0)

    def set_setting(self, key: str, value: str) -> int:
        """Upsert setting; returns updated_at."""
        k = key.strip()
        if not k:
            raise ValueError("setting key 不能为空")
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO platform_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (k, value, now),
            )
        return now

    def delete_setting(self, key: str) -> bool:
        k = key.strip()
        if not k:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM platform_settings WHERE key = ?",
                (k,),
            )
            return cur.rowcount > 0

    def get_dify_retrieval_api_key(self) -> str:
        """Deprecated single-key helper; prefer ``find_dify_api_key``."""
        rows = self.list_dify_api_keys()
        return rows[0].api_key if rows else ""

    def list_dify_api_keys(self) -> list[DifyApiKeyRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM dify_api_keys
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [_row_to_dify_key(r) for r in rows]

    def get_dify_api_key(self, key_id: str) -> DifyApiKeyRow | None:
        kid = key_id.strip()
        if not kid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dify_api_keys WHERE id = ?",
                (kid,),
            ).fetchone()
        return _row_to_dify_key(row) if row else None

    def find_dify_api_key_by_secret(self, api_key: str) -> DifyApiKeyRow | None:
        provided = api_key.strip()
        if not provided:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dify_api_keys"
            ).fetchall()
        for row in rows:
            stored = str(row["api_key"] or "")
            if len(stored) == len(provided) and secrets.compare_digest(
                stored, provided
            ):
                return _row_to_dify_key(row)
        return None

    def create_dify_api_key(
        self,
        *,
        name: str,
        endpoint: str,
        knowledge_id: str,
    ) -> tuple[str, DifyApiKeyRow]:
        n = name.strip()
        ep = endpoint.strip().rstrip("/")
        kid = knowledge_id.strip() or "faq"
        if not n:
            raise ValueError("名称不能为空")
        if not ep:
            raise ValueError("API Endpoint 不能为空")
        low = ep.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("API Endpoint 须以 http:// 或 https:// 开头")
        if low.rstrip("/").endswith("/retrieval"):
            raise ValueError("API Endpoint 不要带 /retrieval 后缀")
        plaintext = f"rk_dify_{secrets.token_urlsafe(32)}"
        now = int(time.time())
        row_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dify_api_keys
                (id, name, endpoint, knowledge_id, api_key,
                 created_at, updated_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (row_id, n, ep, kid, plaintext, now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM dify_api_keys WHERE id = ?",
                (row_id,),
            ).fetchone()
        assert row is not None
        return plaintext, _row_to_dify_key(row)

    def update_dify_api_key(
        self,
        key_id: str,
        *,
        name: str | None = None,
        endpoint: str | None = None,
        knowledge_id: str | None = None,
    ) -> DifyApiKeyRow:
        kid = key_id.strip()
        if not kid:
            raise KeyError("配置不存在")
        with self._lock:
            existing_row = self._conn.execute(
                "SELECT * FROM dify_api_keys WHERE id = ?",
                (kid,),
            ).fetchone()
            if existing_row is None:
                raise KeyError("配置不存在")
            existing = _row_to_dify_key(existing_row)
            new_name = existing.name if name is None else name.strip()
            new_ep = (
                existing.endpoint
                if endpoint is None
                else endpoint.strip().rstrip("/")
            )
            new_kid = (
                existing.knowledge_id
                if knowledge_id is None
                else (knowledge_id.strip() or existing.knowledge_id)
            )
            if not new_name:
                raise ValueError("名称不能为空")
            if not new_ep:
                raise ValueError("API Endpoint 不能为空")
            low = new_ep.lower()
            if not (low.startswith("http://") or low.startswith("https://")):
                raise ValueError("API Endpoint 须以 http:// 或 https:// 开头")
            if low.rstrip("/").endswith("/retrieval"):
                raise ValueError("API Endpoint 不要带 /retrieval 后缀")
            now = int(time.time())
            self._conn.execute(
                """
                UPDATE dify_api_keys
                SET name = ?, endpoint = ?, knowledge_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_name, new_ep, new_kid, now, existing.id),
            )
            row = self._conn.execute(
                "SELECT * FROM dify_api_keys WHERE id = ?",
                (existing.id,),
            ).fetchone()
        assert row is not None
        return _row_to_dify_key(row)

    def delete_dify_api_key(self, key_id: str) -> bool:
        kid = key_id.strip()
        if not kid:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM dify_api_keys WHERE id = ?",
                (kid,),
            )
            return cur.rowcount > 0

    def touch_dify_api_key_used(self, key_id: str) -> None:
        kid = key_id.strip()
        if not kid:
            return
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                UPDATE dify_api_keys
                SET last_used_at = ?
                WHERE id = ?
                """,
                (now, kid),
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

    def get_feishu_user_token(
        self, config_id: str, open_id: str
    ) -> FeishuUserTokenRow | None:
        cid = config_id.strip()
        oid = open_id.strip()
        if not cid or not oid:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM feishu_user_tokens
                WHERE config_id = ? AND open_id = ?
                """,
                (cid, oid),
            ).fetchone()
        return _row_to_user_token(row) if row else None

    def upsert_feishu_user_token(
        self,
        config_id: str,
        open_id: str,
        *,
        access_token: str,
        refresh_token: str,
        expires_at: int,
        refresh_expires_at: int | None = None,
    ) -> FeishuUserTokenRow:
        cid = config_id.strip()
        oid = open_id.strip()
        if not cid or not oid:
            raise ValueError("config_id 与 open_id 不能为空")
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feishu_user_tokens (
                    config_id, open_id, access_token, refresh_token,
                    expires_at, refresh_expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_id, open_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    refresh_expires_at = excluded.refresh_expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    cid,
                    oid,
                    access_token.strip(),
                    refresh_token.strip(),
                    int(expires_at),
                    (
                        int(refresh_expires_at)
                        if refresh_expires_at is not None
                        else None
                    ),
                    now,
                ),
            )
        row = self.get_feishu_user_token(cid, oid)
        assert row is not None
        return row

    def delete_feishu_user_token(self, config_id: str, open_id: str) -> None:
        cid = config_id.strip()
        oid = open_id.strip()
        if not cid or not oid:
            return
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM feishu_user_tokens
                WHERE config_id = ? AND open_id = ?
                """,
                (cid, oid),
            )


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


def _row_to_dify_key(row: sqlite3.Row) -> DifyApiKeyRow:
    last_used = row["last_used_at"]
    return DifyApiKeyRow(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        endpoint=str(row["endpoint"] or ""),
        knowledge_id=str(row["knowledge_id"] or ""),
        api_key=str(row["api_key"] or ""),
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        last_used_at=int(last_used) if last_used is not None else None,
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


def _row_to_user_token(row: sqlite3.Row) -> FeishuUserTokenRow:
    refresh_exp = row["refresh_expires_at"]
    return FeishuUserTokenRow(
        config_id=str(row["config_id"]),
        open_id=str(row["open_id"]),
        access_token=str(row["access_token"] or ""),
        refresh_token=str(row["refresh_token"] or ""),
        expires_at=int(row["expires_at"] or 0),
        refresh_expires_at=int(refresh_exp) if refresh_exp is not None else None,
        updated_at=int(row["updated_at"] or 0),
    )


@lru_cache(maxsize=4)
def get_channel_store(db_path: str | Path) -> ChannelStore:
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return ChannelStore(path)
