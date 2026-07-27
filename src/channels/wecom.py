"""企业微信自建应用消息渠道：文本收发 + session 映射。"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from src.channels.wecom_crypto import WeComCrypt, WeComCryptoError
from src.config import Settings


@dataclass(frozen=True)
class WeComIncoming:
    msg_type: str
    from_user: str
    to_user: str
    content: str
    agent_id: str


def wecom_configured(settings: Settings) -> bool:
    return bool(
        settings.wecom_enabled
        and settings.wecom_corp_id
        and settings.wecom_token
        and settings.wecom_aes_key
    )


def build_crypt(settings: Settings) -> WeComCrypt:
    return WeComCrypt(
        token=settings.wecom_token,
        encoding_aes_key=settings.wecom_aes_key,
        receive_id=settings.wecom_corp_id,
    )


def session_id_for_user(settings: Settings, userid: str) -> str:
    prefix = settings.wecom_session_prefix or "ww:"
    return f"{prefix}{userid.strip()}"


def parse_incoming_xml(xml_text: str) -> WeComIncoming:
    root = ET.fromstring(xml_text)
    def _text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    return WeComIncoming(
        msg_type=_text("MsgType").lower(),
        from_user=_text("FromUserName"),
        to_user=_text("ToUserName"),
        content=_text("Content"),
        agent_id=_text("AgentID"),
    )


def build_text_reply_xml(*, to_user: str, from_user: str, content: str) -> str:
    # 企微被动回复：ToUserName = 成员 UserId，FromUserName = CorpId
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )


def format_bot_answer(
    answer: str,
    *,
    clarify_options: list[str] | None = None,
    images: list[str] | None = None,
) -> str:
    parts = [(answer or "").strip()]
    options = [o.strip() for o in (clarify_options or []) if o and o.strip()]
    if options:
        lines = "\n".join(f"{i}. {o}" for i, o in enumerate(options, start=1))
        parts.append("您可能想问：\n" + lines)
    image_urls = [u.strip() for u in (images or []) if u and u.strip()]
    if image_urls:
        parts.append("相关图片：\n" + "\n".join(image_urls))
    text = "\n\n".join(p for p in parts if p)
    # 企微文本消息建议不超过约 2048 字节，这里按字符粗截断
    if len(text) > 1800:
        text = text[:1790] + "…"
    return text or "（空回复）"


def verify_url_echo(
    settings: Settings,
    *,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    crypt = build_crypt(settings)
    return crypt.verify_url(msg_signature, timestamp, nonce, echostr)


def decrypt_post(
    settings: Settings,
    *,
    body: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> WeComIncoming:
    crypt = build_crypt(settings)
    plain_xml = crypt.decrypt_message(body, msg_signature, timestamp, nonce)
    return parse_incoming_xml(plain_xml)


def encrypt_text_reply(
    settings: Settings,
    *,
    to_user: str,
    content: str,
    nonce: str,
    timestamp: str | None = None,
) -> str:
    crypt = build_crypt(settings)
    reply_xml = build_text_reply_xml(
        to_user=to_user,
        from_user=settings.wecom_corp_id,
        content=content,
    )
    return crypt.encrypt_message(reply_xml, nonce=nonce, timestamp=timestamp)


__all__ = [
    "WeComCryptoError",
    "WeComIncoming",
    "wecom_configured",
    "session_id_for_user",
    "format_bot_answer",
    "verify_url_echo",
    "decrypt_post",
    "encrypt_text_reply",
]
