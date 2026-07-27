"""Sensitive-word store (P3-4): SQLite CRUD + txt/json import + in-memory cache."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.config import PROJECT_ROOT


@dataclass(frozen=True)
class SensitiveRow:
    id: str
    pattern: str
    enabled: bool
    note: str
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pattern": self.pattern,
            "enabled": self.enabled,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SensitivePage:
    items: list[SensitiveRow]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1


@dataclass(frozen=True)
class SensitiveImportResult:
    imported: int
    skipped: int
    total_in_file: int


class SensitiveStore:
    """SQLite store for sensitive-word patterns with a hot-reloadable cache."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._patterns: tuple[str, ...] = ()
        self._init_schema()
        self.reload_cache()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sensitive_words (
                    id TEXT PRIMARY KEY,
                    pattern TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sensitive_pattern "
                "ON sensitive_words(pattern)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sensitive_updated "
                "ON sensitive_words(updated_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sensitive_enabled "
                "ON sensitive_words(enabled)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reload_cache(self) -> tuple[str, ...]:
        """Reload enabled patterns into memory (call after writes)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT pattern FROM sensitive_words
                WHERE enabled = 1
                ORDER BY id ASC
                """
            ).fetchall()
            self._patterns = tuple(str(r["pattern"]) for r in rows if r["pattern"])
        return self._patterns

    def enabled_patterns(self) -> tuple[str, ...]:
        return self._patterns

    def get(self, word_id: str) -> SensitiveRow | None:
        word_id = (word_id or "").strip()
        if not word_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sensitive_words WHERE id = ?",
                (word_id,),
            ).fetchone()
        return _row_to_sensitive(row) if row else None

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        enabled: bool | None = None,
    ) -> SensitivePage:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        where: list[str] = []
        params: list[Any] = []

        if enabled is not None:
            where.append("enabled = ?")
            params.append(1 if enabled else 0)
        if keyword is not None and str(keyword).strip():
            kw = f"%{str(keyword).strip()}%"
            where.append("(pattern LIKE ? OR note LIKE ?)")
            params.extend([kw, kw])

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM sensitive_words {clause}",
                    params,
                ).fetchone()["n"]
            )
            offset = (page - 1) * page_size
            rows = self._conn.execute(
                f"""
                SELECT * FROM sensitive_words
                {clause}
                ORDER BY updated_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

        return SensitivePage(
            items=[_row_to_sensitive(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def create(
        self,
        *,
        pattern: str,
        note: str = "",
        enabled: bool = True,
        word_id: str | None = None,
    ) -> SensitiveRow:
        pattern = (pattern or "").strip()
        if not pattern:
            raise ValueError("pattern 不能为空")
        note = (note or "").strip()
        now = int(time.time())

        with self._lock:
            new_id = (word_id or "").strip() or uuid.uuid4().hex
            existing_id = self._conn.execute(
                "SELECT 1 FROM sensitive_words WHERE id = ?",
                (new_id,),
            ).fetchone()
            if existing_id:
                raise LookupError(f"敏感词 ID 已存在: {new_id}")

            existing_pattern = self._conn.execute(
                "SELECT 1 FROM sensitive_words WHERE pattern = ?",
                (pattern,),
            ).fetchone()
            if existing_pattern:
                raise LookupError(f"敏感词已存在: {pattern}")

            self._conn.execute(
                """
                INSERT INTO sensitive_words (
                    id, pattern, enabled, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id, pattern, 1 if enabled else 0, note, now, now),
            )
        self.reload_cache()
        row = self.get(new_id)
        assert row is not None
        return row

    def update(
        self,
        word_id: str,
        *,
        pattern: str | None = None,
        note: str | None = None,
        enabled: bool | None = None,
    ) -> SensitiveRow:
        word_id = (word_id or "").strip()
        if not word_id:
            raise ValueError("id 不能为空")

        current = self.get(word_id)
        if current is None:
            raise KeyError(f"敏感词不存在: {word_id}")

        if pattern is None and note is None and enabled is None:
            raise ValueError("至少需要更新一个字段")

        new_pattern = current.pattern if pattern is None else pattern.strip()
        if not new_pattern:
            raise ValueError("pattern 不能为空")
        new_note = current.note if note is None else note.strip()
        new_enabled = current.enabled if enabled is None else bool(enabled)
        now = int(time.time())

        with self._lock:
            if new_pattern != current.pattern:
                clash = self._conn.execute(
                    "SELECT 1 FROM sensitive_words WHERE pattern = ? AND id != ?",
                    (new_pattern, word_id),
                ).fetchone()
                if clash:
                    raise LookupError(f"敏感词已存在: {new_pattern}")

            self._conn.execute(
                """
                UPDATE sensitive_words SET
                    pattern = ?, note = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_pattern,
                    new_note,
                    1 if new_enabled else 0,
                    now,
                    word_id,
                ),
            )
        self.reload_cache()
        row = self.get(word_id)
        assert row is not None
        return row

    def delete_many(self, ids: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            word_id = str(raw or "").strip()
            if not word_id or word_id in seen:
                continue
            seen.add(word_id)
            cleaned.append(word_id)
        if not cleaned:
            raise ValueError("ids 不能为空")

        deleted: list[str] = []
        with self._lock:
            for word_id in cleaned:
                cur = self._conn.execute(
                    "DELETE FROM sensitive_words WHERE id = ?",
                    (word_id,),
                )
                if cur.rowcount > 0:
                    deleted.append(word_id)
        if deleted:
            self.reload_cache()
        return deleted

    def import_patterns(
        self,
        patterns: list[str],
        *,
        notes: list[str] | None = None,
        enabled: bool = True,
    ) -> SensitiveImportResult:
        """Insert new patterns; skip duplicates. Does not auto-run on startup."""
        notes = notes or []
        imported = 0
        skipped = 0
        total = 0

        for i, raw in enumerate(patterns):
            pattern = str(raw or "").strip()
            if not pattern:
                continue
            total += 1
            note = str(notes[i]).strip() if i < len(notes) else ""
            try:
                self.create(pattern=pattern, note=note, enabled=enabled)
                imported += 1
            except LookupError:
                skipped += 1

        return SensitiveImportResult(
            imported=imported,
            skipped=skipped,
            total_in_file=total,
        )

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sensitive_words"
            ).fetchone()
        return int(row["n"] if row else 0)


def load_patterns_from_path(path: Path) -> list[str]:
    """Load patterns from ``.txt`` (one per line) or ``.json`` (string array / objects)."""
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"敏感词文件不存在: {path}")

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _parse_json_patterns(text, source=str(path))
    return _parse_txt_patterns(text)


def load_patterns_from_url(url: str) -> list[str]:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url 仅支持 http:// 或 https://")
    if not parsed.netloc:
        raise ValueError("url 无效")

    req = Request(
        url,
        headers={"User-Agent": "replykit/0.1", "Accept": "*/*"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            content_type = (resp.headers.get("Content-Type") or "").lower()
    except HTTPError as exc:
        raise ValueError(f"拉取敏感词 URL 失败: HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError(f"拉取敏感词 URL 失败: {exc.reason}") from exc

    path_lower = (parsed.path or "").lower()
    if "json" in content_type or path_lower.endswith(".json"):
        return _parse_json_patterns(body, source=url)
    return _parse_txt_patterns(body)


def resolve_sensitive_import_path(path: str) -> Path:
    p = Path(path.strip())
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _parse_txt_patterns(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _parse_json_patterns(text: str, *, source: str) -> list[str]:
    raw = json.loads(text)
    if isinstance(raw, dict) and "sensitive_words" in raw:
        raw = raw["sensitive_words"]
    if not isinstance(raw, list):
        raise ValueError(f"敏感词 JSON 须为数组（或含 sensitive_words 数组）：{source}")

    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            pattern = item.strip()
            if pattern:
                out.append(pattern)
        elif isinstance(item, dict):
            pattern = str(item.get("pattern") or "").strip()
            if pattern:
                out.append(pattern)
    return out


def _row_to_sensitive(row: sqlite3.Row) -> SensitiveRow:
    return SensitiveRow(
        id=str(row["id"]),
        pattern=str(row["pattern"] or ""),
        enabled=bool(row["enabled"]),
        note=str(row["note"] or ""),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


@lru_cache(maxsize=4)
def _get_sensitive_store_cached(resolved_path: str) -> SensitiveStore:
    return SensitiveStore(Path(resolved_path))


def get_sensitive_store(path: str | Path | None = None) -> SensitiveStore:
    """Shared store (path-normalized). Pass None for ``SENSITIVE_DB_PATH``."""
    from src.config import get_settings

    if path is None:
        resolved = str(get_settings().sensitive_db_path.resolve())
    else:
        resolved = str(Path(path).resolve())
    return _get_sensitive_store_cached(resolved)


def reload_sensitive_store(path: Path | None = None) -> SensitiveStore:
    _get_sensitive_store_cached.cache_clear()
    return get_sensitive_store(path)
