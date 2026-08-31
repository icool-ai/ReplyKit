"""Small helpers shared by SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def dt_to_unix(dt: datetime | None) -> int:
    """Convert a datetime to Unix seconds (0 if None)."""
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def unix_to_dt(ts: int | float | None) -> datetime:
    """Convert Unix seconds to timezone-aware UTC datetime."""
    if ts is None:
        return utc_now()
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)
