"""Redis health probe + chat-run fallback observability."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.chat_runs import ChatRunStore, RedisRequiredError


class RedisHealthTests(unittest.TestCase):
    def test_probe_not_configured(self) -> None:
        from mp_agent.dao.redis_client import probe_redis_health

        with patch(
            "mp_agent.dao.redis_client.redis_is_configured", return_value=False
        ):
            result = probe_redis_health()
        self.assertFalse(result["configured"])
        self.assertFalse(result["ok"])

    def test_probe_ok(self) -> None:
        from mp_agent.dao.redis_client import probe_redis_health

        class _Client:
            def ping(self) -> bool:
                return True

        with (
            patch(
                "mp_agent.dao.redis_client.redis_is_configured", return_value=True
            ),
            patch(
                "mp_agent.dao.redis_client.get_redis_client", return_value=_Client()
            ),
        ):
            result = probe_redis_health()
        self.assertTrue(result["configured"])
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["latency_ms"])

    def test_fallback_observability_and_require(self) -> None:
        store = ChatRunStore(require_redis=False)
        with (
            patch("src.chat_runs.redis_is_configured", return_value=True),
            patch("src.chat_runs.get_redis_client", return_value=None),
        ):
            run = store.create(
                session_id="obs-s1",
                username="u",
                message="hi",
                public_base_url="http://x",
            )
            self.assertEqual(run.backend, "memory")
            obs = store.observability()
            self.assertEqual(obs["backend"], "memory")
            self.assertGreaterEqual(obs["memory_fallback_count"], 1)
            self.assertIsNotNone(obs["last_memory_fallback_at"])

        strict = ChatRunStore(require_redis=True)
        with (
            patch("src.chat_runs.redis_is_configured", return_value=True),
            patch("src.chat_runs.get_redis_client", return_value=None),
        ):
            with self.assertRaises(RedisRequiredError):
                strict.create(
                    session_id="obs-s2",
                    username="u",
                    message="hi",
                    public_base_url="http://x",
                )


if __name__ == "__main__":
    unittest.main()
