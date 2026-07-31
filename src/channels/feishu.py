"""飞书自建应用事件渠道：验签/解密 + 异步回复 API。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from base64 import b64decode
from typing import Any

from Crypto.Cipher import AES

from src.channel_store import ChannelConfigRow

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"@_user_\d+")
SESSION_PREFIX = "feishu:"

_seen_message_ids: dict[str, float] = {}
_seen_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


class FeishuCryptoError(Exception):
    """验签或解密失败。"""


class AESCipher:
    """飞书 Encrypt Key 解密（官方 AES-256-CBC）。"""

    def __init__(self, key: str) -> None:
        self.key = hashlib.sha256(key.encode("utf-8")).digest()

    @staticmethod
    def _unpad(s: bytes) -> bytes:
        return s[: -ord(s[len(s) - 1 :])]

    def decrypt_string(self, enc: str) -> str:
        raw = b64decode(enc)
        iv, data = raw[: AES.block_size], raw[AES.block_size :]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return self._unpad(cipher.decrypt(data)).decode("utf-8")


def feishu_ready(row: ChannelConfigRow) -> bool:
    return bool(
        row.enabled
        and row.app_id.strip()
        and row.app_secret.strip()
        and row.verification_token.strip()
    )


def feishu_can_reply(row: ChannelConfigRow) -> bool:
    """能否调用飞书发消息 API（不要求已启用）。"""
    return bool(row.app_id.strip() and row.app_secret.strip())


def feishu_setup_hint(row: ChannelConfigRow) -> str:
    """给终端用户的配置引导文案（发到飞书会话）。"""
    if not row.enabled:
        return (
            "客服机器人尚未启用。"
            "请管理员登录 ReplyKit →「渠道配置」→ 打开飞书「启用」并保存。"
        )
    missing: list[str] = []
    if not row.app_secret.strip():
        missing.append("App Secret")
    if not row.verification_token.strip():
        missing.append("Verification Token")
    if missing:
        return (
            f"飞书渠道配置不完整（缺少{'、'.join(missing)}）。"
            "请管理员在 ReplyKit「渠道配置」中补全后保存。"
        )
    return (
        "飞书渠道暂不可用，请管理员检查 ReplyKit「渠道配置」是否已正确保存。"
    )


def session_id_for(config_id: str, open_id: str) -> str:
    return f"{SESSION_PREFIX}{config_id}:{open_id.strip()}"


def verify_signature(
    encrypt_key: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
) -> bool:
    if not encrypt_key:
        return True
    expected = hashlib.sha256(
        (timestamp + nonce + encrypt_key).encode("utf-8") + body
    ).hexdigest()
    return expected == signature


def parse_event_body(raw: bytes, encrypt_key: str) -> dict[str, Any]:
    payload = json.loads(raw.decode("utf-8"))
    if "encrypt" in payload:
        if not encrypt_key:
            raise FeishuCryptoError("收到加密事件但未配置 encrypt_key")
        try:
            plain = AESCipher(encrypt_key).decrypt_string(payload["encrypt"])
            return json.loads(plain)
        except Exception as exc:  # noqa: BLE001
            raise FeishuCryptoError("事件解密失败") from exc
    return payload


def extract_text(content: str) -> str:
    try:
        obj = json.loads(content)
        text = str(obj.get("text") or "")
    except json.JSONDecodeError:
        text = content
    return _MENTION_RE.sub("", text).strip()


def format_bot_answer(
    answer: str,
    *,
    clarify_options: list[str] | None = None,
    images: list[str] | None = None,
) -> str:
    parts = [(answer or "").strip()]
    options = [o.strip() for o in (clarify_options or []) if o and o.strip()]
    if options:
        parts.append("你可以回复序号或点选以下问题继续：")
        for i, opt in enumerate(options, 1):
            parts.append(f"{i}. {opt}")
    imgs = [u for u in (images or []) if u]
    if imgs:
        parts.append("相关图片：")
        parts.extend(imgs)
    return "\n".join(p for p in parts if p).strip() or "（空回复）"


def remember_message(dedupe_key: str) -> bool:
    """True = 首次，应处理。"""
    now = time.time()
    with _seen_lock:
        expired = [k for k, t in _seen_message_ids.items() if now - t > 600]
        for k in expired:
            del _seen_message_ids[k]
        if dedupe_key in _seen_message_ids:
            return False
        _seen_message_ids[dedupe_key] = now
        return True


def http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail}") from exc
    if not raw:
        return {}
    return json.loads(raw)


def verify_app_credentials(app_id: str, app_secret: str) -> None:
    """调用飞书获取 tenant_access_token；失败抛 ValueError。"""
    aid = app_id.strip()
    secret = app_secret.strip()
    if not aid or not secret:
        raise ValueError("App ID 与 App Secret 不能为空")
    try:
        data = http_json(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": aid, "app_secret": secret},
            timeout=15.0,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法校验飞书凭证：{exc}") from exc
    if data.get("code") != 0:
        raise ValueError(
            f"飞书 App ID/Secret 无效：{data.get('msg') or data}"
        )


def tenant_access_token(app_id: str, app_secret: str) -> str:
    cache_key = f"{app_id}:{app_secret}"
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]
    data = http_json(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    token = str(data["tenant_access_token"])
    expires = time.time() + int(data.get("expire", 7200)) - 120
    with _token_lock:
        _token_cache[cache_key] = (token, expires)
    return token


def reply_text(
    *,
    app_id: str,
    app_secret: str,
    message_id: str,
    text: str,
) -> None:
    token = tenant_access_token(app_id, app_secret)
    data = http_json(
        "POST",
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
        body={
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"飞书回复失败: {data}")
