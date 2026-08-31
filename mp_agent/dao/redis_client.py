"""Optional Redis client wrapper (graceful fallback when Redis unavailable).

Design mirrors ``sync_db.py``:
* Redis is OPTIONAL — if REDIS_URL is not set or connect fails,
  all helpers degrade to safe no-ops (never break core auth flow).
* Single global client, lazily initialised.
* Exposes focused helpers for this project's two use-cases:
  1. JWT access-token blacklist  (场景 1)
  2. Login rate-limit counter    (场景 5)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

_logger = logging.getLogger(__name__)

_global_client: object | None = None
_global_init_lock = threading.Lock()
_last_connect_fail_ts: float = 0.0
_CONNECT_BACKOFF_SEC: float = 5.0


def _build_redis_url() -> str | None:
    """Resolve REDIS_URL from env, honouring both config sources."""
    raw = (
        os.getenv("REDIS_URL")
        or os.getenv("MP_AGENT_REDIS_URL")
        or ""
    ).strip()
    if not raw:
        return None
    enabled_flag = (
        os.getenv("REDIS_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if not enabled_flag:
        return None
    return raw


def _try_create_client(url: str) -> object | None:
    """Attempt to create + ping a Redis client; return None on any failure."""
    try:
        from redis import Redis  # type: ignore
    except ImportError:
        _logger.warning(
            "redis 包未安装（pip install redis）；Redis 黑名单/限流功能跳过"
        )
        return None
    try:
        client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        _logger.info("Redis 连接成功: %s", url.split("@")[-1])
        return client
    except Exception as exc:  # pragma: no cover - network dependent
        _logger.warning("Redis 连接失败，黑名单/限流功能降级为跳过: %s", exc)
        return None


def get_redis_client() -> object | None:
    """Return the shared Redis client or None when unavailable.

    Thread-safe lazy init; after a failed connect we back off for
    ``_CONNECT_BACKOFF_SEC`` seconds to avoid spamming the server.
    """
    global _global_client, _last_connect_fail_ts

    if _global_client is not None:
        return _global_client

    url = _build_redis_url()
    if url is None:
        return None

    with _global_init_lock:
        if _global_client is not None:
            return _global_client
        now = time.time()
        if now - _last_connect_fail_ts < _CONNECT_BACKOFF_SEC:
            return None
        client = _try_create_client(url)
        if client is None:
            _last_connect_fail_ts = now
            return None
        _global_client = client
        return client


@contextmanager
def get_redis() -> Iterator[object | None]:
    """Context manager yielding the Redis client or None.

    Usage::

        with get_redis() as r:
            if r is not None:
                r.setex("k", 3600, "v")
    """
    yield get_redis_client()


# ---------------------------------------------------------------------------
# High-level helpers: JWT access-token blacklist
# ---------------------------------------------------------------------------

_BLACKLIST_KEY_PREFIX = "auth:blacklist:access:"
_USER_REVOKE_KEY_PREFIX = "auth:revoke_all:before:"


def blacklist_access_jti(jti: str, ttl_seconds: int) -> bool:
    """Add a single access-token (by jti) to the Redis blacklist with TTL.

    Returns True if blacklisted, False if Redis unavailable.
    ``ttl_seconds`` should equal the token's remaining lifetime so the
    blacklist auto-evicts stale entries.
    """
    if not jti or ttl_seconds <= 0:
        return False
    with get_redis() as r:
        if r is None:
            return False
        try:
            key = f"{_BLACKLIST_KEY_PREFIX}{jti}"
            # SET NX EX 语义：设置成功才 True；已存在也算成功（幂等）
            # 直接用 setex，覆盖旧值（TTL 会刷新为最新剩余寿命，可接受）
            r.setex(key, ttl_seconds, "1")
            return True
        except Exception as exc:
            _logger.warning("Redis blacklist write failed: %s", exc)
            return False


def is_access_blacklisted(jti: str) -> bool:
    """Return True if the jti is blacklisted; False when clean or no Redis."""
    if not jti:
        return False
    with get_redis() as r:
        if r is None:
            return False
        try:
            return bool(r.exists(f"{_BLACKLIST_KEY_PREFIX}{jti}"))
        except Exception as exc:
            _logger.warning("Redis blacklist read failed: %s", exc)
            return False


def revoke_all_access_for_user(username: str, ttl_seconds: int) -> bool:
    """Revoke ALL access tokens issued *before now* for the given user.

    Implemented by storing a "revoke-before" unix timestamp per user.
    ``decode_access`` must compare ``iat < revoke_before``.
    ``ttl_seconds`` should be >= access_ttl so the marker outlives any
    currently-issued access token.
    """
    if not username or ttl_seconds <= 0:
        return False
    with get_redis() as r:
        if r is None:
            return False
        try:
            key = f"{_USER_REVOKE_KEY_PREFIX}{username}"
            r.setex(key, ttl_seconds, str(int(time.time())))
            return True
        except Exception as exc:
            _logger.warning("Redis revoke-all write failed: %s", exc)
            return False


def get_user_revoke_before(username: str) -> int:
    """Return the unix timestamp before which access tokens are revoked.

    Returns 0 when no revocation marker exists or Redis is unavailable.
    """
    if not username:
        return 0
    with get_redis() as r:
        if r is None:
            return 0
        try:
            raw = r.get(f"{_USER_REVOKE_KEY_PREFIX}{username}")
            return int(raw) if raw else 0
        except Exception as exc:
            _logger.warning("Redis revoke-all read failed: %s", exc)
            return 0


# ---------------------------------------------------------------------------
# High-level helpers: Login rate limiting (fixed window counter)
# ---------------------------------------------------------------------------

_RATE_KEY_PREFIX = "ratelimit:login:"


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: int, attempts: int, limit: int, window_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.limit = limit
        self.window_seconds = window_seconds
        super().__init__(
            f"登录尝试 {attempts}/{limit} 超过 {window_seconds}s 窗口限制，"
            f"请 {retry_after_seconds}s 后重试"
        )


def login_rate_limit_increment(
    *,
    ip: str,
    username: str,
    limit: int = 5,
    window_seconds: int = 300,
) -> None:
    """Increment the login-fail counter. Raises ``RateLimitExceeded`` if
    count > ``limit`` within the current window.

    Atomic on Redis via INCR + first-time EXPIRE (standard pattern).
    When Redis is unavailable this is a safe no-op (never raise).
    """
    if not ip and not username:
        return
    key = f"{_RATE_KEY_PREFIX}{ip or 'unknown'}:{username or '*'}"
    with get_redis() as r:
        if r is None:
            return
        try:
            attempts = r.incr(key)
            if attempts == 1:
                r.expire(key, window_seconds)
            ttl = r.ttl(key)
            retry_after = int(ttl) if isinstance(ttl, int) and ttl > 0 else window_seconds
            if attempts > limit:
                raise RateLimitExceeded(
                    retry_after_seconds=retry_after,
                    attempts=attempts,
                    limit=limit,
                    window_seconds=window_seconds,
                )
        except RateLimitExceeded:
            raise
        except Exception as exc:
            _logger.warning("Redis rate-limit write failed: %s", exc)
            return


def login_rate_limit_reset(*, ip: str, username: str) -> None:
    """Clear the rate-limit counter after a successful login."""
    if not ip and not username:
        return
    key = f"{_RATE_KEY_PREFIX}{ip or 'unknown'}:{username or '*'}"
    with get_redis() as r:
        if r is None:
            return
        try:
            r.delete(key)
        except Exception as exc:
            _logger.warning("Redis rate-limit reset failed: %s", exc)
