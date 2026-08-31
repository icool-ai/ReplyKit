"""Channel configuration + Feishu user tokens + Dify API keys + settings (SQLAlchemy)."""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, and_, func, select
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, utc_now
from mp_agent.dao.models import (
    ChannelConfig,
    DifyApiKey,
    FeishuUserToken,
    PlatformSetting,
)
from mp_agent.dao.sync_db import sync_engine
from src.config import PROJECT_ROOT

CHANNEL_FEISHU = "feishu"


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
    api_key: str
    created_at: int
    updated_at: int
    last_used_at: int | None


class ChannelStore:
    """SQLAlchemy-backed store for channel configs and Dify API keys."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or sync_engine

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
            if len(stored) == len(provided) and secrets.compare_digest(
                stored, provided
            ):
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
                    api_key=plaintext,
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
            row = db.scalar(
                select(ChannelConfig).where(
                    and_(
                        ChannelConfig.channel == channel,
                        ChannelConfig.verification_token == tok,
                    )
                )
            )
            return _model_to_config(row) if row else None

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
                        app_secret=secret,
                        verification_token=token,
                        encrypt_key=enc,
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
            row.app_secret = secret
            row.verification_token = token
            row.encrypt_key = enc
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
                    access_token=access_token.strip(),
                    refresh_token=refresh_token.strip(),
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
                row.access_token = access_token.strip()
                row.refresh_token = refresh_token.strip()
                row.expires_at = int(expires_at)
                row.refresh_expires_at = (
                    int(refresh_expires_at)
                    if refresh_expires_at is not None
                    else None
                )
                row.updated_at = now
            db.commit()
            return _model_to_user_token(row)

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
    return DifyApiKeyRow(
        id=str(m.id),
        name=str(m.name or ""),
        endpoint=str(m.endpoint or ""),
        knowledge_id=str(m.knowledge_id or ""),
        api_key=str(m.api_key or ""),
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
        app_secret=str(m.app_secret or ""),
        verification_token=str(m.verification_token or ""),
        encrypt_key=str(m.encrypt_key or ""),
        created_at=dt_to_unix(m.created_at),
        updated_at=dt_to_unix(m.updated_at),
    )


def _model_to_user_token(m: FeishuUserToken) -> FeishuUserTokenRow:
    return FeishuUserTokenRow(
        config_id=str(m.config_id),
        open_id=str(m.open_id),
        access_token=str(m.access_token or ""),
        refresh_token=str(m.refresh_token or ""),
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
