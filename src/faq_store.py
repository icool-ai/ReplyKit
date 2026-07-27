"""FAQ source-of-truth store (P3-3): SQLite CRUD + JSON import."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from src.config import PROJECT_ROOT


@dataclass(frozen=True)
class FaqRow:
    id: str
    category: str
    question: str
    answer: str
    similar: list[str]
    enabled: bool
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "question": self.question,
            "answer": self.answer,
            "similar": list(self.similar),
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class FaqPage:
    items: list[FaqRow]
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
class FaqImportResult:
    imported: int
    total_in_file: int
    touched_ids: list[str]


class FaqStore:
    """SQLite store for structured FAQ entries."""

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
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS faqs (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    similar TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_faqs_updated "
                "ON faqs(updated_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_faqs_category "
                "ON faqs(category)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_faqs_enabled "
                "ON faqs(enabled)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def count_enabled(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM faqs WHERE enabled = 1"
            ).fetchone()
        return int(row["n"] if row else 0)

    def list_enabled(self) -> list[FaqRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM faqs
                WHERE enabled = 1
                ORDER BY id ASC
                """
            ).fetchall()
        return [_row_to_faq(r) for r in rows]

    def get(self, faq_id: str) -> FaqRow | None:
        faq_id = (faq_id or "").strip()
        if not faq_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM faqs WHERE id = ?",
                (faq_id,),
            ).fetchone()
        return _row_to_faq(row) if row else None

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        category: str | None = None,
        keyword: str | None = None,
        enabled: bool | None = None,
    ) -> FaqPage:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        where: list[str] = []
        params: list[Any] = []

        if category is not None and str(category).strip():
            where.append("category = ?")
            params.append(str(category).strip())
        if enabled is not None:
            where.append("enabled = ?")
            params.append(1 if enabled else 0)
        if keyword is not None and str(keyword).strip():
            kw = f"%{str(keyword).strip()}%"
            where.append(
                "(question LIKE ? OR answer LIKE ? OR similar LIKE ?)"
            )
            params.extend([kw, kw, kw])

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM faqs {clause}",
                    params,
                ).fetchone()["n"]
            )
            offset = (page - 1) * page_size
            rows = self._conn.execute(
                f"""
                SELECT * FROM faqs
                {clause}
                ORDER BY updated_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

        return FaqPage(
            items=[_row_to_faq(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def create(
        self,
        *,
        question: str,
        answer: str,
        similar: list[str] | None = None,
        category: str = "",
        faq_id: str | None = None,
        enabled: bool = True,
    ) -> FaqRow:
        question = (question or "").strip()
        answer = (answer or "").strip()
        if not question or not answer:
            raise ValueError("question 与 answer 不能为空")

        similar_list = _normalize_similar(similar)
        category = (category or "").strip()
        now = int(time.time())

        with self._lock:
            new_id = (faq_id or "").strip() or self._next_id_unlocked()
            existing = self._conn.execute(
                "SELECT 1 FROM faqs WHERE id = ?",
                (new_id,),
            ).fetchone()
            if existing:
                raise LookupError(f"FAQ ID 已存在: {new_id}")

            self._conn.execute(
                """
                INSERT INTO faqs (
                    id, category, question, answer, similar,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    category,
                    question,
                    answer,
                    json.dumps(similar_list, ensure_ascii=False),
                    1 if enabled else 0,
                    now,
                    now,
                ),
            )
        row = self.get(new_id)
        assert row is not None
        return row

    def update(
        self,
        faq_id: str,
        *,
        question: str | None = None,
        answer: str | None = None,
        similar: list[str] | None = None,
        category: str | None = None,
        enabled: bool | None = None,
    ) -> FaqRow:
        faq_id = (faq_id or "").strip()
        if not faq_id:
            raise ValueError("id 不能为空")

        current = self.get(faq_id)
        if current is None:
            raise KeyError(f"FAQ 不存在: {faq_id}")

        new_question = (
            current.question if question is None else question.strip()
        )
        new_answer = current.answer if answer is None else answer.strip()
        if not new_question or not new_answer:
            raise ValueError("question 与 answer 不能为空")

        if (
            question is None
            and answer is None
            and similar is None
            and category is None
            and enabled is None
        ):
            raise ValueError("至少需要更新一个字段")

        new_similar = (
            current.similar if similar is None else _normalize_similar(similar)
        )
        new_category = (
            current.category if category is None else category.strip()
        )
        new_enabled = current.enabled if enabled is None else bool(enabled)
        now = int(time.time())

        with self._lock:
            self._conn.execute(
                """
                UPDATE faqs SET
                    category = ?, question = ?, answer = ?, similar = ?,
                    enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_category,
                    new_question,
                    new_answer,
                    json.dumps(new_similar, ensure_ascii=False),
                    1 if new_enabled else 0,
                    now,
                    faq_id,
                ),
            )
        row = self.get(faq_id)
        assert row is not None
        return row

    def delete_many(self, ids: list[str]) -> list[str]:
        """Delete existing ids; missing ids are ignored. Returns deleted ids."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            faq_id = str(raw or "").strip()
            if not faq_id or faq_id in seen:
                continue
            seen.add(faq_id)
            cleaned.append(faq_id)
        if not cleaned:
            raise ValueError("ids 不能为空")

        deleted: list[str] = []
        with self._lock:
            for faq_id in cleaned:
                cur = self._conn.execute(
                    "DELETE FROM faqs WHERE id = ?",
                    (faq_id,),
                )
                if cur.rowcount > 0:
                    deleted.append(faq_id)
        return deleted

    def import_entries(self, entries: list[dict[str, Any]]) -> FaqImportResult:
        """Append all valid entries as new rows; ignore any ``id`` in the file."""
        imported = 0
        touched: list[str] = []
        total = 0

        for item in entries:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question", "")).strip()
            answer = str(item.get("answer", "")).strip()
            if not question or not answer:
                continue
            total += 1
            similar = item.get("similar") or []
            if isinstance(similar, str):
                similar = [similar]
            category = str(item.get("category") or "").strip()
            enabled_raw = item.get("enabled", True)
            enabled = bool(enabled_raw) if enabled_raw is not None else True

            row = self.create(
                question=question,
                answer=answer,
                similar=[str(s) for s in similar],
                category=category,
                faq_id=None,
                enabled=enabled,
            )
            imported += 1
            touched.append(row.id)

        return FaqImportResult(
            imported=imported,
            total_in_file=total,
            touched_ids=touched,
        )

    def _next_id_unlocked(self) -> str:
        """Next id as plain decimal string: 1, 2, 3, ..."""
        rows = self._conn.execute("SELECT id FROM faqs").fetchall()
        max_n = 0
        for row in rows:
            raw = str(row["id"]).strip()
            if raw.isdigit():
                max_n = max(max_n, int(raw))
        return str(max_n + 1)


def load_faq_entries_from_path(path: Path) -> list[dict[str, Any]]:
    from src.faq_import import parse_faq_path

    return parse_faq_path(path)


def load_faq_entries_from_url(url: str) -> list[dict[str, Any]]:
    from src.faq_import import parse_faq_url

    return parse_faq_url(url)


def resolve_import_path(path: str) -> Path:
    """Resolve local import path relative to project root when not absolute."""
    p = Path(path.strip())
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _normalize_similar(similar: list[str] | None) -> list[str]:
    if not similar:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in similar:
        text = str(item or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _row_to_faq(row: sqlite3.Row) -> FaqRow:
    similar_raw = row["similar"] or "[]"
    try:
        similar = json.loads(similar_raw)
    except json.JSONDecodeError:
        similar = []
    if isinstance(similar, str):
        similar = [similar]
    if not isinstance(similar, list):
        similar = []
    return FaqRow(
        id=str(row["id"]),
        category=str(row["category"] or ""),
        question=str(row["question"] or ""),
        answer=str(row["answer"] or ""),
        similar=[str(s).strip() for s in similar if str(s).strip()],
        enabled=bool(row["enabled"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )
