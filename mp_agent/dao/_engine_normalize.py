"""Normalize an `engine` argument passed to legacy Store ctors.

History: Store constructors declared `engine: Engine | None = None` to accept
a SQLAlchemy engine, defaulting to the shared sync engine from
``mp_agent.dao.sync_db``. During migration, callers sometimes passed a
``pathlib.Path`` (e.g. ``settings.faq_db_path``) thinking it built a
per-store SQLite file; that caused confusing errors like::

    AttributeError: 'WindowsPath' object has no attribute 'connect'

This helper accepts:

* ``None`` / omitted -> returns the global ``sync_engine``
* a SQLAlchemy ``Engine`` -> returned as-is
* a ``Path`` / ``str`` -> **ignores the path** (we've long moved to single
  shared database via ``DB_URL``/``sync_engine``) and returns ``sync_engine``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from mp_agent.dao.sync_db import sync_engine


def normalize_store_engine(engine: Any) -> Engine:
    if isinstance(engine, Engine):
        return engine
    # Path / str -> legacy "per-store path" input; single shared DB now.
    if engine is None or isinstance(engine, (Path, str)):
        return sync_engine
    # Fallback: allow any duck-typed engine-like object.
    return engine  # type: ignore[return-value]
