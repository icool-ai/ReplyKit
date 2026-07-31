"""Feishu user OAuth: authorize URL, callback code exchange, token refresh."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from src.channel_store import ChannelStore
from src.channels.feishu import http_json

log = logging.getLogger(__name__)

OAUTH_CALLBACK_PATH = "/oauth/feishu/callback"
OAUTH_SCOPES = (
    "offline_access task:task:read task:tasklist:read "
    "task:section:read contact:user:search"
)
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"

_STATE_TTL_SEC = 900
_state_lock = threading.Lock()
_oauth_states: dict[str, tuple[str, str, float]] = {}  # state → (config_id, open_id, exp)


@dataclass(frozen=True)
class FeishuUserToken:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int | None = None


def oauth_redirect_uri(public_base: str) -> str:
    base = (public_base or "").rstrip("/")
    if not base:
        raise ValueError("未配置公网地址（ASSET_BASE_URL），无法生成飞书授权回调")
    return f"{base}{OAUTH_CALLBACK_PATH}"


def create_oauth_state(config_id: str, open_id: str) -> str:
    cid = (config_id or "").strip()
    oid = (open_id or "").strip()
    if not cid or not oid:
        raise ValueError("config_id 与 open_id 不能为空")
    state = secrets.token_urlsafe(24)
    exp = time.time() + _STATE_TTL_SEC
    with _state_lock:
        _purge_states_unlocked()
        _oauth_states[state] = (cid, oid, exp)
    return state


def consume_oauth_state(state: str) -> tuple[str, str] | None:
    key = (state or "").strip()
    if not key:
        return None
    now = time.time()
    with _state_lock:
        _purge_states_unlocked(now)
        row = _oauth_states.pop(key, None)
    if row is None:
        return None
    config_id, open_id, exp = row
    if now > exp:
        return None
    return config_id, open_id


def _purge_states_unlocked(now: float | None = None) -> None:
    t = time.time() if now is None else now
    expired = [k for k, (_, _, exp) in _oauth_states.items() if t > exp]
    for k in expired:
        del _oauth_states[k]


def build_authorize_url(
    *,
    app_id: str,
    redirect_uri: str,
    state: str,
    scope: str = OAUTH_SCOPES,
) -> str:
    params = {
        "client_id": app_id.strip(),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def build_user_authorize_link(
    *,
    public_base: str,
    config_id: str,
    open_id: str,
    app_id: str,
) -> str:
    redirect = oauth_redirect_uri(public_base)
    state = create_oauth_state(config_id, open_id)
    return build_authorize_url(
        app_id=app_id,
        redirect_uri=redirect,
        state=state,
    )


def exchange_code_for_token(
    *,
    app_id: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
) -> FeishuUserToken:
    data = http_json(
        "POST",
        TOKEN_URL,
        body={
            "grant_type": "authorization_code",
            "client_id": app_id.strip(),
            "client_secret": app_secret.strip(),
            "code": code.strip(),
            "redirect_uri": redirect_uri,
        },
        timeout=20.0,
    )
    return _parse_token_response(data)


def refresh_user_access_token(
    *,
    app_id: str,
    app_secret: str,
    refresh_token: str,
) -> FeishuUserToken:
    data = http_json(
        "POST",
        TOKEN_URL,
        body={
            "grant_type": "refresh_token",
            "client_id": app_id.strip(),
            "client_secret": app_secret.strip(),
            "refresh_token": refresh_token.strip(),
        },
        timeout=20.0,
    )
    return _parse_token_response(data)


def _parse_token_response(data: dict[str, Any]) -> FeishuUserToken:
    if int(data.get("code") or 0) != 0:
        raise RuntimeError(
            f"飞书 OAuth 换票失败: {data.get('error') or data.get('msg') or data}"
        )
    access = str(data.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    if not access:
        raise RuntimeError(f"飞书 OAuth 响应缺少 access_token: {data}")
    now = int(time.time())
    expires_in = int(data.get("expires_in") or 7200)
    refresh_expires_in = data.get("refresh_token_expires_in")
    refresh_exp = (
        now + int(refresh_expires_in) if refresh_expires_in is not None else None
    )
    return FeishuUserToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + expires_in,
        refresh_expires_at=refresh_exp,
    )


def get_valid_user_access_token(
    store: ChannelStore,
    *,
    config_id: str,
    open_id: str,
    app_id: str,
    app_secret: str,
) -> str | None:
    """Return a usable user_access_token, refreshing when needed. None if re-auth required."""
    row = store.get_feishu_user_token(config_id, open_id)
    if row is None:
        return None
    now = int(time.time())
    # Refresh ~2 minutes before expiry.
    if row.expires_at > now + 120 and row.access_token:
        return row.access_token
    if not row.refresh_token:
        store.delete_feishu_user_token(config_id, open_id)
        return None
    if row.refresh_expires_at is not None and row.refresh_expires_at <= now:
        store.delete_feishu_user_token(config_id, open_id)
        return None
    try:
        fresh = refresh_user_access_token(
            app_id=app_id,
            app_secret=app_secret,
            refresh_token=row.refresh_token,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "feishu refresh token failed config=%s open_id=%s: %s",
            config_id,
            open_id,
            exc,
        )
        store.delete_feishu_user_token(config_id, open_id)
        return None
    store.upsert_feishu_user_token(
        config_id,
        open_id,
        access_token=fresh.access_token,
        refresh_token=fresh.refresh_token or row.refresh_token,
        expires_at=fresh.expires_at,
        refresh_expires_at=fresh.refresh_expires_at,
    )
    return fresh.access_token


def html_oauth_page(*, title: str, message: str, ok: bool) -> str:
    color = "#0f766e" if ok else "#b91c1c"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #f8fafc;
           color: #0f172a; display: flex; min-height: 100vh;
           align-items: center; justify-content: center; margin: 0; }}
    .box {{ max-width: 28rem; padding: 1.5rem 1.75rem; background: #fff;
            border-radius: 12px; box-shadow: 0 8px 24px rgba(15,23,42,.08); }}
    h1 {{ font-size: 1.25rem; margin: 0 0 .75rem; color: {color}; }}
    p {{ margin: 0; line-height: 1.6; color: #334155; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>
"""
