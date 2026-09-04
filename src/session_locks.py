"""Per-session locks so different chat sessions can run in parallel."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class SessionLockRegistry:
    """One ``threading.Lock`` per session_id (created lazily)."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, session_id: str) -> threading.Lock:
        key = (session_id or "").strip() or "default"
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def hold(self, session_id: str) -> Iterator[None]:
        lock = self.lock_for(session_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
