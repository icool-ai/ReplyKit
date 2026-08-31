"""FAQ source-of-truth store (P3-3): SQLAlchemy CRUD + JSON import."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, utc_now
from mp_agent.dao.models import Faq
from mp_agent.dao.sync_db import sync_engine
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


def _row_to_faq(faq: Faq) -> FaqRow:
    similar = list(faq.similar or [])
    return FaqRow(
        id=faq.id,
        category=faq.category,
        question=faq.question,
        answer=faq.answer,
        similar=[str(s).strip() for s in similar if str(s).strip()],
        enabled=faq.enabled,
        created_at=dt_to_unix(faq.created_at),
        updated_at=dt_to_unix(faq.updated_at),
    )


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


class FaqStore:
    """SQLAlchemy store for structured FAQ entries."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine or sync_engine

    def count_enabled(self) -> int:
        with Session(self._engine) as session:
            return session.scalar(
                select(func.count()).where(Faq.enabled.is_(True))
            ) or 0

    def list_enabled(self) -> list[FaqRow]:
        with Session(self._engine) as session:
            rows = session.execute(select(Faq).where(Faq.enabled.is_(True)).order_by(Faq.id))
            return [_row_to_faq(r) for r in rows.scalars()]

    def get(self, faq_id: str) -> FaqRow | None:
        faq_id = (faq_id or "").strip()
        if not faq_id:
            return None
        with Session(self._engine) as session:
            faq = session.get(Faq, faq_id)
            return _row_to_faq(faq) if faq else None

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

        with Session(self._engine) as session:
            stmt = select(Faq)
            count_stmt = select(func.count()).select_from(Faq)

            filters: list[Any] = []
            if category is not None and str(category).strip():
                filters.append(Faq.category == str(category).strip())
            if enabled is not None:
                filters.append(Faq.enabled.is_(bool(enabled)))
            if keyword is not None and str(keyword).strip():
                kw = f"%{str(keyword).strip()}%"
                filters.append(
                    (Faq.question.ilike(kw))
                    | (Faq.answer.ilike(kw))
                    | (Faq.similar.cast(str).ilike(kw))
                )

            if filters:
                for f in filters:
                    stmt = stmt.where(f)
                    count_stmt = count_stmt.where(f)

            total = session.scalar(count_stmt) or 0
            offset = (page - 1) * page_size
            rows = session.execute(
                stmt.order_by(Faq.updated_at.desc(), Faq.id.asc())
                .limit(page_size)
                .offset(offset)
            )
            return FaqPage(
                items=[_row_to_faq(r) for r in rows.scalars()],
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
        now = utc_now()
        new_id = (faq_id or "").strip() or self._next_id()

        with Session(self._engine) as session:
            if session.get(Faq, new_id):
                raise LookupError(f"FAQ ID 已存在: {new_id}")

            session.add(
                Faq(
                    id=new_id,
                    category=category,
                    question=question,
                    answer=answer,
                    similar=similar_list,
                    enabled=bool(enabled),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

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

        if (
            question is None
            and answer is None
            and similar is None
            and category is None
            and enabled is None
        ):
            raise ValueError("至少需要更新一个字段")

        new_question = current.question if question is None else question.strip()
        new_answer = current.answer if answer is None else answer.strip()
        if not new_question or not new_answer:
            raise ValueError("question 与 answer 不能为空")
        new_similar = current.similar if similar is None else _normalize_similar(similar)
        new_category = current.category if category is None else category.strip()
        new_enabled = current.enabled if enabled is None else bool(enabled)

        with Session(self._engine) as session:
            faq = session.get(Faq, faq_id)
            if faq is None:
                raise KeyError(f"FAQ 不存在: {faq_id}")
            faq.question = new_question
            faq.answer = new_answer
            faq.similar = new_similar
            faq.category = new_category
            faq.enabled = new_enabled
            faq.updated_at = utc_now()
            session.commit()

        row = self.get(faq_id)
        assert row is not None
        return row

    def delete_many(self, ids: list[str]) -> list[str]:
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
        with Session(self._engine) as session:
            for faq_id in cleaned:
                faq = session.get(Faq, faq_id)
                if faq is not None:
                    session.delete(faq)
                    deleted.append(faq_id)
            session.commit()
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

    def _next_id(self) -> str:
        """Next id as plain decimal string: 1, 2, 3, ..."""
        with Session(self._engine) as session:
            rows = session.execute(select(Faq.id))
            max_n = 0
            for (raw,) in rows:
                text = str(raw).strip()
                if text.isdigit():
                    max_n = max(max_n, int(text))
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


@lru_cache(maxsize=4)
def _get_faq_store_cached(engine_identity: str) -> FaqStore:
    return FaqStore(sync_engine)


def get_faq_store(engine: Engine | None = None) -> FaqStore:
    """Shared store (engine-normalized). Pass None for default sync engine."""
    return FaqStore(engine)


def reload_faq_store() -> FaqStore:
    _get_faq_store_cached.cache_clear()
    return get_faq_store()
