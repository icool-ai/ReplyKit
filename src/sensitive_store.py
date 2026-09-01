"""Sensitive-word store (P3-4): SQLAlchemy CRUD + txt/json import + in-memory cache."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, utc_now
from mp_agent.dao._engine_normalize import normalize_store_engine
from mp_agent.dao.models import SensitiveWord
from mp_agent.dao.sync_db import sync_engine
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


def _row_to_sensitive(word: SensitiveWord) -> SensitiveRow:
    return SensitiveRow(
        id=word.id,
        pattern=word.pattern,
        enabled=word.enabled,
        note=word.note,
        created_at=dt_to_unix(word.created_at),
        updated_at=dt_to_unix(word.updated_at),
    )


class SensitiveStore:
    """SQLAlchemy store for sensitive-word patterns with a hot-reloadable cache."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = normalize_store_engine(engine)
        self._patterns: tuple[str, ...] = ()
        self.reload_cache()

    def close(self) -> None:
        pass

    def reload_cache(self) -> tuple[str, ...]:
        """Reload enabled patterns into memory (call after writes)."""
        with Session(self._engine) as session:
            rows = session.execute(
                select(SensitiveWord)
                .where(SensitiveWord.enabled.is_(True))
                .order_by(SensitiveWord.id.asc())
            )
            self._patterns = tuple(
                str(w.pattern) for w in rows.scalars() if w.pattern
            )
        return self._patterns

    def enabled_patterns(self) -> tuple[str, ...]:
        return self._patterns

    def get(self, word_id: str) -> SensitiveRow | None:
        word_id = (word_id or "").strip()
        if not word_id:
            return None
        with Session(self._engine) as session:
            word = session.get(SensitiveWord, word_id)
            return _row_to_sensitive(word) if word else None

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

        with Session(self._engine) as session:
            stmt = select(SensitiveWord)
            count_stmt = select(func.count()).select_from(SensitiveWord)

            filters: list[Any] = []
            if enabled is not None:
                filters.append(SensitiveWord.enabled.is_(bool(enabled)))
            if keyword is not None and str(keyword).strip():
                kw = f"%{str(keyword).strip()}%"
                filters.append(
                    (SensitiveWord.pattern.ilike(kw)) | (SensitiveWord.note.ilike(kw))
                )

            if filters:
                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

            total = session.scalar(count_stmt) or 0
            offset = (page - 1) * page_size
            rows = session.execute(
                stmt.order_by(SensitiveWord.updated_at.desc(), SensitiveWord.id.asc())
                .limit(page_size)
                .offset(offset)
            )
            return SensitivePage(
                items=[_row_to_sensitive(w) for w in rows.scalars()],
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
        now = utc_now()

        new_id = (word_id or "").strip() or uuid.uuid4().hex

        with Session(self._engine) as session:
            if session.get(SensitiveWord, new_id):
                raise LookupError(f"敏感词 ID 已存在: {new_id}")
            existing = session.scalar(
                select(SensitiveWord).where(SensitiveWord.pattern == pattern)
            )
            if existing:
                raise LookupError(f"敏感词已存在: {pattern}")

            session.add(
                SensitiveWord(
                    id=new_id,
                    pattern=pattern,
                    enabled=bool(enabled),
                    note=note,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

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

        with Session(self._engine) as session:
            word = session.get(SensitiveWord, word_id)
            if word is None:
                raise KeyError(f"敏感词不存在: {word_id}")
            if new_pattern != current.pattern:
                clash = session.scalar(
                    select(SensitiveWord).where(
                        SensitiveWord.pattern == new_pattern,
                        SensitiveWord.id != word_id,
                    )
                )
                if clash:
                    raise LookupError(f"敏感词已存在: {new_pattern}")
            word.pattern = new_pattern
            word.note = new_note
            word.enabled = new_enabled
            word.updated_at = utc_now()
            session.commit()

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
        with Session(self._engine) as session:
            for word_id in cleaned:
                word = session.get(SensitiveWord, word_id)
                if word is not None:
                    session.delete(word)
                    deleted.append(word_id)
            session.commit()

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
        with Session(self._engine) as session:
            return session.scalar(select(func.count()).select_from(SensitiveWord)) or 0


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


@lru_cache(maxsize=4)
def _get_sensitive_store_cached(engine_identity: str) -> SensitiveStore:
    return SensitiveStore(sync_engine)


def get_sensitive_store(engine: Engine | None = None) -> SensitiveStore:
    """Shared store (engine-normalized). Pass None for default sync engine."""
    return SensitiveStore(engine)


def reload_sensitive_store(engine: Engine | None = None) -> SensitiveStore:
    _get_sensitive_store_cached.cache_clear()
    return get_sensitive_store(engine)
