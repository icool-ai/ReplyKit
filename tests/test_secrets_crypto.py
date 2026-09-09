"""Unit tests for at-rest secret hashing / AES-GCM helpers."""

from __future__ import annotations

import secrets
import unittest

from src.secrets_crypto import (
    SecretsCryptoError,
    decrypt_secret,
    encrypt_secret,
    hash_api_key,
    is_api_key_hash,
    is_encrypted_secret,
    mask_api_key_prefix,
    parse_api_key_hash,
    stored_api_key_prefix,
    verify_api_key,
)


def _key() -> bytes:
    return secrets.token_bytes(32)


class SecretsCryptoTests(unittest.TestCase):
    def test_api_key_hash_roundtrip(self) -> None:
        pepper = secrets.token_bytes(32)
        plain = f"rk_dify_{secrets.token_urlsafe(32)}"
        stored = hash_api_key(plain, pepper=pepper)
        self.assertTrue(is_api_key_hash(stored))
        prefix, digest = parse_api_key_hash(stored)
        self.assertEqual(prefix, plain[:12])
        self.assertEqual(len(digest), 64)
        self.assertTrue(verify_api_key(stored, plain, pepper=pepper))
        self.assertFalse(verify_api_key(stored, plain + "x", pepper=pepper))
        self.assertEqual(stored_api_key_prefix(stored), plain[:12])
        self.assertTrue(mask_api_key_prefix(prefix).endswith("…****"))

    def test_legacy_plaintext_verify(self) -> None:
        plain = "rk_dify_legacy_plaintext_key_value"
        self.assertTrue(verify_api_key(plain, plain))
        self.assertFalse(verify_api_key(plain, plain + "no"))

    def test_aes_gcm_roundtrip(self) -> None:
        key = _key()
        plain = "feishu-app-secret-value"
        enc = encrypt_secret(plain, master_key=key)
        self.assertTrue(is_encrypted_secret(enc))
        self.assertNotEqual(enc, plain)
        self.assertEqual(decrypt_secret(enc, master_key=key), plain)
        # idempotent encrypt
        self.assertEqual(encrypt_secret(enc, master_key=key), enc)
        # empty passthrough
        self.assertEqual(encrypt_secret("", master_key=key), "")
        self.assertEqual(decrypt_secret("", master_key=key), "")

    def test_aes_gcm_wrong_key_fails(self) -> None:
        enc = encrypt_secret("secret", master_key=_key())
        with self.assertRaises(SecretsCryptoError):
            decrypt_secret(enc, master_key=_key())

    def test_example_env_key_parses(self) -> None:
        from src.secrets_crypto import parse_master_key

        key = parse_master_key("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        self.assertEqual(len(key), 32)
        self.assertEqual(key, b"\x00" * 32)


if __name__ == "__main__":
    unittest.main()
