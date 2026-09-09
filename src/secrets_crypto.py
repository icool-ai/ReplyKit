"""At-rest secret helpers: HMAC-SHA256 for API keys, AES-256-GCM for reversible secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from Crypto.Cipher import AES

# Dify API key hash: v1$<prefix>$<hex>
_API_KEY_HASH_PREFIX = "v1$"
# Reversible ciphertext: enc:v1:<b64(nonce|ciphertext|tag)>
_ENC_PREFIX = "enc:v1:"
_AES_NONCE_LEN = 12
_AES_TAG_LEN = 16
_API_KEY_PREFIX_LEN = 12


class SecretsCryptoError(ValueError):
    """Master key / decrypt / hash format errors."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def parse_master_key(raw: str) -> bytes:
    """Accept urlsafe/standard base64 or raw 32-byte utf-8 (discouraged)."""
    text = (raw or "").strip()
    if not text:
        raise SecretsCryptoError("SECRETS_MASTER_KEY 未配置")
    for decoder in (_b64url_decode, base64.b64decode):
        try:
            key = decoder(text)
            if len(key) == 32:
                return key
        except Exception:  # noqa: BLE001
            continue
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) == 32:
        return raw_bytes
    raise SecretsCryptoError(
        "SECRETS_MASTER_KEY 须为 32 字节（推荐 urlsafe base64）。"
        "可用: python -c \"import secrets,base64; "
        "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
    )


def generate_master_key_b64() -> str:
    return _b64url_encode(secrets.token_bytes(32))


@lru_cache(maxsize=1)
def _cached_master_key(raw: str) -> bytes:
    return parse_master_key(raw)


def get_master_key(raw: str | None = None) -> bytes:
    from src.config import get_settings

    value = (raw if raw is not None else get_settings().secrets_master_key).strip()
    return _cached_master_key(value)


def get_api_key_pepper(raw: str | None = None) -> bytes:
    """Pepper for API key HMAC; defaults to HKDF-like digest of master key."""
    from src.config import get_settings

    settings = get_settings()
    pepper = (raw if raw is not None else settings.api_key_pepper).strip()
    if pepper:
        return hashlib.sha256(pepper.encode("utf-8")).digest()
    return hashlib.sha256(b"replykit-api-key-pepper|" + get_master_key()).digest()


def clear_crypto_caches() -> None:
    """Test helper: drop cached master key."""
    _cached_master_key.cache_clear()


def api_key_display_prefix(plaintext: str, length: int = _API_KEY_PREFIX_LEN) -> str:
    text = (plaintext or "").strip()
    if not text:
        return ""
    return text[:length]


def mask_api_key_prefix(prefix: str) -> str:
    p = (prefix or "").strip()
    if not p:
        return ""
    return f"{p}…****"


def is_api_key_hash(stored: str) -> bool:
    return (stored or "").startswith(_API_KEY_HASH_PREFIX)


def hash_api_key(plaintext: str, *, pepper: bytes | None = None) -> str:
    plain = (plaintext or "").strip()
    if not plain:
        raise SecretsCryptoError("api_key 不能为空")
    digest = hmac.new(
        pepper if pepper is not None else get_api_key_pepper(),
        plain.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    prefix = api_key_display_prefix(plain)
    return f"{_API_KEY_HASH_PREFIX}{prefix}${digest}"


def parse_api_key_hash(stored: str) -> tuple[str, str]:
    """Return (prefix, hex_digest)."""
    text = stored or ""
    if not text.startswith(_API_KEY_HASH_PREFIX):
        raise SecretsCryptoError("不是 API Key 哈希格式")
    body = text[len(_API_KEY_HASH_PREFIX) :]
    prefix, sep, digest = body.partition("$")
    if not sep or len(digest) != 64:
        raise SecretsCryptoError("API Key 哈希格式损坏")
    return prefix, digest


def verify_api_key(
    stored: str,
    provided: str,
    *,
    pepper: bytes | None = None,
) -> bool:
    """Constant-time verify against hashed or legacy plaintext storage."""
    provided_clean = (provided or "").strip()
    stored_clean = stored or ""
    if not provided_clean or not stored_clean:
        return False
    if is_api_key_hash(stored_clean):
        try:
            _, digest = parse_api_key_hash(stored_clean)
        except SecretsCryptoError:
            return False
        expected = hmac.new(
            pepper if pepper is not None else get_api_key_pepper(),
            provided_clean.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return secrets.compare_digest(digest, expected)
    if len(stored_clean) != len(provided_clean):
        return False
    return secrets.compare_digest(stored_clean, provided_clean)


def stored_api_key_prefix(stored: str) -> str:
    if is_api_key_hash(stored):
        try:
            prefix, _ = parse_api_key_hash(stored)
            return prefix
        except SecretsCryptoError:
            return ""
    return api_key_display_prefix(stored)


def is_encrypted_secret(stored: str) -> bool:
    return (stored or "").startswith(_ENC_PREFIX)


def encrypt_secret(plaintext: str, *, master_key: bytes | None = None) -> str:
    text = plaintext or ""
    if not text:
        return ""
    if is_encrypted_secret(text):
        return text
    key = master_key if master_key is not None else get_master_key()
    nonce = secrets.token_bytes(_AES_NONCE_LEN)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(text.encode("utf-8"))
    blob = nonce + ciphertext + tag
    return _ENC_PREFIX + _b64url_encode(blob)


def decrypt_secret(stored: str, *, master_key: bytes | None = None) -> str:
    text = stored or ""
    if not text:
        return ""
    if not is_encrypted_secret(text):
        return text
    key = master_key if master_key is not None else get_master_key()
    try:
        blob = _b64url_decode(text[len(_ENC_PREFIX) :])
    except Exception as exc:  # noqa: BLE001
        raise SecretsCryptoError("密文解码失败") from exc
    if len(blob) < _AES_NONCE_LEN + _AES_TAG_LEN:
        raise SecretsCryptoError("密文过短")
    nonce = blob[:_AES_NONCE_LEN]
    tag = blob[-_AES_TAG_LEN:]
    ciphertext = blob[_AES_NONCE_LEN : -_AES_TAG_LEN]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        plain = cipher.decrypt_and_verify(ciphertext, tag)
    except Exception as exc:  # noqa: BLE001
        raise SecretsCryptoError("密文解密失败（主密钥是否变更？）") from exc
    return plain.decode("utf-8")
