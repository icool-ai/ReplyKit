"""Channel configuration + Feishu user tokens + Dify API keys + settings (SQLAlchemy)."""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, and_, select
from sqlalchemy.orm import Session

import time as _time

from mp_agent.dao._helpers import dt_to_unix, utc_now
from mp_agent.dao._engine_normalize import normalize_store_engine
from mp_agent.dao.models import (
    ChannelConfig,
    DifyApiKey,
    FeishuUserToken,
    PlatformSetting,
)
from mp_agent.dao.redis_client import (
    feishu_user_access_cache_invalidate,
    feishu_user_access_cache_put,
)
from mp_agent.dao.sync_db import sync_engine
from src.secrets_crypto import (
    SecretsCryptoError,
    decrypt_secret,
    encrypt_secret,
    hash_api_key,
    is_api_key_hash,
    is_encrypted_secret,
    mask_api_key_prefix,
    stored_api_key_prefix,
    verify_api_key,
)

CHANNEL_FEISHU = "feishu"
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppIdTakenError(ValueError):
    app_id: str
    taken_by: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"AppId 已被其他账号占用: {self.app_id} ({self.taken_by})"


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
        return {
            "id": self.id,
            "owner_username": self.owner_username,
            "channel": self.channel,
            "enabled": self.enabled,
            "app_id": self.app_id,
            "app_secret_set": bool(self.app_secret.strip()),
            "verification_token_set": bool(self.verification_token.strip()),
            "encrypt_key_set": bool(self.encrypt_key.strip()),
            "callback_path": "/webhooks/feishu",
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


@dataclass(frozen=True)
class DifyApiKeyRow:
    id: str
    name: str
    endpoint: str
    knowledge_id: str
    api_key_prefix: str
    created_at: int
    updated_at: int
    last_used_at: int | None

    def to_public_dict(
        self, *, api_key_plaintext: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "knowledge_id": self.knowledge_id,
            "api_key_masked": mask_api_key_prefix(self.api_key_prefix),
            "api_key_set": bool(self.api_key_prefix),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "api_key": api_key_plaintext,
        }


class ChannelStore:
    """SQLAlchemy-backed store for channel configs and Dify API keys."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = normalize_store_engine(engine)
        self.migrate_secrets_at_rest()

    def migrate_secrets_at_rest(self) -> None:
        """Upgrade legacy plaintext Dify keys / channel secrets / OAuth tokens in place."""
        try:
            self._migrate_dify_api_keys()
            self._migrate_channel_secrets()
            self._migrate_feishu_user_tokens()
        except SecretsCryptoError as exc:
            _logger.error("落库密钥迁移失败: %s", exc)
            raise

    def _migrate_dify_api_keys(self) -> None:
        with Session(self._engine) as db:
            rows = db.execute(select(DifyApiKey)).scalars().all()
            changed = 0
            for row in rows:
                stored = str(row.api_key or "")
                if not stored or is_api_key_hash(stored):
                    continue
                row.api_key = hash_api_key(stored)
                row.updated_at = utc_now()
                changed += 1
            if changed:
                db.commit()
                _logger.info("已将 %s 条 Dify API Key 升级为哈希存储", changed)

    def _migrate_channel_secrets(self) -> None:
        fields = ("app_secret", "verification_token", "encrypt_key")
        with Session(self._engine) as db:
            rows = db.execute(select(ChannelConfig)).scalars().all()
            changed = 0
            for row in rows:
                dirty = False
                for name in fields:
                    value = str(getattr(row, name) or "")
                    if not value or is_encrypted_secret(value):
                        continue
                    setattr(row, name, encrypt_secret(value))
                    dirty = True
                if dirty:
                    row.updated_at = utc_now()
                    changed += 1
            if changed:
                db.commit()
                _logger.info("已将 %s 条渠道密钥升级为 AES-GCM 密文", changed)

    def _migrate_feishu_user_tokens(self) -> None:
        with Session(self._engine) as db:
            rows = db.execute(select(FeishuUserToken)).scalars().all()
            changed = 0
            for row in rows:
                dirty = False
                for name in ("access_token", "refresh_token"):
                    value = str(getattr(row, name) or "")
                    if not value or is_encrypted_secret(value):
                        continue
                    setattr(row, name, encrypt_secret(value))
                    dirty = True
                if dirty:
                    row.updated_at = utc_now()
                    changed += 1
            if changed:
                db.commit()
                _logger.info("已将 %s 条飞书 OAuth token 升级为 AES-GCM 密文", changed)

    def get_setting(self, key: str) -> str:
        k = key.strip()
        if not k:
            return ""
        with Session(self._engine) as db:
            row = db.get(PlatformSetting, k)
            return str(row.value) if row and row.value is not None else ""

    def set_setting(self, key: str, value: str) -> None:
        k = key.strip()
        if not k:
            return
        now = utc_now()
        with Session(self._engine) as db:
            row = db.get(PlatformSetting, k)
            if row is None:
                db.add(PlatformSetting(key=k, value=str(value or ""), updated_at=now))
            else:
                row.value = str(value or "")
                row.updated_at = now
            db.commit()

    def list_dify_api_keys(self) -> list[DifyApiKeyRow]:
        with Session(self._engine) as db:
            rows = db.execute(
                select(DifyApiKey).order_by(DifyApiKey.created_at.desc(), DifyApiKey.id)
            ).scalars().all()
            return [_model_to_dify_key(r) for r in rows]

    def get_dify_api_key(self, key_id: str) -> DifyApiKeyRow | None:
        kid = key_id.strip()
        if not kid:
            return None
        with Session(self._engine) as db:
            row = db.get(DifyApiKey, kid)
            return _model_to_dify_key(row) if row else None

    def find_dify_api_key_by_secret(self, api_key: str) -> DifyApiKeyRow | None:
        provided = api_key.strip()
        if not provided:
            return None
        with Session(self._engine) as db:
            rows = db.execute(select(DifyApiKey)).scalars().all()
            for row in rows:
                stored = str(row.api_key or "")
                if not verify_api_key(stored, provided):
                    continue
                if not is_api_key_hash(stored):
                    row.api_key = hash_api_key(provided)
                    row.updated_at = utc_now()
                    db.commit()
                return _model_to_dify_key(row)
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
        row_id = uuid.uuid4().hex
        now = utc_now()
        with Session(self._engine) as db:
            db.add(
                DifyApiKey(
                    id=row_id,
                    name=n,
                    endpoint=ep,
                    knowledge_id=kid,
                    api_key=hash_api_key(plaintext),
                    created_at=now,
                    updated_at=now,
                    last_used_at=None,
                )
            )
            db.commit()
            created = db.get(DifyApiKey, row_id)
            assert created is not None
            return plaintext, _model_to_dify_key(created)

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
        with Session(self._engine) as db:
            existing = db.get(DifyApiKey, kid)
            if existing is None:
                raise KeyError("配置不存在")
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
            existing.name = new_name
            existing.endpoint = new_ep
            existing.knowledge_id = new_kid
            existing.updated_at = utc_now()
            db.commit()
            return _model_to_dify_key(existing)

    def delete_dify_api_key(self, key_id: str) -> bool:
        kid = key_id.strip()
        if not kid:
            return False
        with Session(self._engine) as db:
            row = db.get(DifyApiKey, kid)
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True

    def touch_dify_api_key_used(self, key_id: str) -> None:
        kid = key_id.strip()
        if not kid:
            return
        now = utc_now()
        with Session(self._engine) as db:
            row = db.get(DifyApiKey, kid)
            if row is not None:
                row.last_used_at = now
                db.commit()

    def close(self) -> None:
        return None

    def get_for_owner(
        self, owner_username: str, channel: str = CHANNEL_FEISHU
    ) -> ChannelConfigRow | None:
        owner = owner_username.strip()
        if not owner:
            return None
        with Session(self._engine) as db:
            row = db.scalar(
                select(ChannelConfig).where(
                    and_(
                        ChannelConfig.owner_username == owner,
                        ChannelConfig.channel == channel,
                    )
                )
            )
            return _model_to_config(row) if row else None

    def get_by_id(self, config_id: str) -> ChannelConfigRow | None:
        cid = config_id.strip()
        if not cid:
            return None
        with Session(self._engine) as db:
            row = db.get(ChannelConfig, cid)
            return _model_to_config(row) if row else None

    def find_by_app_id(
        self, app_id: str, *, channel: str = CHANNEL_FEISHU
    ) -> ChannelConfigRow | None:
        aid = app_id.strip()
        if not aid:
            return None
        with Session(self._engine) as db:
            row = db.scalar(
                select(ChannelConfig).where(
                    and_(ChannelConfig.channel == channel, ChannelConfig.app_id == aid)
                )
            )
            return _model_to_config(row) if row else None

    def find_by_verification_token(
        self, token: str, *, channel: str = CHANNEL_FEISHU
    ) -> ChannelConfigRow | None:
        tok = token.strip()
        if not tok:
            return None
        with Session(self._engine) as db:
            rows = db.execute(
                select(ChannelConfig).where(ChannelConfig.channel == channel)
            ).scalars().all()
            for row in rows:
                plain = decrypt_secret(str(row.verification_token or ""))
                if not plain:
                    continue
                if len(plain) == len(tok) and secrets.compare_digest(plain, tok):
                    return _model_to_config(row)
        return None

    def list_enabled_feishu(self) -> list[ChannelConfigRow]:
        with Session(self._engine) as db:
            rows = db.execute(
                select(ChannelConfig).where(
                    and_(
                        ChannelConfig.channel == CHANNEL_FEISHU,
                        ChannelConfig.enabled.is_(True),
                    )
                )
            ).scalars().all()
            return [_model_to_config(r) for r in rows]

    def list_feishu(self) -> list[ChannelConfigRow]:
        with Session(self._engine) as db:
            rows = db.execute(
                select(ChannelConfig).where(ChannelConfig.channel == CHANNEL_FEISHU)
            ).scalars().all()
            return [_model_to_config(r) for r in rows]

    def find_app_id_holder(
        self,
        app_id: str,
        *,
        channel: str = CHANNEL_FEISHU,
        exclude_owner: str | None = None,
    ) -> ChannelConfigRow | None:
        aid = app_id.strip()
        if not aid:
            return None
        with Session(self._engine) as db:
            stmt = select(ChannelConfig).where(
                and_(ChannelConfig.channel == channel, ChannelConfig.app_id == aid)
            )
            if exclude_owner:
                stmt = stmt.where(ChannelConfig.owner_username != exclude_owner.strip())
            row = db.scalar(stmt)
            return _model_to_config(row) if row else None

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
        owner = owner_username.strip()
        if not owner:
            raise ValueError("owner_username 不能为空")

        existing = self.get_for_owner(owner, CHANNEL_FEISHU)
        now = utc_now()
        app_id_v = app_id.strip()

        if existing is None:
            secret = (app_secret or "").strip()
            token = (verification_token or "").strip()
            enc = (encrypt_key or "").strip()
            if enabled:
                _require_feishu_ready(app_id_v, secret, token)
            self._ensure_app_id_available(app_id_v, owner)
            config_id = uuid.uuid4().hex
            with Session(self._engine) as db:
                db.add(
                    ChannelConfig(
                        id=config_id,
                        owner_username=owner,
                        channel=CHANNEL_FEISHU,
                        enabled=bool(enabled),
                        app_id=app_id_v,
                        app_secret=encrypt_secret(secret),
                        verification_token=encrypt_secret(token),
                        encrypt_key=encrypt_secret(enc),
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.commit()
                row = db.get(ChannelConfig, config_id)
                assert row is not None
                return _model_to_config(row)

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

        with Session(self._engine) as db:
            row = db.get(ChannelConfig, existing.id)
            if row is None:
                raise KeyError("配置不存在")
            row.enabled = bool(enabled)
            row.app_id = app_id_v
            row.app_secret = encrypt_secret(secret)
            row.verification_token = encrypt_secret(token)
            row.encrypt_key = encrypt_secret(enc)
            row.updated_at = now
            db.commit()
            return _model_to_config(row)

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
        with Session(self._engine) as db:
            row = db.scalar(
                select(FeishuUserToken).where(
                    and_(
                        FeishuUserToken.config_id == cid,
                        FeishuUserToken.open_id == oid,
                    )
                )
            )
            return _model_to_user_token(row) if row else None

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
        now = utc_now()
        access_plain = access_token.strip()
        refresh_plain = refresh_token.strip()
        with Session(self._engine) as db:
            row = db.scalar(
                select(FeishuUserToken).where(
                    and_(
                        FeishuUserToken.config_id == cid,
                        FeishuUserToken.open_id == oid,
                    )
                )
            )
            if row is None:
                row = FeishuUserToken(
                    config_id=cid,
                    open_id=oid,
                    access_token=encrypt_secret(access_plain),
                    refresh_token=encrypt_secret(refresh_plain),
                    expires_at=int(expires_at),
                    refresh_expires_at=(
                        int(refresh_expires_at)
                        if refresh_expires_at is not None
                        else None
                    ),
                    updated_at=now,
                )
                db.add(row)
            else:
                row.access_token = encrypt_secret(access_plain)
                row.refresh_token = encrypt_secret(refresh_plain)
                row.expires_at = int(expires_at)
                row.refresh_expires_at = (
                    int(refresh_expires_at)
                    if refresh_expires_at is not None
                    else None
                )
                row.updated_at = now
            db.commit()
            result = _model_to_user_token(row)
        # 写入 DB 后同步写 Redis 读穿透缓存（TTL = expires_at - now - 120s，提前 2min 自然过期）
        ttl_left = int(result.expires_at) - int(_time.time()) - 120
        if ttl_left > 0 and result.access_token:
            feishu_user_access_cache_put(cid, oid, result.access_token, ttl_left)
        return result

    def delete_feishu_user_token(self, config_id: str, open_id: str) -> None:
        cid = config_id.strip()
        oid = open_id.strip()
        if not cid or not oid:
            return
        with Session(self._engine) as db:
            row = db.scalar(
                select(FeishuUserToken).where(
                    and_(
                        FeishuUserToken.config_id == cid,
                        FeishuUserToken.open_id == oid,
                    )
                )
            )
            if row is not None:
                db.delete(row)
                db.commit()
        # DB 删除后同步失效 Redis 读穿透缓存
        feishu_user_access_cache_invalidate(cid, oid)


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


def _model_to_dify_key(m: DifyApiKey) -> DifyApiKeyRow:
    last_used = dt_to_unix(m.last_used_at) if m.last_used_at else None
    stored = str(m.api_key or "")
    return DifyApiKeyRow(
        id=str(m.id),
        name=str(m.name or ""),
        endpoint=str(m.endpoint or ""),
        knowledge_id=str(m.knowledge_id or ""),
        api_key_prefix=stored_api_key_prefix(stored),
        created_at=dt_to_unix(m.created_at),
        updated_at=dt_to_unix(m.updated_at),
        last_used_at=last_used,
    )


def _model_to_config(m: ChannelConfig) -> ChannelConfigRow:
    return ChannelConfigRow(
        id=str(m.id),
        owner_username=str(m.owner_username),
        channel=str(m.channel),
        enabled=bool(m.enabled),
        app_id=str(m.app_id or ""),
        app_secret=decrypt_secret(str(m.app_secret or "")),
        verification_token=decrypt_secret(str(m.verification_token or "")),
        encrypt_key=decrypt_secret(str(m.encrypt_key or "")),
        created_at=dt_to_unix(m.created_at),
        updated_at=dt_to_unix(m.updated_at),
    )


def _model_to_user_token(m: FeishuUserToken) -> FeishuUserTokenRow:
    return FeishuUserTokenRow(
        config_id=str(m.config_id),
        open_id=str(m.open_id),
        access_token=decrypt_secret(str(m.access_token or "")),
        refresh_token=decrypt_secret(str(m.refresh_token or "")),
        expires_at=int(m.expires_at or 0),
        refresh_expires_at=(
            int(m.refresh_expires_at) if m.refresh_expires_at is not None else None
        ),
        updated_at=dt_to_unix(m.updated_at),
    )


@lru_cache(maxsize=4)
def get_channel_store(engine_identity: str = "default") -> ChannelStore:
    """Shared store (identity string for cache key only)."""
    _ = engine_identity
    return ChannelStore(sync_engine)
