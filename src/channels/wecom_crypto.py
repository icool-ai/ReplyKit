"""企业微信回调消息加解密（官方 AES-256-CBC + SHA1 签名协议）。

参考：https://developer.work.weixin.qq.com/document/path/96211
"""

from __future__ import annotations

import base64
import hashlib
import logging
import random
import socket
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

OK = 0
ERR_SIGNATURE = -40001
ERR_XML = -40002
ERR_AES_KEY = -40004
ERR_CORP_ID = -40005
ERR_ENCRYPT = -40006
ERR_DECRYPT = -40007
ERR_BUFFER = -40008


class WeComCryptoError(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        super().__init__(message or f"wecom crypto error {code}")


def _sha1_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


class _PKCS7:
    block_size = 32

    @classmethod
    def pad(cls, data: bytes) -> bytes:
        amount = cls.block_size - (len(data) % cls.block_size)
        if amount == 0:
            amount = cls.block_size
        return data + bytes([amount] * amount)

    @classmethod
    def unpad(cls, data: bytes) -> bytes:
        pad = data[-1]
        if pad < 1 or pad > cls.block_size:
            return data
        return data[:-pad]


@dataclass(frozen=True)
class WeComCrypt:
    token: str
    encoding_aes_key: str
    receive_id: str

    def __post_init__(self) -> None:
        try:
            key = base64.b64decode(self.encoding_aes_key + "=")
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError(ERR_AES_KEY, "EncodingAESKey 非法") from exc
        if len(key) != 32:
            raise WeComCryptoError(ERR_AES_KEY, "EncodingAESKey 长度错误")
        object.__setattr__(self, "_aes_key", key)

    @property
    def _key(self) -> bytes:
        return getattr(self, "_aes_key")  # type: ignore[no-any-return]

    def verify_url(
        self, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> str:
        expected = _sha1_signature(self.token, timestamp, nonce, echostr)
        if expected != msg_signature:
            raise WeComCryptoError(ERR_SIGNATURE, "URL 签名校验失败")
        plain = self._decrypt(echostr)
        return plain.decode("utf-8")

    def decrypt_message(
        self, post_data: str, msg_signature: str, timestamp: str, nonce: str
    ) -> str:
        encrypt = self._extract_encrypt(post_data)
        expected = _sha1_signature(self.token, timestamp, nonce, encrypt)
        if expected != msg_signature:
            raise WeComCryptoError(ERR_SIGNATURE, "消息签名校验失败")
        plain = self._decrypt(encrypt)
        return plain.decode("utf-8")

    def encrypt_message(
        self, reply_xml: str, nonce: str, timestamp: str | None = None
    ) -> str:
        ts = timestamp or str(int(time.time()))
        encrypt = self._encrypt(reply_xml.encode("utf-8")).decode("utf-8")
        signature = _sha1_signature(self.token, ts, nonce, encrypt)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{ts}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )

    def _extract_encrypt(self, xml_text: str) -> str:
        try:
            root = ET.fromstring(xml_text)
            node = root.find("Encrypt")
            if node is None or not (node.text or "").strip():
                raise WeComCryptoError(ERR_XML, "缺少 Encrypt 节点")
            return node.text.strip()
        except WeComCryptoError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError(ERR_XML, "XML 解析失败") from exc

    def _encrypt(self, msg: bytes) -> bytes:
        random16 = str(random.randint(10**15, 10**16 - 1)).encode("utf-8")
        pack = (
            random16
            + struct.pack("I", socket.htonl(len(msg)))
            + msg
            + self.receive_id.encode("utf-8")
        )
        padded = _PKCS7.pad(pack)
        try:
            cipher = AES.new(self._key, AES.MODE_CBC, self._key[:16])
            return base64.b64encode(cipher.encrypt(padded))
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError(ERR_ENCRYPT, "AES 加密失败") from exc

    def _decrypt(self, encrypt_b64: str) -> bytes:
        try:
            cipher = AES.new(self._key, AES.MODE_CBC, self._key[:16])
            plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError(ERR_DECRYPT, "AES 解密失败") from exc
        try:
            plain = _PKCS7.unpad(plain)
            content = plain[16:]
            xml_len = socket.ntohl(struct.unpack("I", content[:4])[0])
            xml_content = content[4 : 4 + xml_len]
            from_receive_id = content[4 + xml_len :].decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError(ERR_BUFFER, "解密后 buffer 非法") from exc
        if from_receive_id != self.receive_id:
            raise WeComCryptoError(ERR_CORP_ID, "CorpId/ReceiveId 不匹配")
        return xml_content
