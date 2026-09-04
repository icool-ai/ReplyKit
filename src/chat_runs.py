"""Chat run store: append-only event log for SSE resume (A) + active run (B).

Uses Redis when available (multi-replica safe). Falls back to process memory
when Redis is unset/unreachable (single-instance only).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mp_agent.dao.redis_client import get_redis_client, redis_is_configured

_logger = logging.getLogger(__name__)

_META_PREFIX = "chatrun:meta:"
_EVENTS_PREFIX = "chatrun:events:"
_SEQ_PREFIX = "chatrun:seq:"
_ACTIVE_PREFIX = "chatrun:active:"
_FALLBACK_LOG_INTERVAL_SEC = 60.0


class ConcurrentChatRunError(RuntimeError):
    """Session already has an in-flight chat run."""


class RedisRequiredError(RuntimeError):
    """Redis is required for chat runs but unavailable."""


def _meta_key(run_id: str) -> str:
    return f"{_META_PREFIX}{run_id}"


def _events_key(run_id: str) -> str:
    return f"{_EVENTS_PREFIX}{run_id}"


def _seq_key(run_id: str) -> str:
    return f"{_SEQ_PREFIX}{run_id}"


def _active_key(session_id: str) -> str:
    return f"{_ACTIVE_PREFIX}{session_id}"


@dataclass
class ChatRun:
    """Facade over memory or Redis-backed event log."""

    run_id: str
    session_id: str
    username: str
    message: str
    public_base_url: str
    created_at: float
    backend: str  # "memory" | "redis"
    _store: "ChatRunStore" = field(repr=False)

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        return self._store.append_event(self.run_id, event_type, **payload)

    def events_after(self, after_id: int) -> list[dict[str, Any]]:
        return self._store.events_after(self.run_id, after_id)

    def snapshot_done(self) -> bool:
        return self._store.is_done(self.run_id)

    @property
    def delta_emitted(self) -> bool:
        return self._store.delta_emitted(self.run_id)


@dataclass
class _MemoryRun:
    run_id: str
    session_id: str
    username: str
    message: str
    public_base_url: str
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 1
    done: bool = False
    delta_emitted: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class ChatRunStore:
    """Redis-first run registry with in-memory fallback."""

    def __init__(
        self,
        *,
        absolute_ttl_sec: float = 900.0,
        retain_after_done_sec: float = 300.0,
        require_redis: bool = False,
    ) -> None:
        self._runs: dict[str, _MemoryRun] = {}
        self._active_by_session: dict[str, str] = {}
        self._lock = threading.Lock()
        self.absolute_ttl_sec = int(absolute_ttl_sec)
        self.retain_after_done_sec = int(retain_after_done_sec)
        self.require_redis = bool(require_redis)
        self._fallback_count = 0
        self._last_fallback_at: float | None = None
        self._last_fallback_reason: str | None = None
        self._fallback_log_lock = threading.Lock()
        self._last_fallback_log_at = 0.0

    def observability(self) -> dict[str, Any]:
        """Snapshot for /health monitors."""
        redis_up = self._redis() is not None
        configured = redis_is_configured()
        backend = "redis" if redis_up else "memory"
        return {
            "backend": backend,
            "redis_configured": configured,
            "require_redis": self.require_redis,
            "memory_fallback_count": self._fallback_count,
            "last_memory_fallback_at": self._last_fallback_at,
            "last_memory_fallback_reason": self._last_fallback_reason,
        }

    def _note_memory_fallback(self, reason: str) -> None:
        """Record + rate-limited ERROR log when Redis was expected but unused."""
        now = time.time()
        with self._fallback_log_lock:
            self._fallback_count += 1
            self._last_fallback_at = now
            self._last_fallback_reason = reason
            count = self._fallback_count
            should_log = (now - self._last_fallback_log_at) >= _FALLBACK_LOG_INTERVAL_SEC
            if should_log:
                self._last_fallback_log_at = now
        if should_log and redis_is_configured():
            _logger.error(
                "chat-run Redis unavailable; using process memory "
                "(multi-replica unsafe). reason=%s count=%s",
                reason,
                count,
            )

    def _ensure_redis_or_raise(self, *, op: str) -> Any:
        r = self._redis()
        if r is not None:
            return r
        if self.require_redis and redis_is_configured():
            raise RedisRequiredError(
                f"chat-run requires Redis but it is unavailable ({op})"
            )
        if self.require_redis and not redis_is_configured():
            raise RedisRequiredError(
                "chat-run requires Redis but REDIS_URL is not configured"
            )
        return None

    def _redis(self) -> Any | None:
        return get_redis_client()

    def _wrap(self, raw: _MemoryRun | dict[str, Any], *, backend: str) -> ChatRun:
        if backend == "memory":
            assert isinstance(raw, _MemoryRun)
            return ChatRun(
                run_id=raw.run_id,
                session_id=raw.session_id,
                username=raw.username,
                message=raw.message,
                public_base_url=raw.public_base_url,
                created_at=raw.created_at,
                backend="memory",
                _store=self,
            )
        assert isinstance(raw, dict)
        return ChatRun(
            run_id=str(raw["run_id"]),
            session_id=str(raw["session_id"]),
            username=str(raw["username"]),
            message=str(raw["message"]),
            public_base_url=str(raw.get("public_base_url") or ""),
            created_at=float(raw.get("created_at") or time.time()),
            backend="redis",
            _store=self,
        )

    def create(
        self,
        *,
        session_id: str,
        username: str,
        message: str,
        public_base_url: str,
    ) -> ChatRun:
        r = self._ensure_redis_or_raise(op="create")
        if r is not None:
            try:
                return self._redis_create(
                    r,
                    session_id=session_id,
                    username=username,
                    message=message,
                    public_base_url=public_base_url,
                )
            except ConcurrentChatRunError:
                raise
            except RedisRequiredError:
                raise
            except Exception as exc:
                if self.require_redis:
                    raise RedisRequiredError(
                        f"chat-run Redis create failed: {exc}"
                    ) from exc
                self._note_memory_fallback(f"create failed: {exc}")
        elif redis_is_configured():
            self._note_memory_fallback("create: client unavailable")
        return self._memory_create(
            session_id=session_id,
            username=username,
            message=message,
            public_base_url=public_base_url,
        )

    def get(self, run_id: str) -> ChatRun | None:
        r = self._redis()
        if r is not None:
            try:
                found = self._redis_get(r, run_id)
                if found is not None:
                    return found
            except Exception as exc:
                _logger.warning("Redis chat-run get failed: %s", exc)
        return self._memory_get(run_id)

    def active_run_id(self, session_id: str) -> str | None:
        r = self._redis()
        if r is not None:
            try:
                return self._redis_active(r, session_id)
            except Exception as exc:
                _logger.warning("Redis chat-run active lookup failed: %s", exc)
        return self._memory_active(session_id)

    def finish_active(self, session_id: str, run_id: str) -> None:
        r = self._redis()
        if r is not None:
            try:
                self._redis_finish_active(r, session_id, run_id)
            except Exception as exc:
                _logger.warning("Redis chat-run finish_active failed: %s", exc)
        self._memory_finish_active(session_id, run_id)

    def append_event(
        self, run_id: str, event_type: str, **payload: Any
    ) -> dict[str, Any]:
        # Prefer Redis if this run lives there; else memory.
        r = self._redis()
        if r is not None:
            try:
                if r.exists(_meta_key(run_id)):
                    return self._redis_append(r, run_id, event_type, **payload)
            except Exception as exc:
                if self.require_redis:
                    raise RedisRequiredError(
                        f"chat-run Redis append failed: {exc}"
                    ) from exc
                self._note_memory_fallback(f"append failed: {exc}")
        return self._memory_append(run_id, event_type, **payload)

    def events_after(self, run_id: str, after_id: int) -> list[dict[str, Any]]:
        r = self._redis()
        if r is not None:
            try:
                if r.exists(_meta_key(run_id)):
                    return self._redis_events_after(r, run_id, after_id)
            except Exception as exc:
                _logger.warning("Redis chat-run events_after failed: %s", exc)
        return self._memory_events_after(run_id, after_id)

    def is_done(self, run_id: str) -> bool:
        r = self._redis()
        if r is not None:
            try:
                raw = r.get(_meta_key(run_id))
                if raw:
                    meta = json.loads(raw)
                    return bool(meta.get("done"))
            except Exception as exc:
                _logger.warning("Redis chat-run is_done failed: %s", exc)
        with self._lock:
            run = self._runs.get(run_id)
            return bool(run and run.done)

    def delta_emitted(self, run_id: str) -> bool:
        r = self._redis()
        if r is not None:
            try:
                raw = r.get(_meta_key(run_id))
                if raw:
                    meta = json.loads(raw)
                    return bool(meta.get("delta_emitted"))
            except Exception as exc:
                _logger.warning("Redis chat-run delta_emitted failed: %s", exc)
        with self._lock:
            run = self._runs.get(run_id)
            return bool(run and run.delta_emitted)

    # ---- Redis ----

    def _redis_create(
        self,
        r: Any,
        *,
        session_id: str,
        username: str,
        message: str,
        public_base_url: str,
    ) -> ChatRun:
        active = _active_key(session_id)
        existing = r.get(active)
        if existing:
            meta_raw = r.get(_meta_key(str(existing)))
            if meta_raw:
                meta = json.loads(meta_raw)
                if not meta.get("done"):
                    raise ConcurrentChatRunError(str(existing))
            r.delete(active)

        run_id = uuid.uuid4().hex
        now = time.time()
        meta = {
            "run_id": run_id,
            "session_id": session_id,
            "username": username,
            "message": message,
            "public_base_url": public_base_url,
            "created_at": now,
            "finished_at": None,
            "done": False,
            "delta_emitted": False,
        }
        pipe = r.pipeline()
        pipe.set(_meta_key(run_id), json.dumps(meta, ensure_ascii=False))
        pipe.set(_seq_key(run_id), 0)
        pipe.set(active, run_id)
        pipe.expire(_meta_key(run_id), self.absolute_ttl_sec)
        pipe.expire(_seq_key(run_id), self.absolute_ttl_sec)
        pipe.expire(_events_key(run_id), self.absolute_ttl_sec)
        pipe.expire(active, self.absolute_ttl_sec)
        pipe.execute()
        return self._wrap(meta, backend="redis")

    def _redis_get(self, r: Any, run_id: str) -> ChatRun | None:
        raw = r.get(_meta_key(run_id))
        if not raw:
            return None
        meta = json.loads(raw)
        return self._wrap(meta, backend="redis")

    def _redis_active(self, r: Any, session_id: str) -> str | None:
        run_id = r.get(_active_key(session_id))
        if not run_id:
            return None
        raw = r.get(_meta_key(str(run_id)))
        if not raw:
            r.delete(_active_key(session_id))
            return None
        meta = json.loads(raw)
        if meta.get("done"):
            return None
        return str(run_id)

    def _redis_finish_active(self, r: Any, session_id: str, run_id: str) -> None:
        active = _active_key(session_id)
        cur = r.get(active)
        if cur == run_id:
            r.delete(active)

    def _redis_append(
        self, r: Any, run_id: str, event_type: str, **payload: Any
    ) -> dict[str, Any]:
        event_id = int(r.incr(_seq_key(run_id)))
        event: dict[str, Any] = {"id": event_id, "type": event_type, **payload}
        raw_meta = r.get(_meta_key(run_id))
        if not raw_meta:
            raise KeyError(f"run not found: {run_id}")
        meta = json.loads(raw_meta)
        if event_type == "delta":
            meta["delta_emitted"] = True
        if event_type == "done":
            meta["done"] = True
            meta["finished_at"] = time.time()
        pipe = r.pipeline()
        pipe.rpush(_events_key(run_id), json.dumps(event, ensure_ascii=False))
        pipe.set(_meta_key(run_id), json.dumps(meta, ensure_ascii=False))
        ttl = (
            self.retain_after_done_sec
            if event_type == "done"
            else self.absolute_ttl_sec
        )
        pipe.expire(_meta_key(run_id), ttl)
        pipe.expire(_events_key(run_id), ttl)
        pipe.expire(_seq_key(run_id), ttl)
        if event_type == "done":
            pipe.delete(_active_key(str(meta["session_id"])))
        else:
            pipe.expire(_active_key(str(meta["session_id"])), self.absolute_ttl_sec)
        pipe.execute()
        return event

    def _redis_events_after(
        self, r: Any, run_id: str, after_id: int
    ) -> list[dict[str, Any]]:
        raw_list = r.lrange(_events_key(run_id), 0, -1) or []
        out: list[dict[str, Any]] = []
        for item in raw_list:
            try:
                event = json.loads(item)
            except json.JSONDecodeError:
                continue
            if int(event.get("id") or 0) > after_id:
                out.append(event)
        return out

    # ---- Memory ----

    def _memory_create(
        self,
        *,
        session_id: str,
        username: str,
        message: str,
        public_base_url: str,
    ) -> ChatRun:
        self._memory_cleanup()
        with self._lock:
            existing = self._active_by_session.get(session_id)
            if existing:
                prev = self._runs.get(existing)
                if prev is not None and not prev.done:
                    raise ConcurrentChatRunError(existing)
                self._active_by_session.pop(session_id, None)
            run_id = uuid.uuid4().hex
            run = _MemoryRun(
                run_id=run_id,
                session_id=session_id,
                username=username,
                message=message,
                public_base_url=public_base_url,
            )
            self._runs[run_id] = run
            self._active_by_session[session_id] = run_id
            return self._wrap(run, backend="memory")

    def _memory_get(self, run_id: str) -> ChatRun | None:
        self._memory_cleanup()
        with self._lock:
            run = self._runs.get(run_id)
            return self._wrap(run, backend="memory") if run else None

    def _memory_active(self, session_id: str) -> str | None:
        self._memory_cleanup()
        with self._lock:
            run_id = self._active_by_session.get(session_id)
            if not run_id:
                return None
            run = self._runs.get(run_id)
            if run is None or run.done:
                return None
            return run_id

    def _memory_finish_active(self, session_id: str, run_id: str) -> None:
        with self._lock:
            if self._active_by_session.get(session_id) == run_id:
                self._active_by_session.pop(session_id, None)

    def _memory_append(
        self, run_id: str, event_type: str, **payload: Any
    ) -> dict[str, Any]:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        with run.lock:
            event: dict[str, Any] = {
                "id": run.next_id,
                "type": event_type,
                **payload,
            }
            run.next_id += 1
            run.events.append(event)
            if event_type == "delta":
                run.delta_emitted = True
            if event_type == "done":
                run.done = True
                run.finished_at = time.time()
            return dict(event)

    def _memory_events_after(
        self, run_id: str, after_id: int
    ) -> list[dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return []
        with run.lock:
            return [dict(e) for e in run.events if int(e["id"]) > after_id]

    def _memory_cleanup(self) -> None:
        now = time.time()
        with self._lock:
            expired: list[str] = []
            for run_id, run in self._runs.items():
                age = now - run.created_at
                if age > self.absolute_ttl_sec:
                    expired.append(run_id)
                    continue
                if (
                    run.done
                    and run.finished_at is not None
                    and (now - run.finished_at) > self.retain_after_done_sec
                ):
                    expired.append(run_id)
            for run_id in expired:
                run = self._runs.pop(run_id, None)
                if run and self._active_by_session.get(run.session_id) == run_id:
                    self._active_by_session.pop(run.session_id, None)
