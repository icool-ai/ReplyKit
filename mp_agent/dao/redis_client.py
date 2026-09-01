"""Optional Redis client wrapper (graceful fallback when Redis unavailable).

Design mirrors ``sync_db.py``:
* Redis is OPTIONAL — if REDIS_URL is not set or connect fails,
  all helpers degrade to safe no-ops (never break core auth flow).
* Single global client, lazily initialised.
* Exposes focused helpers for this project's two use-cases:
  1. JWT access-token blacklist  (场景 1)
  2. Login rate-limit counter    (场景 5)

Testing / override hook:
  Call ``_reset_global_client(force_redis_url="redis://...")`` to swap the
  singleton client mid-process (used by test scripts that target a
  specific Redis endpoint without relying on process-wide env vars).
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

_override_redis_url: str | None = None
_override_redis_lock = threading.Lock()


def _reset_global_client(*, force_redis_url: str | None = None) -> None:
    """Reset (or override) the global Redis client for testing purposes.

    Pass ``force_redis_url`` to bypass ``_build_redis_url`` env resolution
    and target a specific endpoint; pass ``None`` to clear the override
    and revert to env-driven behaviour.
    """
    global _global_client, _override_redis_url, _last_connect_fail_ts
    with _override_redis_lock, _global_init_lock:
        _override_redis_url = force_redis_url
        _global_client = None
        _last_connect_fail_ts = 0.0


def _build_redis_url() -> str | None:
    """Resolve REDIS_URL from env, honouring both config sources.

    If an in-process override has been installed via
    :func:`_reset_global_client`, that value takes precedence (for tests).
    """
    with _override_redis_lock:
        if _override_redis_url:
            return _override_redis_url
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


def revoke_all_access_for_user(
    username: str,
    ttl_seconds: int,
    *,
    now_margin_seconds: int = 1,
) -> bool:
    """Revoke ALL access tokens issued *at or before (now - margin)* for a user.

    Marker stores ``max(0, now - now_margin_seconds)`` as the unix timestamp.
    ``decode_access`` compares ``iat <= marker``.

    Margin strategy — call site picks based on whether *new valid tokens*
    are expected in the very same second:

    * ``now_margin_seconds=1`` — safe for password changes.  User resets
      pw and immediately logs in with the new one; the fresh token's
      ``iat == now`` must NOT be caught by the marker, so marker = now-1.
    * ``now_margin_seconds=0`` — aggressive for disable-user / explicit
      logout flows where we want *every* in-flight token (including ones
      minted this second) revoked.

    ``ttl_seconds >= access_ttl`` ensures the marker outlives any token
    it is meant to kill.
    """
    if not username or ttl_seconds <= 0:
        return False
    with get_redis() as r:
        if r is None:
            return False
        try:
            key = f"{_USER_REVOKE_KEY_PREFIX}{username}"
            marker = int(time.time()) - max(0, int(now_margin_seconds))
            if marker < 0:
                marker = 0
            r.setex(key, ttl_seconds, str(marker))
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


# ---------------------------------------------------------------------------
# High-level helpers: Generic KV cache (场景 2/3 通用)
# ---------------------------------------------------------------------------

def cache_get(key: str) -> str | None:
    """Fetch a cached string from Redis. Returns None on miss / unavailable."""
    if not key:
        return None
    with get_redis() as r:
        if r is None:
            return None
        try:
            return r.get(key)
        except Exception as exc:
            _logger.warning("Redis cache_get %s failed: %s", key, exc)
            return None


def cache_setex(key: str, ttl_seconds: int, value: str) -> bool:
    """Write a string to Redis with TTL. Returns True on success."""
    if not key or ttl_seconds <= 0 or value is None:
        return False
    with get_redis() as r:
        if r is None:
            return False
        try:
            r.setex(key, ttl_seconds, value)
            return True
        except Exception as exc:
            _logger.warning("Redis cache_setex %s failed: %s", key, exc)
            return False


def cache_delete(*keys: str) -> int:
    """Delete one or more cache keys. Returns number of keys removed."""
    cleaned = [k for k in keys if k]
    if not cleaned:
        return 0
    with get_redis() as r:
        if r is None:
            return 0
        try:
            return int(r.delete(*cleaned) or 0)
        except Exception as exc:
            _logger.warning("Redis cache_delete %s failed: %s", cleaned, exc)
            return 0


# ---------------------------------------------------------------------------
# High-level helpers: ChatSession 热数据缓存 (场景 3)
#
# 建模模式：Write-Through 写穿透 + TTL 冷热淘汰 + get/save 双路穿透
# 场景特征：读写都高频 + 每条数据大 (history 几十到几百条消息) + 明显冷热分层
#           (活跃用户最近会话是热数据，超过 30min 没人访问的会话自动冷出)
#
# Key:   cache:session:{session_id}  (String → JSON payload)
# TTL:   SESSION_CACHE_TTL_SEC (default 1800s = 30min)
#        → 30min 内无任何 get/save 访问，Redis 自动淘汰，内存让位给热会话
#        → 每次命中都 EXPIRE 刷新 TTL (滑动窗口)，持续访问的会话永不过期
#
# 一致性策略：
#   * SQLite 是单一 Source of Truth，Redis 纯为 Look-Aside 加速层
#   * Write-Through: save() 路径先写 DB，再同步写 Redis，同请求周期完成
#   * 读穿透 (Read-Through): get() 未命中时，从 DB 回填 Redis
#   * Redis 故障/淘汰: 下次 get() 透明从 DB 重建，无数据丢失风险
# ---------------------------------------------------------------------------

_SESSION_CACHE_KEY_PREFIX = "cache:session:"
_SESSION_CACHE_TTL_DEFAULT = 1800


def _session_cache_key(session_id: str) -> str:
    return f"{_SESSION_CACHE_KEY_PREFIX}{session_id}"


def session_cache_get(
    session_id: str,
    *,
    ttl_seconds: int = _SESSION_CACHE_TTL_DEFAULT,
) -> str | None:
    """读穿透 GET: 命中返回 JSON 字符串并刷新 TTL (滑动窗口保热); 未命中返回 None。

    Redis 不可用时返回 None (调用方透明回退 DB，优雅降级)。
    """
    if not session_id:
        return None
    key = _session_cache_key(session_id)
    with get_redis() as r:
        if r is None:
            return None
        try:
            val = r.get(key)
            if val is None:
                return None
            if ttl_seconds > 0:
                try:
                    r.expire(key, ttl_seconds)
                except Exception:
                    pass
            return val
        except Exception as exc:
            _logger.warning("Redis session_cache_get %s failed: %s", session_id, exc)
            return None


def session_cache_put(
    session_id: str,
    json_payload: str,
    *,
    ttl_seconds: int = _SESSION_CACHE_TTL_DEFAULT,
) -> bool:
    """写穿透 PUT: SETEX 写入 JSON + TTL。用于 save() 写穿透 和 get() 未命中回填。

    返回 True 表示写入成功，False 表示 Redis 不可用或失败 (调用方无需处理)。
    """
    if not session_id or json_payload is None or ttl_seconds <= 0:
        return False
    key = _session_cache_key(session_id)
    with get_redis() as r:
        if r is None:
            return False
        try:
            r.setex(key, ttl_seconds, json_payload)
            return True
        except Exception as exc:
            _logger.warning("Redis session_cache_put %s failed: %s", session_id, exc)
            return False


def session_cache_invalidate(*session_ids: str) -> int:
    """主动失效 (用于 delete / clear)。返回实际删除的 key 数。"""
    keys = [_session_cache_key(sid) for sid in session_ids if sid]
    return cache_delete(*keys)


# ---------------------------------------------------------------------------
# High-level helpers: 飞书 OAuth state (优先级 1)
#
# 场景特征：短时一次性票据，TTL=15min，多实例必须共享（否则多 worker 回调必失败）
# 设计：SETNX + SETEX 保证唯一写入后 TTL，GET + DEL 原子消费（用 Lua 避免 race）
# Key:   oauth:feishu:state:{state}  (String → JSON: {cid, open_id, exp})
# ---------------------------------------------------------------------------

_OAUTH_STATE_KEY_PREFIX = "oauth:feishu:state:"


def oauth_state_put(state: str, config_id: str, open_id: str, ttl_seconds: int) -> bool:
    """写入 OAuth state（带 TTL）。返回 True 成功，False 表示 Redis 不可用。"""
    if not state or not config_id or not open_id or ttl_seconds <= 0:
        return False
    key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
    import json as _json
    payload = _json.dumps({"cid": config_id, "oid": open_id})
    return cache_setex(key, ttl_seconds, payload)


def oauth_state_consume(state: str) -> tuple[str, str] | None:
    """一次性消费 OAuth state：返回 (config_id, open_id)，不存在/过期/Redis 不可用返回 None。

    使用 Lua 脚本保证「GET + DEL」原子性，避免并发回调重复消费。
    """
    if not state:
        return None
    key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
    with get_redis() as r:
        if r is None:
            return None
        try:
            lua = """
                local val = redis.call("GET", KEYS[1])
                if val then
                    redis.call("DEL", KEYS[1])
                end
                return val
            """
            raw = r.eval(lua, 1, key)
            if not raw:
                return None
            import json as _json
            obj = _json.loads(raw)
            return str(obj.get("cid") or ""), str(obj.get("oid") or "")
        except Exception as exc:
            _logger.warning("Redis oauth_state_consume %s failed: %s", state, exc)
            return None


# ---------------------------------------------------------------------------
# High-level helpers: 飞书/外部短期凭据缓存 (优先级 2: tenant_access_token / jsapi_ticket)
#
# 场景特征：2h 左右 TTL，多实例共享避免重复请求外部 API 触达频控 + 节省配额
# 设计：GET 命中直接返回；未命中由调用方请求外部 API 后 SETNX + SETEX
#       （Redis 层提供 get/set 两个简单 helper，竞争安全由调用方用 SET NX 保证）
# Key:   cred:{category}:{identity}  (String → credential payload)
# ---------------------------------------------------------------------------

_CRED_KEY_PREFIX = "cred:"


def cred_get(category: str, identity: str) -> str | None:
    """读取短期凭据缓存：未命中/Redis 不可用返回 None。"""
    if not category or not identity:
        return None
    key = f"{_CRED_KEY_PREFIX}{category}:{identity}"
    return cache_get(key)


def cred_setex(
    category: str, identity: str, value: str, ttl_seconds: int, *, nx: bool = False
) -> bool:
    """写入短期凭据缓存。

    * ``nx=True``：仅当 key 不存在时写入（SET NX），避免并发刷新时互相覆盖。
      典型用法：刷新 token 前 SETNX 抢锁，成功那个才去请求外部 API，然后再用普通 setex 写最终 token。
    """
    if not category or not identity or not value or ttl_seconds <= 0:
        return False
    key = f"{_CRED_KEY_PREFIX}{category}:{identity}"
    if not nx:
        return cache_setex(key, ttl_seconds, value)
    with get_redis() as r:
        if r is None:
            return False
        try:
            ok = bool(r.set(key, value, nx=True, ex=ttl_seconds))
            return ok
        except Exception as exc:
            _logger.warning("Redis cred_setex NX %s failed: %s", key, exc)
            return False


def cred_delete(category: str, identity: str) -> int:
    """主动失效凭据（外部 API 返回 invalid token 时调用）。"""
    if not category or not identity:
        return 0
    key = f"{_CRED_KEY_PREFIX}{category}:{identity}"
    return cache_delete(key)


# ---------------------------------------------------------------------------
# High-level helpers: 飞书用户 access_token 读穿透缓存 (优先级 3)
#
# 场景特征：FeishuUserToken.access_token 2h 内有效，读极高频（每次飞书任务/查联系人前）
#           但写极少（refresh 才写）。refresh_token 必须仍保留在 DB。
# 设计：Look-Aside 读穿透，TTL 提前 2min 自然过期，强制刷新用 invalidate helper
# Key:   cred:feishu_user_access:{config_id}:{open_id}  (String → access_token)
# ---------------------------------------------------------------------------

_FEISHU_USER_ACCESS_CATEGORY = "feishu_user_access"


def feishu_user_access_cache_get(config_id: str, open_id: str) -> str | None:
    """读穿透：命中返回 access_token；未命中/Redis 不可用返回 None（调用方查 DB 回填）。"""
    return cred_get(_FEISHU_USER_ACCESS_CATEGORY, f"{config_id}:{open_id}")


def feishu_user_access_cache_put(
    config_id: str, open_id: str, access_token: str, ttl_seconds: int
) -> bool:
    """DB 查到 / refresh 成功后回填。TTL 请传入「expires_at - now - 120」（提前 2min 自然过期）。"""
    if ttl_seconds <= 0:
        return False
    return cred_setex(
        _FEISHU_USER_ACCESS_CATEGORY,
        f"{config_id}:{open_id}",
        access_token,
        ttl_seconds,
    )


def feishu_user_access_cache_invalidate(config_id: str, open_id: str) -> int:
    """refresh token 失败 / 用户换绑后主动失效。"""
    return cred_delete(_FEISHU_USER_ACCESS_CATEGORY, f"{config_id}:{open_id}")
